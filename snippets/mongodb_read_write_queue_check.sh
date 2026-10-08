#!/usr/bin/env bash

# ---
# title: "MongoDB Read/Write Queue High Check"
# description: "This script checks global lock queues and identifies long-running or unindexed queries causing queue buildup."
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
# diagnostic_categories: []
# service_type: mongodb
# alerts:
#   - MongoDBReadWriteQueueHigh
# ---

# Usage:
#   ./mongodb_read_write_queue_check.sh [--host=HOST] [--port=PORT] [--user=USER] \
#       [--password=PASS] [--auth-database=DB]
#
# The credentials reach the MongoDB shell over stdin and never appear on its
# command line.

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
Run the MongoDB Read/Write Queue High Check against a MongoDB instance.

Command line options:

   --host             MongoDB host (default: localhost)
   --port             MongoDB port (default: 27017)
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
# partial combination instead of silently connecting unauthenticated.
if { [ -n "$USER" ] && [ -z "$PASSWORD" ]; } || { [ -z "$USER" ] && [ -n "$PASSWORD" ]; }; then
    echo "Error: --user and --password must be provided together, or both omitted." >&2
    usage 1
fi

MONGO_BIN="mongosh"
command -v mongosh &> /dev/null || MONGO_BIN="mongo"
MONGO_ARGS=(--host "$HOST" --port "$PORT")
MONGO_ENDPOINT="$HOST:$PORT"

# The script stays on --eval and only the credentials travel over stdin: piped
# into mongosh, a script runs as a REPL that ignores a failed db.auth().
MONGO_AUTH_FAILED=3
MONGO_AUTH_JS="var __creds = (typeof require === 'function' ? require('fs').readFileSync(0, 'utf8') : cat('/dev/stdin')).split('\n'), __ok = false;
try { __creds = __creds.slice(0, 3).map(function (h) { return decodeURIComponent(h.replace(/(..)/g, '%\$1')); }); } catch (e) { quit($MONGO_AUTH_FAILED); }
try { __ok = db.getSiblingDB(__creds[0]).auth(__creds[1], __creds[2]); } catch (e) { if (e.code !== 18) { throw e; } }
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

check_mongo_auth

echo "********* Global lock queue *********"
echo ""
mongo_shell "
var ss = db.serverStatus();
var gl = ss.globalLock;
print('currentQueue readers: ' + gl.currentQueue.readers);
print('currentQueue writers: ' + gl.currentQueue.writers);
print('currentQueue total: ' + gl.currentQueue.total);
print('activeClients readers: ' + gl.activeClients.readers);
print('activeClients writers: ' + gl.activeClients.writers);
print('activeClients total: ' + gl.activeClients.total);
" 2> /dev/null || echo "Cannot retrieve server status."

echo ""
echo "********* Long-running operations *********"
echo ""
mongo_shell "
db.currentOp({'secs_running': {\$gt: 10}}).inprog.forEach(function(op) {
    print('OpID: ' + op.opid + '  secs: ' + op.secs_running + '  NS: ' + op.ns + '  plan: ' + (op.planSummary || 'N/A') + '  Cmd: ' + JSON.stringify(op.command).substring(0, 120));
});
" 2> /dev/null || echo "Cannot check currentOp."

echo ""
echo "********* COLLSCAN queries (unindexed) *********"
echo ""
mongo_shell "
db.currentOp({'planSummary': 'COLLSCAN'}).inprog.forEach(function(op) {
    print('OpID: ' + op.opid + '  secs: ' + op.secs_running + '  NS: ' + op.ns + '  Cmd: ' + JSON.stringify(op.command).substring(0, 120));
});
" 2> /dev/null || echo "Cannot check for COLLSCAN queries."

echo ""
echo "********* Recent slow query log entries *********"
echo ""
journalctl -u mongod --no-pager -n 200 2> /dev/null | grep -i "Slow query\|COLLSCAN\|durationMillis" | tail -20 ||
    grep -i "Slow query\|COLLSCAN\|durationMillis" /var/log/mongodb/mongod.log 2> /dev/null | tail -20 ||
    echo "No slow query log entries found."
