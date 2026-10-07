#!/usr/bin/env bash

# ---
# title: "MongoDB Query Tuning"
# description: "Collects data for MongoDB query tuning: database stats, collection stats with index details, index definitions, and optionally an allPlansExecution explain plan for a given query."
# allow_extra_args: false
# parameters:
#  - name: host
#    type: str
#    label: MongoDB host
#    description: Hostname or IP address of the MongoDB instance
#    default: 127.0.0.1
#  - name: port
#    type: int
#    label: MongoDB port
#    description: TCP port of the MongoDB instance
#    default: 27017
#    ge: 1
#    le: 65535
#  - name: user
#    type: str
#    label: MongoDB user
#    description: Username for MongoDB authentication. Leave empty if auth is disabled.
#    default: ""
#  - name: password
#    type: str
#    label: MongoDB password
#    description: Password for MongoDB authentication. Leave empty if auth is disabled.
#    default: ""
#  - name: auth-database
#    type: str
#    label: Authentication database
#    description: Database used for authenticating the user.
#    default: admin
#  - name: database
#    type: str
#    label: Database name
#    description: The database containing the collection to analyze.
#  - name: collection
#    type: str
#    label: Collection name
#    description: The collection to analyze.
#  - name: query
#    type: str
#    label: Query to tune
#    description: "MongoDB query method to explain, e.g.: find({status:\"active\"}) or aggregate([{\"$match\":{status:\"active\"}}]). If omitted, only stats and index data are collected."
#    default: ""
#  - name: execute
#    type: bool
#    label: Execute diagnostic commands
#    description: If enabled, the diagnostic commands will be executed and results saved to output files.
#    default: false
#  - name: help
#    type: bool
#    label: Show help message
#    description: Print the script's usage text instead of running it.
#    default: false
# service_type: mongodb
# diagnostic_categories:
#   - QUERY_TUNING_OPTIMIZATION
# ---

# Usage: ./mongodb_query_tuning.sh --database=DB --collection=COLL [--query=METHOD] [--execute] [OPTIONS]
# Example: ./mongodb_query_tuning.sh --database=mydb --collection=orders --query='find({status:"pending"})' --execute

set -euo pipefail

HOST="127.0.0.1"
PORT=27017
USER=""
PASSWORD=""
AUTH_DB="admin"
DATABASE=""
COLLECTION=""
QUERY=""
DEST=
EXECUTE=0

usage() {
    local -i exit_code="${1:-0}"
    cat << EOS
Usage: $(basename "${0}") [OPTIONS]
Collects data for MongoDB query tuning.

This snippet gathers diagnostic data to support query performance analysis.
It always writes a commands.sh file with the equivalent mongosh commands.
When --execute is set, it also runs those commands and saves the output to
separate .out files, then compresses everything into a tar.gz archive.

Data collected:
  dbStats.out       -- Database-level statistics (dbStats)
  collStats.out     -- Collection stats with per-index details
  getIndexes.out    -- All index definitions for the collection
  query.out         -- allPlansExecution explain plan (only if --query is set)

Command line options:

   --host             MongoDB hostname or IP (default: 127.0.0.1)
   --port             MongoDB port (default: 27017)
   --user             MongoDB username
   --password         MongoDB password
   --auth-database    Authentication database (default: admin)
   -D, --database     Database containing the collection (required)
   -C, --collection   Collection to analyze (required)
   -q, --query        MongoDB query method to explain, e.g.:
                        find({status:"active"})
                        aggregate([{"\$match":{status:"active"}}])
                      Use double quotes for string literals in the query.
                      If omitted, only stats and index data are collected.
   -d, --dest         Destination directory for output files.
                      Default: \$(pwd)/\$(hostname)-\$(date +%Y-%m-%d-%H-%M-%S)
   -e, --execute      Execute diagnostic commands and save results.
   -h, --help         Show this help message

EOS
    exit "${exit_code}"
}

compress_data() {
    tar czf "${DEST}.tar.gz" -C "$(dirname "${DEST}")" "$(basename "${DEST}")"
}

if ! OPTS=$(getopt --options D:C:q:d:eh --longoptions 'host:,port:,user:,password:,auth-database:,database:,collection:,query:,dest:,execute,help' -- "$@"); then
    echo "Error parsing options" >&2
    usage 1
