#!/usr/bin/env bash

# ---
# title: "MongoDB Replication Lag Check"
# description: "This script checks replication lag between primary and secondaries using replica set status."
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
# diagnostic_categories:
#  - REPLICA_SET_REPLICATION
# service_type: mongodb
# alerts:
#   - MongoDBReplicationLag
# ---

# Usage:
#   ./mongodb_repl_lag_check.sh [--host=HOST] [--port=PORT] [--user=USER] \
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
Run the MongoDB Replication Lag Check against a MongoDB instance.

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

check_mongo_auth

echo "********* Replica set member lag *********"
echo ""
mongo_shell "
var fmt = typeof tojson === 'function' ? tojson : JSON.stringify;
var status = rs.status();
var primary = null;
status.members.forEach(function(m) {
    if (m.stateStr === 'PRIMARY') primary = m;
});
if (primary) {
    var primaryOptimeDate = primary.optimeDate;
    var primaryOptimeStr = primaryOptimeDate ? fmt(primaryOptimeDate) : 'N/A';
    print('Primary: ' + primary.name + '  optime: ' + primaryOptimeStr);
    status.members.forEach(function(m) {
        if (m.stateStr !== 'PRIMARY') {
            var memberOptimeStr = m.optimeDate ? fmt(m.optimeDate) : 'N/A';
            var lagStr = 'N/A';
            if (primaryOptimeDate && m.optimeDate) {
                var lagMs = primaryOptimeDate - m.optimeDate;
                if (!isNaN(lagMs)) {
                    lagStr = (lagMs / 1000) + 's';
                }
            }
            print('  ' + m.name + '  state: ' + m.stateStr + '  lag: ' + lagStr + '  optime: ' + memberOptimeStr);
        }
    });
} else {
    print('No PRIMARY found in replica set.');
    status.members.forEach(function(m) {
        print('  ' + m.name + '  state: ' + m.stateStr + '  health: ' + m.health);
    });
}
" 2> /dev/null || echo "Cannot retrieve replica set status."

echo ""
echo "********* Replication info (oplog) *********"
echo ""
mongo_shell "printjson(db.getReplicationInfo())" 2> /dev/null || true

echo ""
echo "********* Current operations on primary *********"
echo ""
mongo_shell "
db.currentOp({'secs_running': {\$gt: 10}}).inprog.forEach(function(op) {
    print('OpID: ' + op.opid + '  secs: ' + op.secs_running + '  NS: ' + op.ns + '  plan: ' + (op.planSummary || 'N/A'));
});
" 2> /dev/null || true

echo ""
echo "********* Disk IO on secondary *********"
echo ""
if command -v iostat &> /dev/null; then
    iostat -xz 1 3 | tail -15
else
    echo "iostat not available."
fi
