#!/usr/bin/env bash

# ---
# title: MongoDB PBM Backup Diagnostics
# description: Collects Percona Backup for MongoDB (PBM) diagnostics from a node — pbm configuration, backup list, event logs, status, and the pbm-agent service unit. Run it on every node involved in the backup and/or restore. Optionally appends the tail of the local MongoDB server log for troubleshooting a node after a restore.
# allow_extra_args: false
# sudo: always
# diagnostic_categories:
#  - BACKUP_PBM
# service_type: mongodb
# parameters:
#  - name: mongodb-uri
#    type: str
#    label: PBM MongoDB URI
#    description: MongoDB connection string PBM uses to reach this node. Auto-detected from the pbm-agent service environment when left empty. It carries credentials, so the password is redacted from this snippet's output.
#    placeholder: mongodb://USER:PASSWORD@localhost:27017/?replicaSet=rs0&authSource=admin
#  - name: log-entries
#    type: int
#    label: PBM log entries
#    description: How many of the most recent PBM event-log entries to collect.
#    default: 10000
#    ge: 1
#    le: 1000000
#  - name: journal-lines
#    type: int
#    label: pbm-agent journal lines
#    description: Trailing lines of the pbm-agent systemd journal to collect. Independent of "PBM log entries", which controls the PBM event log.
#    default: 2000
#    ge: 1
#    le: 1000000
#  - name: include-mongo-logs
#    type: bool
#    label: Include MongoDB server log
#    description: Append the tail of the local MongoDB server log. Enable this when the issue appears after restoring this node.
#  - name: mongo-log-lines
#    type: int
#    label: MongoDB log lines
#    description: Trailing lines of the MongoDB server log to include when "Include MongoDB server log" is enabled.
#    default: 2000
#    ge: 1
#    le: 1000000
#  - name: mongo-log-file
#    type: str
#    label: MongoDB log file path
#    description: Path to the MongoDB server log. Auto-detected from the running mongod process or its config when left empty.
#    placeholder: /var/log/mongodb/mongod.log
# alerts:
#   - MongoDBInstanceNotAvailable
# ---

# mongodb_pbm_diagnostics.sh
#
# Gathers the standard Percona Backup for MongoDB (PBM) troubleshooting set
# from the node it runs on:
#   - pbm version
#   - pbm config --list
#   - pbm list
#   - pbm logs --tail=<N>
#   - pbm status
#   - systemctl cat / status pbm-agent and recent pbm-agent journal
#   - (optional) tail of the local MongoDB server log, for post-restore issues
#
# Run it once on every node taking part in the backup and/or restore.
#
# The PBM MongoDB URI is passed to pbm through the PBM_MONGODB_URI environment
# variable rather than the --mongodb-uri flag, so the embedded password is not
# exposed in the process list. Any URI this snippet prints has its password
# redacted.
#
# Usage:
#   ./mongodb_pbm_diagnostics.sh [--mongodb-uri <uri>] [--log-entries <N>] \
#       [--journal-lines <N>] [--include-mongo-logs] [--mongo-log-lines <N>] \
#       [--mongo-log-file <path>]

set -euo pipefail

MONGODB_URI=""
LOG_ENTRIES=10000
JOURNAL_LINES=2000
INCLUDE_MONGO_LOGS=0
MONGO_LOG_LINES=2000
MONGO_LOG_FILE=""

usage() {
    local -i exit_code="${1:-0}"
    cat << EOS
Usage: $(basename "$0") [OPTIONS]

Collect Percona Backup for MongoDB (PBM) diagnostics from this node.

Options:
  --mongodb-uri <uri>      MongoDB connection string PBM uses. Auto-detected
                           from the pbm-agent service environment when omitted.
  --log-entries <N>        Recent PBM event-log entries to collect (default: 10000).
  --journal-lines <N>      Trailing pbm-agent journal lines to collect (default: 2000).
  --include-mongo-logs     Append the tail of the local MongoDB server log.
  --mongo-log-lines <N>    Trailing MongoDB log lines to include (default: 2000).
  --mongo-log-file <path>  MongoDB server log path. Auto-detected when omitted.
  -h, --help               Show this help message.
EOS
    exit "${exit_code}"
}

if ! OPTS=$(getopt --options h --longoptions 'mongodb-uri:,log-entries:,journal-lines:,include-mongo-logs,mongo-log-lines:,mongo-log-file:,help' -- "$@"); then
    echo "Error parsing options" >&2
    usage 1
fi

eval set -- "$OPTS"

