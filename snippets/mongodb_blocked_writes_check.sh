#!/usr/bin/env bash

# ---
# title: "MongoDB Blocked Writes Check"
# description: "Periodically samples pt-summary, MongoDB diagnostics (serverStatus, currentOp, mongostat) and OS metrics (vmstat, iostat, mpstat, sar, top) to a destination directory while writes appear blocked. Stop by creating an 'exit-percona-monitor' marker file in the destination."
# allow_extra_args: false
# sudo: optional
# parameters:
#  - name: dest
#    type: str
#    label: Destination directory
#    description: Directory where samples and pt-summary output are stored.
#    default: /tmp/mongodb-diagnostics
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
#  - name: iterations
#    type: int
#    label: Iterations
#    description: How many sample cycles to run before exiting.
#    default: 3
#    ge: 1
#    le: 1440
#  - name: sleep
#    type: int
#    label: Sleep between iterations
#    description: Seconds to sleep between sample cycles.
#    default: 30
#    ge: 1
#    le: 3600
#  - name: retention-days
#    type: int
#    label: Sample retention (days)
#    description: Files in the destination older than this are purged at the end of each cycle.
#    default: 3
#    ge: 1
#    le: 365
# diagnostic_categories:
#  - WRITES_ARE_BLOCKED
#  - NOT_RESPONDING
#  - OVERALL_SLOWNESS
#  - TEMPORARY_STALLS
# service_type: mongodb
# alerts:
#   - MongoDBHighWriteConflict
#   - MongoDBHighFlowControl
# ---

# Usage:
#   ./mongodb_blocked_writes_check.sh [--dest=DIR] [--port=PORT] [--user=USER] \
#       [--password=PASS] [--auth-database=DB] [--iterations=N] [--sleep=SECS] \
#       [--retention-days=DAYS]
#
# Stop early by creating the marker file in the destination directory:
#   touch /tmp/mongodb-diagnostics/exit-percona-monitor
#
# The password never appears on the command line of mongosh, mongo or mongostat:
# the shells read it from stdin and mongostat from a --config file descriptor,
# with the user name as -u. A --password given to this script is still visible
# in this script's own argv.

set -euo pipefail

DEST="/tmp/mongodb-diagnostics"
PORT=27017
USER=""
PASSWORD=""
AUTH_DB="admin"
ITERATIONS=3
SLEEP_SECS=30
RETENTION_DAYS=3

usage() {
    local -i exit_code="${1:-0}"
    cat << EOS
Usage: $(basename "$0") [OPTIONS]
Sample MongoDB and OS diagnostics into a destination directory.

Command line options:

   --dest               Destination directory (default: /tmp/mongodb-diagnostics)
   --port               MongoDB port (default: 27017)
   --user               MongoDB user (provide together with --password)
   --password           MongoDB password (provide together with --user)
   --auth-database      Authentication database (default: admin)
   --iterations         Number of sample cycles (default: 3)
   --sleep              Seconds to sleep between cycles (default: 30)
   --retention-days     Purge samples older than this at each cycle (default: 3)
   -h, --help           Show this help message

Stop early by creating the marker file in the destination directory:
   touch <dest>/exit-percona-monitor
EOS
    exit "${exit_code}"
}

if ! OPTS=$(getopt --options h --longoptions 'dest:,port:,user:,password:,auth-database:,iterations:,sleep:,retention-days:,help' -- "$@"); then
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
        --iterations)
            ITERATIONS="$2"
            shift 2
            ;;
        --sleep)
            SLEEP_SECS="$2"
            shift 2
            ;;
        --retention-days)
            RETENTION_DAYS="$2"
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
MONGOSTAT_ARGS=(--port "$PORT")

# Match C0 controls and DEL, C1 controls (U+0080-U+009F) and the line and
# paragraph separators (U+2028, U+2029) byte by byte, whatever the locale.
password_has_control_character() {
    local LC_ALL=C
    [[ $PASSWORD == *[[:cntrl:]]* ||
        $PASSWORD == *$'\xc2'[$'\x80'-$'\x9f']* ||
        $PASSWORD == *$'\xe2\x80'[$'\xa8\xa9']* ]]
}

if [ -n "$USER" ]; then
    MONGOSTAT_ARGS+=(-u "$USER" --authenticationDatabase "$AUTH_DB")
    # The tools' driver SASLpreps the password under the default mechanism even
    # before negotiating one, and SASLprep rejects every control character; only
    # a SCRAM-SHA-1 credential can hold one.
    if password_has_control_character; then
        MONGOSTAT_ARGS+=(--authenticationMechanism SCRAM-SHA-1)
    fi
fi

