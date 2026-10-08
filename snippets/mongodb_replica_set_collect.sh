#!/usr/bin/env bash

# ---
# title: "MongoDB Replica Set Collect"
# description: "Collects replica-set diagnostics from a MongoDB node into a destination directory and packs them into a tar.gz for support: pt-summary, getParameter, getCmdLineOpts, serverStatus, hostInfo, in-progress operations, rs.status(), rs.conf(), replication info and the latest oplog entry."
# allow_extra_args: false
# sudo: optional
# parameters:
#  - name: dest
#    type: str
#    label: Destination directory
#    description: Directory where the collected files and the resulting archive are written.
#    default: /tmp/mongodb-replicaset
#    pattern: ^/[A-Za-z0-9._/-]+$
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
#  - SERVER_CRASHED_RESTART_NOT_SUCCESSFUL
#  - PERFORMANCE_OTHER
#  - REPLICA_SET_REPLICATION
# service_type: mongodb
# alerts:
#   - MongoDBInstanceNotAvailable
#   - MongoDBReplicaState
#   - MongoDBNoPrimary
#   - MongoDBReplicationLag
#   - MongoDBOplogWindowLow
# ---

# mongodb_replica_set_collect.sh
#
# One-shot data collection for a MongoDB replica set node, matching the
# "MongoDB replica set" data-collection checklist used by Percona Support.
#
# It writes one file per command into the destination directory and then
# packs the directory into a tar.gz alongside it:
#   pt-summary.out                      -- host summary (percona-toolkit)
#   getParameter.out                    -- db.adminCommand({getParameter:'*'})
#   getCmdLineOpts.out                  -- db.adminCommand({getCmdLineOpts:1})
#   serverStatus.out                    -- db.serverStatus()
#   host_info.out                       -- db.hostInfo()
#   currentOp.out                       -- in-progress ops running > 1s
#   rs_status.out                       -- rs.status()
#   rs_conf.out                         -- rs.conf()
#   rs_printReplicationInfo.out          -- rs.printReplicationInfo()
#   rs_printSecondaryReplicationInfo.out -- rs.printSecondaryReplicationInfo()
#   oplog_last.out                       -- newest local.oplog.rs entry
#
# The credentials reach the MongoDB shell over stdin and never appear on its
# command line. A --password given to this script is still visible in this
# script's own argv. Run it on every node of the replica set.
#
# mongod logs and FTDC (diagnostic.data) are intentionally out of scope here;
# collect them with mongodb_log_extractor.sh and mongodb_ftdc_collect.sh.
#
# Usage:
#   ./mongodb_replica_set_collect.sh [--dest=DIR] [--port=PORT] [--user=USER] \
#       [--password=PASS] [--auth-database=DB]

set -euo pipefail

DEST="/tmp/mongodb-replicaset"
PORT=27017
USER=""
PASSWORD=""
AUTH_DB="admin"

usage() {
    local -i exit_code="${1:-0}"
    cat << EOS
Usage: $(basename "$0") [OPTIONS]
Collect MongoDB replica-set diagnostics into a destination directory.

Command line options:

   --dest             Destination directory (default: /tmp/mongodb-replicaset)
   --port             MongoDB port (default: 27017)
   --user             MongoDB user (provide together with --password)
   --password         MongoDB password (provide together with --user)
   --auth-database    Authentication database (default: admin)
   -h, --help         Show this help message
EOS
    exit "${exit_code}"
}

if ! OPTS=$(getopt --options h --longoptions 'dest:,port:,user:,password:,auth-database:,help' -- "$@"); then
    echo "Error parsing options" >&2
    usage 1
fi

eval set -- "$OPTS"

while [[ -n $* ]]; do
    case "$1" in
        --dest)
            DEST="$2"
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

mkdir -p "$DEST"

# Pick the MongoDB shell binary, preferring mongosh.
MONGO_BIN=""
if command -v mongosh > /dev/null 2>&1; then
    MONGO_BIN="mongosh"
elif command -v mongo > /dev/null 2>&1; then
    MONGO_BIN="mongo"
fi

MONGO_ARGS=(--port "$PORT")
MONGO_ENDPOINT="localhost:$PORT"

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

mongo_eval() {
    local script="$1"
    local outfile="$2"
    if [ -z "$MONGO_BIN" ]; then
        echo "Neither mongosh nor mongo is installed; skipping: $outfile" > "$outfile"
        return
    fi
    mongo_shell "$script" > "$outfile" 2>&1 || true
}

check_mongo_auth

echo "=== MongoDB Replica Set Collect ==="
echo "Destination: $DEST"
echo "MongoDB shell: ${MONGO_BIN:-<none>}"
echo ""

# ----- Host summary (percona-toolkit) -----
if command -v pt-summary > /dev/null 2>&1; then
    pt-summary > "$DEST/pt-summary.out" 2>&1 || true
else
    echo "pt-summary is not installed; install percona-toolkit to enable this capture" \
        > "$DEST/pt-summary.out"
fi

# ----- Instance-level diagnostics -----
mongo_eval "JSON.stringify(db.adminCommand({ getParameter: '*' }), null, 2)" \
    "$DEST/getParameter.out"
mongo_eval "JSON.stringify(db.adminCommand({ getCmdLineOpts: 1 }), null, 2)" \
    "$DEST/getCmdLineOpts.out"
mongo_eval "JSON.stringify(db.serverStatus(), null, 2)" \
    "$DEST/serverStatus.out"
mongo_eval "JSON.stringify(db.hostInfo(), null, 2)" \
    "$DEST/host_info.out"
# Filter active ops (running > 1s) client-side so the script needs no
# MongoDB query operators that would clash with shell expansion.
mongo_eval "JSON.stringify((db.currentOp(true).inprog || []).filter(function (o) { return o.active && o.secs_running > 1; }), null, 2)" \
    "$DEST/currentOp.out"

# ----- Replica-set diagnostics -----
# rs.status() / rs.conf() return errors on a standalone node; the error text is
# captured in the output files so support can confirm the topology.
mongo_eval "JSON.stringify(rs.status(), null, 2)" "$DEST/rs_status.out"
mongo_eval "JSON.stringify(rs.conf(), null, 2)" "$DEST/rs_conf.out"
mongo_eval "rs.printReplicationInfo()" "$DEST/rs_printReplicationInfo.out"

# rs.printSlaveReplicationInfo() was renamed to rs.printSecondaryReplicationInfo()
# in newer servers; prefer the modern name and fall back to the legacy one.
mongo_eval "
if (typeof rs.printSecondaryReplicationInfo === 'function') {
    rs.printSecondaryReplicationInfo();
} else {
    rs.printSlaveReplicationInfo();
}
" "$DEST/rs_printSecondaryReplicationInfo.out"

# ----- Latest oplog entry -----
mongo_eval "JSON.stringify(db.getSiblingDB('local').oplog.rs.find().sort({ ts: -1 }).limit(1).toArray(), null, 2)" \
    "$DEST/oplog_last.out"

# ----- Package the collected files -----
echo "=== Collected files ==="
ls -lh "$DEST"
echo ""

ARCHIVE="${DEST%/}.tar.gz"
tar czf "$ARCHIVE" -C "$(dirname "$DEST")" "$(basename "$DEST")"
echo "Archive: $ARCHIVE"
echo ""
echo "Run this on every node of the replica set, and attach each archive plus"
echo "the mongod logs to the support case."
echo "=== Done ==="
