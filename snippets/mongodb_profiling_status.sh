#!/usr/bin/env bash

# ---
# title: "MongoDB Profiling Status"
# description: "Captures the database profiler status from a MongoDB instance via db.getProfilingStatus() — profiling level, slowms threshold, sample rate and filter — for every database, or for a single named one."
# allow_extra_args: false
# sudo: optional
# parameters:
#  - name: host
#    type: str
#    label: MongoDB host
#    description: Hostname or IP of the MongoDB instance.
#    default: localhost
#  - name: port
#    type: int
#    label: MongoDB port
#    description: TCP port of the MongoDB instance.
#    default: 27017
#    ge: 1
#    le: 65535
#  - name: user
#    type: str
#    label: MongoDB user
#    description: Username for MongoDB authentication. Provide together with the password, or leave both empty if auth is disabled.
#  - name: password
#    type: str
#    label: MongoDB password
#    description: Password for MongoDB authentication. Provide together with the user, or leave both empty if auth is disabled.
#  - name: auth-database
#    type: str
#    label: Authentication database
#    description: Database used for authenticating the user.
#    default: admin
#  - name: database
#    type: str
#    label: Database
#    description: Database whose profiling status to report. Leave empty to report every database.
# diagnostic_categories:
#  - QUERY_TUNING_OPTIMIZATION
#  - OVERALL_SLOWNESS
# service_type: mongodb
# alerts:
#   - MongoDBInstanceNotAvailable
# ---

# mongodb_profiling_status.sh
#
# Captures the database profiler status from a MongoDB instance with
# db.getProfilingStatus(): the profiling level, the slowms threshold, the
# sample rate and (on newer servers) the profiler filter.
#
# The profiler is configured per database, so by default this reports the
# status of every database returned by listDatabases. Pass --database to
# report a single database instead.
#
# The credentials reach the MongoDB shell over stdin and never appear on its
# command line. A --password given to this script is still visible in this
# script's own argv.
#
# Usage:
#   ./mongodb_profiling_status.sh [--host=HOST] [--port=PORT] [--user=USER] \
#       [--password=PASS] [--auth-database=DB] [--database=NAME]

set -euo pipefail

HOST="localhost"
PORT=27017
USER=""
PASSWORD=""
AUTH_DB="admin"
DATABASE=""

usage() {
    local -i exit_code="${1:-0}"
    cat << EOS
Usage: $(basename "$0") [OPTIONS]
Capture db.getProfilingStatus() from a MongoDB instance.

Command line options:

   --host             MongoDB host (default: localhost)
   --port             MongoDB port (default: 27017)
   --user             MongoDB user (provide together with --password)
   --password         MongoDB password (provide together with --user)
   --auth-database    Authentication database (default: admin)
   --database         Database to report (default: every database)
   -h, --help         Show this help message
EOS
    exit "${exit_code}"
}

if ! OPTS=$(getopt --options h --longoptions 'host:,port:,user:,password:,auth-database:,database:,help' -- "$@"); then
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
        --database)
            DATABASE="$2"
            shift 2
            ;;
        -h | --help) usage ;;
        --)
            shift
            break
            ;;
        *)
            echo "Unrecognized option '$1'" >&2
            usage 1
            ;;
    esac
done

# A password with no username (or vice versa) cannot authenticate; reject the
# partial combination instead of silently connecting unauthenticated.
if { [ -n "$USER" ] && [ -z "$PASSWORD" ]; } || { [ -z "$USER" ] && [ -n "$PASSWORD" ]; }; then
    echo "Error: --user and --password must be provided together, or both omitted." >&2
    usage 1
fi

# Pick the MongoDB shell binary, preferring mongosh.
MONGO_BIN=""
if command -v mongosh > /dev/null 2>&1; then
    MONGO_BIN="mongosh"
elif command -v mongo > /dev/null 2>&1; then
    MONGO_BIN="mongo"
fi

if [ -z "$MONGO_BIN" ]; then
    echo "Neither mongosh nor mongo is installed; install one to run this snippet." >&2
    exit 2
fi

MONGO_ARGS=(--host "$HOST" --port "$PORT")
MONGO_ENDPOINT="$HOST:$PORT"

js_escape() {
    local value="$1"
    value=${value//\\/\\\\}
    value=${value//\'/\\\'}
    value=${value//$'\n'/\\n}
    value=${value//$'\r'/\\r}
    value=${value//$'\t'/\\t}
    printf '%s' "$value"
}

# The script stays on --eval and only the credentials travel over stdin: piped
# into mongosh, a script runs as a REPL that ignores a failed db.auth().
MONGO_AUTH_FAILED=3
MONGO_AUTH_JS="var __creds = (typeof require === 'function' ? require('fs').readFileSync(0, 'utf8') : cat('/dev/stdin')).split('\n'), __ok = false;
try { var __dec = function (h) { return decodeURIComponent(h.replace(/(..)/g, '%\$1')); }; __ok = db.getSiblingDB(__dec(__creds[0])).auth(__dec(__creds[1]), __dec(__creds[2])); } catch (e) { __ok = false; }
if (!__ok) { quit($MONGO_AUTH_FAILED); }"

# Hex-encode each value on its own line: a MongoDB user name may contain a line
# break, which would otherwise split the three-line framing.
mongo_credentials() {
    local value
    for value in "$AUTH_DB" "$USER" "$PASSWORD"; do
        printf '%s' "$value" | od -An -tx1 -v | tr -d ' \n'
        printf '\n'
    done
}

mongo_shell() {
    local script="$1"
    if [ -z "$USER" ]; then
        "$MONGO_BIN" "${MONGO_ARGS[@]}" --quiet --eval "$script"
        return
    fi
    mongo_credentials |
        "$MONGO_BIN" "${MONGO_ARGS[@]}" --quiet --eval "$MONGO_AUTH_JS
$script"
}

check_mongo_auth() {
    [ -n "$USER" ] || return 0
    local rc=0
    mongo_shell "quit(0)" > /dev/null 2>&1 || rc=$?
    if [ "$rc" -eq "$MONGO_AUTH_FAILED" ]; then
        echo "Error: MongoDB authentication failed for user '$USER' on authentication database '$AUTH_DB' at $MONGO_ENDPOINT." >&2
        echo "Check the user, password and authentication database." >&2
        exit 1
    fi
}

# Build the profiling-status expression: a single database when --database is
# given, or every database reported by listDatabases otherwise. The database
# name is escaped for a JavaScript single-quoted string before being inlined.
if [ -n "$DATABASE" ]; then
    escaped_database="$(js_escape "$DATABASE")"
    PROFILING_SCRIPT="JSON.stringify({ db: '$escaped_database', status: db.getSiblingDB('$escaped_database').getProfilingStatus() }, null, 2)"
else
    PROFILING_SCRIPT="
var dbs = db.adminCommand({ listDatabases: 1 }).databases || [];
var out = [];
dbs.forEach(function (d) {
    out.push({ db: d.name, status: db.getSiblingDB(d.name).getProfilingStatus() });
});
JSON.stringify(out, null, 2)
"
fi

check_mongo_auth

echo "=== MongoDB Profiling Status ==="
echo "MongoDB shell: $MONGO_BIN"
echo "Endpoint: $HOST:$PORT"
echo "Database: ${DATABASE:-<all>}"
echo ""

echo "********* db.getProfilingStatus() *********"
echo ""
mongo_shell "$PROFILING_SCRIPT" 2>&1 ||
    echo "Could not retrieve profiling status."

echo ""
echo "=== Done ==="
