#!/usr/bin/env bash

# ---
# title: "MongoDB Current Operations"
# description: "Captures db.currentOp(true) from a MongoDB instance — every in-progress operation, including idle and system operations — for support diagnostics. Can be filtered to keep only long-running operations, or to drop idle connections."
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
#  - name: min-secs
#    type: int
#    label: Minimum seconds running
#    description: Keep only operations whose secs_running is at least this value. 0 includes every operation.
#    default: 0
#    ge: 0
#    le: 86400
#  - name: active-only
#    type: bool
#    label: Active operations only
#    description: Exclude idle connections and idle system operations from the output.
#    default: false
# diagnostic_categories:
#  - OVERALL_SLOWNESS
#  - WRITES_ARE_BLOCKED
#  - TEMPORARY_STALLS
# service_type: mongodb
# alerts:
#   - MongoDBInstanceNotAvailable
#   - MongoDBHighWriteConflict
#   - MongoDBReadWriteQueueHigh
#   - MongoDBHighFlowControl
# ---

# mongodb_current_op.sh
#
# Captures db.currentOp(true) from a MongoDB instance: every in-progress
# operation, including idle connections and idle system operations. This is
# the go-to view for diagnosing long-running queries, blocked writes and
# operations stuck waiting on locks.
#
# db.currentOp(true) can return a very large list on a busy server (roughly one
# entry per connection). Narrow it with --min-secs (keep only operations
# running at least N seconds) and --active-only (drop idle operations).
#
# The credentials reach the MongoDB shell over stdin and never appear on its
# command line. A --password given to this script is still visible in this
# script's own argv.
#
# Usage:
#   ./mongodb_current_op.sh [--host=HOST] [--port=PORT] [--user=USER] \
#       [--password=PASS] [--auth-database=DB] [--min-secs=N] [--active-only]

set -euo pipefail

HOST="localhost"
PORT=27017
USER=""
PASSWORD=""
AUTH_DB="admin"
MIN_SECS=0
ACTIVE_ONLY=0

usage() {
    local -i exit_code="${1:-0}"
    cat << EOS
Usage: $(basename "$0") [OPTIONS]
Capture db.currentOp(true) from a MongoDB instance.

Command line options:

   --host             MongoDB host (default: localhost)
   --port             MongoDB port (default: 27017)
   --user             MongoDB user (provide together with --password)
   --password         MongoDB password (provide together with --user)
   --auth-database    Authentication database (default: admin)
   --min-secs         Keep only operations running at least N seconds
                      (default: 0, i.e. every operation)
   --active-only      Exclude idle connections and idle system operations
   -h, --help         Show this help message
EOS
    exit "${exit_code}"
}

if ! OPTS=$(getopt --options h --longoptions 'host:,port:,user:,password:,auth-database:,min-secs:,active-only,help' -- "$@"); then
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
        --min-secs)
            MIN_SECS="$2"
            shift 2
            ;;
        --active-only)
            ACTIVE_ONLY=1
            shift
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

if ! [[ $MIN_SECS =~ ^[0-9]+$ ]]; then
    echo "Error: --min-secs must be a non-negative integer." >&2
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

# Resolve the --active-only flag to a JavaScript boolean literal.
ACTIVE_ONLY_JS="false"
if [ "$ACTIVE_ONLY" -eq 1 ]; then
    ACTIVE_ONLY_JS="true"
fi

# db.currentOp(true) is filtered client-side. MIN_SECS is a validated integer
# and ACTIVE_ONLY_JS is a literal boolean, so both are safe to inline here.
CURRENTOP_SCRIPT="
var r = db.currentOp(true);
var ops = (r.inprog || []).filter(function (o) {
    if ($ACTIVE_ONLY_JS && !o.active) { return false; }
    if ((o.secs_running || 0) < $MIN_SECS) { return false; }
    return true;
});
JSON.stringify({ count: ops.length, inprog: ops }, null, 2)
"

check_mongo_auth

echo "=== MongoDB Current Operations ==="
echo "MongoDB shell: $MONGO_BIN"
echo "Endpoint: $HOST:$PORT"
echo "Filter: min-secs=$MIN_SECS, active-only=$ACTIVE_ONLY_JS"
echo ""

echo "********* db.currentOp(true) *********"
echo ""
mongo_shell "$CURRENTOP_SCRIPT" 2>&1 ||
    echo "Could not retrieve currentOp."

echo ""
echo "=== Done ==="