while [[ -n $* ]]; do
    case "$1" in
        --mongodb-uri)
            MONGODB_URI="$2"
            shift 2
            ;;
        --log-entries)
            LOG_ENTRIES="$2"
            shift 2
            ;;
        --journal-lines)
            JOURNAL_LINES="$2"
            shift 2
            ;;
        --include-mongo-logs)
            INCLUDE_MONGO_LOGS=1
            shift
            ;;
        --mongo-log-lines)
            MONGO_LOG_LINES="$2"
            shift 2
            ;;
        --mongo-log-file)
            MONGO_LOG_FILE="$2"
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

if ! [[ $LOG_ENTRIES =~ ^[0-9]+$ ]] || [[ $LOG_ENTRIES -le 0 ]]; then
    echo "Error: --log-entries must be a positive integer." >&2
    usage 1
fi

if ! [[ $JOURNAL_LINES =~ ^[0-9]+$ ]] || [[ $JOURNAL_LINES -le 0 ]]; then
    echo "Error: --journal-lines must be a positive integer." >&2
    usage 1
fi

if ! [[ $MONGO_LOG_LINES =~ ^[0-9]+$ ]] || [[ $MONGO_LOG_LINES -le 0 ]]; then
    echo "Error: --mongo-log-lines must be a positive integer." >&2
    usage 1
fi

section() {
    echo
    echo "********* $1 *********"
    echo
}

# Mask the password embedded in any scheme://user:password@host URI so it never
# reaches this snippet's output (task logs, downloaded artifacts).
redact_uri() {
    sed -E 's~(://[^:@/[:space:]]+:)[^@[:space:]]+@~\1REDACTED@~g'
}