# Print a mongostat --config file holding the password as a YAML double-quoted
# scalar, so the password reaches mongostat whole without touching argv or disk.
# Every code point is decoded from the UTF-8 bytes and written as an escape, so
# the file is plain ASCII whatever the locale.
mongostat_config() {
    local -a bytes
    local escaped="" escape code extra i j
    read -r -a bytes < <(printf '%s' "$PASSWORD" | od -An -tu1 -v | tr '\n' ' ') || true
    for ((i = 0; i < ${#bytes[@]}; i++)); do
        code=${bytes[i]}
        extra=0
        if ((code >= 0xf0)); then
            code=$((code & 0x07))
            extra=3
        elif ((code >= 0xe0)); then
            code=$((code & 0x0f))
            extra=2
        elif ((code >= 0xc0)); then
            code=$((code & 0x1f))
            extra=1
        fi
        for ((j = 0; j < extra && i + 1 < ${#bytes[@]}; j++)); do
            i=$((i + 1))
            code=$(((code << 6) | (bytes[i] & 0x3f)))
        done
        if ((code < 0x100)); then
            printf -v escape '\\x%02x' "$code"
        elif ((code < 0x10000)); then
            printf -v escape '\\u%04x' "$code"
        else
            printf -v escape '\\U%08x' "$code"
        fi
        escaped+=$escape
    done
    printf 'password: "%s"\n' "$escaped"
}

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

mongo_eval() {
    local script="$1"
    local outfile="$2"
    if [ -z "$MONGO_BIN" ]; then
        echo "Neither mongosh nor mongo is installed; skipping: $outfile" > "$outfile"
        return
    fi
    mongo_shell "$script" > "$outfile" 2>&1 || true
}

run_if_available() {
    local cmd="$1"
    local outfile="$2"
    shift 2
    if command -v "$cmd" > /dev/null 2>&1; then
        "$cmd" "$@" > "$outfile" 2>&1 || true
    else
        echo "$cmd is not installed; skipping" > "$outfile"
    fi
}

check_mongo_auth

echo "Destination: $DEST"
echo "MongoDB shell: ${MONGO_BIN:-<none>}"
echo "Iterations: $ITERATIONS  Sleep: ${SLEEP_SECS}s  Retention: ${RETENTION_DAYS}d"
echo "Stop early: touch $DEST/exit-percona-monitor"
echo ""

# ----- One-shot captures -----

if command -v pt-summary > /dev/null 2>&1; then
    pt-summary > "$DEST/pt-summary.out" 2>&1 || true
else
    echo "pt-summary is not installed; install percona-toolkit to enable this capture" \
        > "$DEST/pt-summary.out"
fi

mongo_eval "JSON.stringify(db.adminCommand({ getParameter: '*' }), null, 2)" \
    "$DEST/getParameter.out"
mongo_eval "JSON.stringify(db.adminCommand({ getCmdLineOpts: 1 }), null, 2)" \
    "$DEST/getCmdLineOpts.out"

run_if_available dmesg "$DEST/dmesg"
run_if_available dmesg "$DEST/dmesg_t" -T
run_if_available journalctl "$DEST/journalctl.out" -a --no-pager
run_if_available sysctl "$DEST/sysctl.out" -a

# ----- Sampling loop -----

for ((i = 1; i <= ITERATIONS; i++)); do
    if [ -f "$DEST/exit-percona-monitor" ]; then
        echo "Stop marker found at $DEST/exit-percona-monitor; exiting loop."
        break
    fi

    d=$(date +%F_%H-%M-%S)
    echo "[$d] iteration $i/$ITERATIONS"

    run_if_available netstat "$DEST/${d}-netstat_s" -s &
    (
        if command -v ps > /dev/null 2>&1; then
            ps faux > "$DEST/${d}-ps" 2>&1 || true

            if [ -n "$PASSWORD" ] && command -v sed > /dev/null 2>&1; then
                sed -i -E \
                    -e 's/(-p)[[:space:]]+[^[:space:]]+/\1 [REDACTED]/g' \
                    -e 's/(--password)=[^[:space:]]+/\1=[REDACTED]/g' \
                    -e 's/(--password)[[:space:]]+[^[:space:]]+/\1 [REDACTED]/g' \
                    "$DEST/${d}-ps" || true
            fi
        else
            echo "ps is not installed; skipping" > "$DEST/${d}-ps"
        fi
    ) &
    run_if_available pidstat "$DEST/${d}-pidstat_d" -d 1 60 &
    run_if_available pidstat "$DEST/${d}-pidstat_u" -u 1 60 &
    run_if_available top "$DEST/${d}-top" -bn1 &
    run_if_available vmstat "$DEST/${d}-vmstat" 1 10 &
    run_if_available iostat "$DEST/${d}-iostat" -dx 1 10 &
    run_if_available mpstat "$DEST/${d}-mpstat" -P ALL 1 10 &
    run_if_available sar "$DEST/${d}-sar_dev" -n DEV 1 10 &
    run_if_available sar "$DEST/${d}-sar_tcp" -n TCP,ETCP 1 10 &

    if command -v mongostat > /dev/null 2>&1; then
        (
            if [ -n "$USER" ]; then
                mongostat "${MONGOSTAT_ARGS[@]}" --config=<(mongostat_config) --rowcount=1 \
                    > "$DEST/${d}-mongostat" 2>&1
            else
                mongostat "${MONGOSTAT_ARGS[@]}" --rowcount=1 > "$DEST/${d}-mongostat" 2>&1
            fi
        ) || true &
    else
        echo "mongostat not installed" > "$DEST/${d}-mongostat" &
    fi

    mongo_eval "JSON.stringify(db.adminCommand({ currentOp: true }), null, 2)" \
        "$DEST/${d}-currentOp.out" &

    (
        for _ in $(seq 1 10); do
            mongo_eval "JSON.stringify(db.serverStatus(), null, 2)" \
                /dev/stdout >> "$DEST/${d}-mongo-serverStatus" || true
            sleep 1
        done
    ) &

    wait || true

    find "$DEST" -mtime "+${RETENTION_DAYS}" -type f \
        ! -name 'purge.log' ! -name 'exit-percona-monitor' \
        -delete -print >> "$DEST/purge.log" 2>&1 || true

    if [ "$i" -lt "$ITERATIONS" ]; then
        sleep "$SLEEP_SECS"
    fi
done

echo "Done. Samples are in $DEST"
