#!/usr/bin/env bash

# ---
# title: "pt-summary"
# description: "Executes pt-summary command. Extra args are passed to pt-summary after known options."
# allow_extra_args: true
# sudo: always
# parameters:
#  - name: defaults-file
#    type: str
#    label: MySQL defaults file
#    description: MySQL option file the client reads for connection settings.
#  - name: save-samples
#    type: bool
#    label: Save samples
#    description: Keep the collected sample files on disk instead of discarding them.
#  - name: help
#    type: bool
#    label: Show help message
#    description: Print the script's usage text instead of running it.
# diagnostic_categories:
#  - SERVER_CRASHED_RESTART_SUCCESSFUL
#  - OVERALL_SLOWNESS
#  - NOT_RESPONDING
#  - WRITES_ARE_BLOCKED
#  - PERFORMANCE_OTHER
#  - TEMPORARY_STALLS
# service_type: generic
# alerts:
#   - HighCPUUsage
#   - HighMemoryUsage
#   - HighIOUtilization
#   - HighDiskUsage
#   - name: MySQLInstanceNotAvailable
#     service_type: mysql
#   - name: PostgreSQLIsDown
#     service_type: postgresql
# ---

# Usage: ./pt-summary.sh [--defaults-file=path] [--dest=path] [--save-samples] [--help] [-- other args...]
# Example: ./pt-summary.sh --dest=/tmp/summary --save-samples

declare DEFAULTS_FILE=""
declare PTDEST=
declare SAVE_SAMPLES=0

usage() {
    local -i exit_code="${1:-0}"
    cat << EOS
Usage: $(basename "${0}") [OPTIONS]
Executes pt-summary script

Command line options:

   --defaults-file   Path to MySQL defaults-file
   -d, --dest        Destination for the samples.
                     Default: $(pwd)/$(hostname)-$(date +%Y-%m-%d-%H-%M-%S)
   --save-samples    Save samples
   -h, --help        Show this help message

EOS
    exit ${exit_code}
}

if ! OPTS=$(getopt --options -d:h --longoptions 'defaults-file:,dest:,save-samples,help' -- "$@"); then
    echo "Error parsing options"
    usage 1
fi

eval set -- "$OPTS"

while [[ -n $* ]]; do
    case "$1" in
        --defaults-file)
            DEFAULTS_FILE="--defaults-file=$2"
            shift 2
            ;;
        -d | --dest)
            PTDEST="$2"
            shift 2
            ;;
        --save-samples)
            SAVE_SAMPLES=1
            shift 1
            ;;
        -h | --help)
            usage
            ;;
        --)
            break
            ;;
        # Need this to catch options mess up that getopt does not recognize
        *)
            echo "Unrecognized option '$1'"
            usage 1
            ;;
    esac
done

test -n "${PTDEST}" || PTDEST="$(pwd)/$(hostname)-$(date +%Y-%m-%d-%H-%M-%S)"

if [ $SAVE_SAMPLES -eq 1 ] && [ -d "${PTDEST}" ]; then
    echo Rejecting use of "${PTDEST}"
    exit 11
fi

if [ $# -gt 1 ]; then
    echo "Starting pt-summary with extra options: $*"
fi

if [ $SAVE_SAMPLES -eq 1 ]; then
    mkdir "${PTDEST}"
    pt-summary "${DEFAULTS_FILE}" --save-samples="${PTDEST}" "$@"
    tar czf "${PTDEST}.tar.gz" -C "$(dirname "${PTDEST}")" "$(basename "${PTDEST}")"
else
    pt-summary "${DEFAULTS_FILE}" "$@"
fi
