#!/usr/bin/env bash

# ---
# title: "MongoDB WiredTiger Dirty Ratio Check"
# description: "This script checks the WiredTiger dirty cache ratio and write throughput to diagnose cache pressure issues."
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
#   - MongoDBHighDirtyRatio
# ---

# Usage:
#   ./mongodb_wt_dirty_ratio_check.sh [--host=HOST] [--port=PORT] [--user=USER] \
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
Run the MongoDB WiredTiger Dirty Ratio Check against a MongoDB instance.

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

echo "********* WiredTiger cache and dirty ratio *********"
echo ""
mongo_shell "
var ss = db.serverStatus();
var cache = ss.wiredTiger.cache;
var total = cache['bytes currently in the cache'];
var dirty = cache['tracked dirty bytes in the cache'];
var maxBytes = cache['maximum bytes configured'];
var dirtyPct = 'N/A';
if (typeof total === 'number' && typeof dirty === 'number' && total > 0) {
    dirtyPct = (dirty / total * 100).toFixed(2);
}
print('cache bytes in use:    ' + total + ' (' + (total / 1024 / 1024 / 1024).toFixed(2) + ' GB)');
print('cache max configured:  ' + maxBytes + ' (' + (maxBytes / 1024 / 1024 / 1024).toFixed(2) + ' GB)');
print('dirty bytes in cache:  ' + dirty + ' (' + (dirty / 1024 / 1024 / 1024).toFixed(2) + ' GB)');
print('dirty ratio:           ' + (dirtyPct === 'N/A' ? dirtyPct : dirtyPct + '%'));
print('');
print('pages read into cache: ' + cache['pages read into cache']);
print('pages written from cache: ' + cache['pages written from cache']);
" 2> /dev/null || echo "Cannot retrieve WiredTiger cache info."

echo ""
echo "********* Opcounters (write volume) *********"
echo ""
mongo_shell "
var ss = db.serverStatus();
var oc = ss.opcounters;
print('insert: ' + oc.insert);
print('update: ' + oc.update);
print('delete: ' + oc.delete);
" 2> /dev/null || true

echo ""
echo "********* Disk IO stats *********"
echo ""
if command -v iostat &> /dev/null; then
    iostat -xz 1 3 | tail -20
else
    echo "iostat not available."
fi