fi

eval set -- "$OPTS"

while [[ -n $* ]]; do
    case "$1" in
        --host)
            HOST="$2"
            shift 2
            ;;
        --port)
            PORT="$2"
            shift 2
            ;;
        --user)
            USER="$2"
            shift 2
            ;;
        --password)
            PASSWORD="$2"
            shift 2
            ;;
        --auth-database)
            AUTH_DB="$2"
            shift 2
            ;;
        -D | --database)
            DATABASE="$2"
            shift 2
            ;;
        -C | --collection)
            COLLECTION="$2"
            shift 2
            ;;
        -q | --query)
            QUERY="$2"
            shift 2
            ;;
        -d | --dest)
            DEST="$2"
            shift 2
            ;;
        -e | --execute)
            EXECUTE=1
            shift 1
            ;;
        -h | --help)
            usage
            ;;
        --)
            shift 1
            break
            ;;
        *)
            echo "Unrecognized option '$1'" >&2
            usage 1
            ;;
    esac
done

if [[ -z $DATABASE ]]; then
    echo "Error: --database is required." >&2
    usage 1
fi

if [[ -z $COLLECTION ]]; then
    echo "Error: --collection is required." >&2
    usage 1
fi

EXPLAIN_ORDER="after"
if [[ -n $QUERY ]]; then
    QUERY_METHOD=$(echo "$QUERY" | grep -oP '^\s*\K\w+' 2> /dev/null || true)
    case "${QUERY_METHOD,,}" in
        insertone | insertmany | insert)
            echo "Warning: explain() does not support insert operations ('${QUERY_METHOD}'). Skipping explain." >&2
            QUERY=""
            ;;
        find | findone | aggregate)
            EXPLAIN_ORDER="after"
            ;;
        *)
            EXPLAIN_ORDER="before"
            ;;
    esac
fi

test -n "${DEST}" || DEST="$(pwd)/$(hostname)-$(date +%Y-%m-%d-%H-%M-%S)"
mkdir "${DEST}"

# Prefer mongosh over the legacy mongo shell
MONGO_BIN=""
if command -v mongosh > /dev/null 2>&1; then
    MONGO_BIN="mongosh"
elif command -v mongo > /dev/null 2>&1; then
    MONGO_BIN="mongo"
fi

MONGO_ARGS=(--host "$HOST" --port "$PORT")

