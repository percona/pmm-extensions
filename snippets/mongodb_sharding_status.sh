#!/usr/bin/env bash

# ---
# title: "MongoDB Sharding Status"
# description: "Runs sh.status() against a MongoDB mongos router to capture the sharded-cluster topology — shards, databases, sharded collections, chunk distribution and balancer state — for support diagnostics. Run it against a mongos."
# allow_extra_args: false
# sudo: optional
# parameters:
#  - name: host
#    type: str
#    label: MongoDB host
#    description: Hostname or IP of the mongos router.
#    default: localhost
#  - name: port
#    type: int
#    label: MongoDB port
#    description: TCP port of the mongos router.
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
# diagnostic_categories:
#  - OVERALL_SLOWNESS
#  - PERFORMANCE_OTHER
# service_type: mongodb
# alerts:
#   - MongoDBChunksImbalance
#   - MongoDBInstanceNotAvailable
# ---

# mongodb_sharding_status.sh
#
# Captures sh.status() from a MongoDB mongos router, matching the
# "MongoDB shards" data-collection checklist used by Percona Support.
# sh.status() reports the cluster's shards, databases, sharded collections,
# chunk distribution per shard and the balancer state.
#
# The credentials reach the MongoDB shell over stdin and never appear on its
# command line. A --password given to this script is still visible in this
# script's own argv.
#
# sh.status() is only meaningful against a mongos; run this on the router.
# Host-level diagnostics for the config server and shard primaries are
# collected separately using the appropriate replica-set diagnostic
# collection for those hosts.
#
# Usage:
#   ./mongodb_sharding_status.sh [--host=HOST] [--port=PORT] [--user=USER] \
#       [--password=PASS] [--auth-database=DB]

set -euo pipefail

HOST="localhost"
PORT=27017
USER=""
PASSWORD=""
AUTH_DB="admin"

usage() {
    local -i exit_code="${1:-0}"
    cat << EOS
Usage: $(basename "$0") [OPTIONS]
Capture sh.status() from a MongoDB mongos router.

Command line options:

   --host             mongos host (default: localhost)
   --port             mongos port (default: 27017)
   --user             MongoDB user (provide together with --password)
   --password         MongoDB password (provide together with --user)
   --auth-database    Authentication database (default: admin)
   -h, --help         Show this help message
EOS
    exit "${exit_code}"
}

if ! OPTS=$(getopt --options h --longoptions 'host:,port:,user:,password:,auth-database:,help' -- "$@"); then
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
# partial combination instead of silently connecting unauthenticated, which
# would later surface only as a generic "Could not retrieve sh.status()."
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

# The script stays on --eval and only the credentials travel over stdin: piped
# into mongosh, a script runs as a REPL that ignores a failed db.auth().
MONGO_AUTH_FAILED=3
MONGO_AUTH_JS="var __creds = typeof require === 'function' ? require('fs').readFileSync(0, 'utf8') : cat('/dev/stdin');
var __s1 = __creds.indexOf('\n'), __s2 = __creds.indexOf('\n', __s1 + 1), __ok = false;
try { __ok = db.getSiblingDB(__creds.substring(0, __s1)).auth(__creds.substring(__s1 + 1, __s2), __creds.substring(__s2 + 1).replace(/\n\$/, '')); } catch (e) { __ok = false; }
if (!__ok) { quit($MONGO_AUTH_FAILED); }"

mongo_shell() {
    local script="$1"
    if [ -z "$USER" ]; then
        "$MONGO_BIN" "${MONGO_ARGS[@]}" --quiet --eval "$script"
        return
    fi
    printf '%s\n%s\n%s\n' "$AUTH_DB" "$USER" "$PASSWORD" |
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

check_mongo_auth

echo "=== MongoDB Sharding Status ==="
echo "MongoDB shell: $MONGO_BIN"
echo "Endpoint: $HOST:$PORT"
echo ""

echo "********* Endpoint check *********"
echo ""
# A mongos router answers isMaster with msg == 'isdbgrid'.
mongo_shell "
var info = db.runCommand({ isMaster: 1 });
if (info.msg === 'isdbgrid') {
    print('Connected to a mongos router; sh.status() applies.');
} else {
    print('WARNING: this endpoint is not a mongos router.');
    print('sh.status() only applies to a sharded cluster; the output below may be an error.');
}
" 2>&1 || echo "Could not determine the endpoint role."

echo ""
echo "********* sh.status() *********"
echo ""
mongo_shell "sh.status()" 2>&1 || echo "Could not retrieve sh.status()."

echo ""
echo "=== Done ==="