# Read PBM_MONGODB_URI from a systemd EnvironmentFile (for example
# /etc/default/pbm-agent or /etc/sysconfig/pbm-agent).
read_uri_from_env_file() {
    local file="$1" line
    [[ -r $file ]] || return 1
    line=$(grep -E '^[[:space:]]*PBM_MONGODB_URI[[:space:]]*=' "$file" 2> /dev/null | tail -1 || true)
    [[ -n $line ]] || return 1
    line=${line#*=}
    line=${line#\"}
    line=${line%\"}
    line=${line#\'}
    line=${line%\'}
    [[ -n $line ]] || return 1
    printf '%s' "$line"
}

# Detect the MongoDB server log path from the running mongod process or config,
# mirroring the discovery chain used by mongodb_log_extractor.sh.
detect_mongo_log() {
    local mongod_cmd="" mongod_pid config_file="" detected_log=""
    mongod_pid=$(pgrep -x mongod 2> /dev/null | head -1 || true)
    if [[ -n $mongod_pid ]]; then
        mongod_cmd=$(ps -p "$mongod_pid" -o args= 2> /dev/null || true)
    fi
    if [[ $mongod_cmd =~ --logpath[[:space:]=]+([^[:space:]]+) ]]; then
        printf '%s' "${BASH_REMATCH[1]}"
        return 0
    fi
    if [[ $mongod_cmd =~ --config[[:space:]=]+([^[:space:]]+) ]]; then
        config_file="${BASH_REMATCH[1]}"
    elif [[ $mongod_cmd =~ -f[[:space:]]+([^[:space:]]+) ]]; then
        config_file="${BASH_REMATCH[1]}"
    fi
    if [[ -z $config_file ]]; then
        for conf in /etc/mongod.conf /etc/mongodb.conf /usr/local/etc/mongod.conf; do
            if [[ -f $conf ]]; then
                config_file="$conf"
                break
            fi
        done
    fi
    if [[ -n $config_file && -f $config_file ]]; then
        detected_log=$(awk '
            /^[[:space:]]*systemLog[[:space:]]*:[[:space:]]*$/ { in_section = 1; next }
            in_section && /^[^[:space:]#]/ { in_section = 0 }
            in_section && /^[[:space:]]+path[[:space:]]*:/ {
                sub(/^[[:space:]]*path[[:space:]]*:[[:space:]]*/, "")
                print
                exit
            }
        ' "$config_file" 2> /dev/null | tr -d "\"'" | xargs 2> /dev/null || true)
    fi
    printf '%s' "$detected_log"
}

echo "=== MongoDB PBM Backup Diagnostics ==="
echo "Host: $(hostname -f 2> /dev/null || hostname)"
echo "Date: $(date -u '+%Y-%m-%d %H:%M:%S %Z')"

# ----- Locate the pbm binary -----

PBM_BIN=$(command -v pbm 2> /dev/null || true)
if [[ -z $PBM_BIN ]]; then
    echo
    echo "WARNING: 'pbm' binary not found in PATH; PBM CLI sections will be skipped."
    echo "Install the percona-backup-mongodb package to enable them."
fi

# ----- Resolve the PBM MongoDB URI -----

URI_SOURCE=""
if [[ -n $MONGODB_URI ]]; then
    URI_SOURCE="--mongodb-uri argument"
elif [[ -n ${PBM_MONGODB_URI:-} ]]; then
    MONGODB_URI="$PBM_MONGODB_URI"
    URI_SOURCE="PBM_MONGODB_URI environment variable"
else
    for env_file in /etc/default/pbm-agent /etc/sysconfig/pbm-agent; do
        if detected=$(read_uri_from_env_file "$env_file"); then
            MONGODB_URI="$detected"
            URI_SOURCE="$env_file"
            break
        fi
    done
fi

if [[ -z $MONGODB_URI ]] && command -v systemctl > /dev/null 2>&1; then
    systemd_env=$(systemctl show pbm-agent --property=Environment 2> /dev/null | sed 's/^Environment=//' || true)
    if [[ $systemd_env == *PBM_MONGODB_URI=* ]]; then
        detected=${systemd_env##*PBM_MONGODB_URI=}
        detected=${detected%% *}
        if [[ -n $detected ]]; then
            MONGODB_URI="$detected"
            URI_SOURCE="pbm-agent systemd unit environment"
        fi
    fi
fi

echo
if [[ -n $MONGODB_URI ]]; then
    export PBM_MONGODB_URI="$MONGODB_URI"
    echo "PBM MongoDB URI resolved from: $URI_SOURCE"
    echo "PBM MongoDB URI: $(printf '%s' "$MONGODB_URI" | redact_uri)"
else
    echo "WARNING: Could not resolve a PBM MongoDB URI."
    echo "Pass it explicitly with --mongodb-uri; PBM commands that need it are skipped."
fi

# ----- PBM CLI captures -----

# Run a pbm subcommand, redacting any URI password from its output. pbm reads
# the connection string from the exported PBM_MONGODB_URI variable.
run_pbm() {
    local label="$1"
    shift
    section "$label"
    if [[ -z $PBM_BIN ]]; then
        echo "Skipped: 'pbm' binary not available on this node."
        return
    fi
    if [[ -z ${PBM_MONGODB_URI:-} ]]; then
        echo "Skipped: no PBM MongoDB URI resolved."
        return
    fi
    # A non-zero pbm exit is reported with an explicit marker so a failure is
    # not mistaken for a successful (but redacted) response in the artifact
    # log — matching the inline "pbm version" block below.
    if ! "$PBM_BIN" "$@" 2>&1 | redact_uri; then
        echo "(pbm $* failed)"
    fi
}

if [[ -n $PBM_BIN ]]; then
    section "pbm version"
    "$PBM_BIN" version 2>&1 || echo "(pbm version failed)"
fi

run_pbm "pbm config --list" config --list
run_pbm "pbm list" list
run_pbm "pbm logs --tail=$LOG_ENTRIES" logs "--tail=$LOG_ENTRIES"
run_pbm "pbm status" status

# ----- pbm-agent service unit and logs -----

section "pbm-agent service unit (systemctl cat pbm-agent)"
if command -v systemctl > /dev/null 2>&1; then
    systemctl cat pbm-agent 2>&1 | redact_uri ||
        echo "(systemctl cat pbm-agent failed — the pbm-agent service may not be installed on this node)"
else
    echo "systemctl is not available on this host."
fi

section "pbm-agent service status"
if command -v systemctl > /dev/null 2>&1; then
    systemctl status pbm-agent --no-pager --full 2>&1 | redact_uri || true
else
    echo "systemctl is not available on this host."
fi

section "pbm-agent journal (last $JOURNAL_LINES lines)"
if command -v journalctl > /dev/null 2>&1; then
    journalctl -u pbm-agent --no-pager -n "$JOURNAL_LINES" 2>&1 | redact_uri || true
else
    echo "journalctl is not available on this host."
fi

# ----- Optional MongoDB server log (post-restore troubleshooting) -----

if [[ $INCLUDE_MONGO_LOGS -eq 1 ]]; then
    section "MongoDB server log (last $MONGO_LOG_LINES lines)"
    if [[ -z $MONGO_LOG_FILE ]]; then
        MONGO_LOG_FILE=$(detect_mongo_log)
    fi
    MONGO_LOG_FILE="${MONGO_LOG_FILE:-/var/log/mongodb/mongod.log}"
    if [[ -r $MONGO_LOG_FILE ]]; then
        echo "Log file: $MONGO_LOG_FILE"
        echo
        tail -n "$MONGO_LOG_LINES" "$MONGO_LOG_FILE" | redact_uri
    elif [[ -f $MONGO_LOG_FILE ]]; then
        echo "MongoDB log file '$MONGO_LOG_FILE' exists but is not readable; re-run this snippet with sudo."
    else
        echo "MongoDB log file not found at '$MONGO_LOG_FILE'."
        echo "Specify the path explicitly with --mongo-log-file."
    fi
    echo
    echo "Tip: for a precise window around the restore, use the 'MongoDB Log Extractor' snippet."
fi

echo
echo "=== Done ==="