js_escape() {
    local value="$1"
    value=${value//\\/\\\\}
    value=${value//\'/\\\'}
    value=${value//$'\n'/\\n}
    value=${value//$'\r'/\\r}
    value=${value//$'\t'/\\t}
    printf '%s' "$value"
}

mongo_eval() {
    local script="$1"
    local outfile="$2"
    local auth_prefix=""
    if [ -z "$MONGO_BIN" ]; then
        echo "Neither mongosh nor mongo is installed; skipping: $outfile" > "$outfile"
        return
    fi
    if [ -n "$USER" ] && [ -n "$PASSWORD" ]; then
        local escaped_auth_db escaped_user escaped_password
        escaped_auth_db="$(js_escape "$AUTH_DB")"
        escaped_user="$(js_escape "$USER")"
        escaped_password="$(js_escape "$PASSWORD")"
        auth_prefix="db = db.getSiblingDB('${escaped_auth_db}'); if (!db.auth('${escaped_user}', '${escaped_password}')) { quit(1); }"
    fi
    printf '%s\n%s\n' "$auth_prefix" "$script" |
        "$MONGO_BIN" "${MONGO_ARGS[@]}" --quiet > "$outfile" 2>&1
}

ESC_DB="$(js_escape "$DATABASE")"
ESC_COLL="$(js_escape "$COLLECTION")"

JS_DBSTATS="JSON.stringify(db.getSiblingDB('${ESC_DB}').runCommand({dbStats: 1}), null, 2)"
JS_COLLSTATS="JSON.stringify(db.getSiblingDB('${ESC_DB}').getCollection('${ESC_COLL}').stats({indexDetails: true}), null, 2)"
JS_INDEXES="JSON.stringify(db.getSiblingDB('${ESC_DB}').getCollection('${ESC_COLL}').getIndexes(), null, 2)"

# Write commands.sh so the user can review and re-run manually
CMDS_FILE="${DEST}/commands.sh"
# shellcheck disable=SC2016
{
    echo "#!/usr/bin/env bash"
    echo "# MongoDB query tuning commands for ${DATABASE}.${COLLECTION}"
    echo "# Generated: $(date)"
    echo "# Re-run manually: bash commands.sh"
    echo ""
    echo 'MONGO_BIN=${MONGO_BIN:-mongosh}'
    echo 'DEST=${DEST:-.}'
    echo ""
    echo "# Database stats"
    echo "\"\$MONGO_BIN\" --host '${HOST}' --port ${PORT} --quiet \\"
    echo "  --eval 'printjson(db.getSiblingDB(\"${DATABASE}\").runCommand({dbStats: 1}))' \\"
    echo '  > "$DEST/dbStats.out"'
    echo ""
    echo "# Collection stats with index details"
    echo "\"\$MONGO_BIN\" --host '${HOST}' --port ${PORT} --quiet \\"
    echo "  --eval 'printjson(db.getSiblingDB(\"${DATABASE}\").getCollection(\"${COLLECTION}\").stats({indexDetails: true}))' \\"
    echo '  > "$DEST/collStats.out"'
    echo ""
    echo "# Collection index definitions"
    echo "\"\$MONGO_BIN\" --host '${HOST}' --port ${PORT} --quiet \\"
    echo "  --eval 'printjson(db.getSiblingDB(\"${DATABASE}\").getCollection(\"${COLLECTION}\").getIndexes())' \\"
    echo '  > "$DEST/getIndexes.out"'
    if [[ -n $QUERY ]]; then
        echo ""
        echo "# Query explain (allPlansExecution)"
        if [[ $EXPLAIN_ORDER == "after" ]]; then
            echo "\"\$MONGO_BIN\" --host '${HOST}' --port ${PORT} --quiet \\"
            echo "  --eval 'printjson(db.getSiblingDB(\"${DATABASE}\").getCollection(\"${COLLECTION}\").${QUERY}.explain(\"allPlansExecution\"))' \\"
            echo '  > "$DEST/query.out"'
        else
            echo "\"\$MONGO_BIN\" --host '${HOST}' --port ${PORT} --quiet \\"
            echo "  --eval 'printjson(db.getSiblingDB(\"${DATABASE}\").getCollection(\"${COLLECTION}\").explain(\"allPlansExecution\").${QUERY})' \\"
            echo '  > "$DEST/query.out"'
        fi
    fi
} > "${CMDS_FILE}"
chmod +x "${CMDS_FILE}"

[ "${EXTENSIONS_DEBUG:-}" ] && echo "Written commands to: ${CMDS_FILE}"

# Execute diagnostics and save output if requested
if [ $EXECUTE -eq 1 ]; then
    if [ -z "$MONGO_BIN" ]; then
        echo "Error: Neither mongosh nor mongo is installed. Cannot execute commands." >&2
        compress_data
        exit 2
    fi

    echo "Collecting database stats..."
    mongo_eval "$JS_DBSTATS" "${DEST}/dbStats.out"

    echo "Collecting collection stats with index details..."
    mongo_eval "$JS_COLLSTATS" "${DEST}/collStats.out"

    echo "Collecting index definitions..."
    mongo_eval "$JS_INDEXES" "${DEST}/getIndexes.out"

    if [[ -n $QUERY ]]; then
        echo 'Running explain("allPlansExecution")...'
        if [[ $EXPLAIN_ORDER == "after" ]]; then
            JS_EXPLAIN="JSON.stringify(db.getSiblingDB('${ESC_DB}').getCollection('${ESC_COLL}').${QUERY}.explain('allPlansExecution'), null, 2)"
        else
            JS_EXPLAIN="JSON.stringify(db.getSiblingDB('${ESC_DB}').getCollection('${ESC_COLL}').explain('allPlansExecution').${QUERY}, null, 2)"
        fi
        mongo_eval "$JS_EXPLAIN" "${DEST}/query.out"
    fi
fi

[ "${EXTENSIONS_DEBUG:-}" ] && echo "Compressing results to: ${DEST}.tar.gz"
compress_data
echo "Output archive: ${DEST}.tar.gz"
