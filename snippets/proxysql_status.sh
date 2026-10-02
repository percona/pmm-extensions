#!/usr/bin/env bash

# ---
# title: "ProxySQL Status"
# description: "Displays ProxySQL status information including tables and configuration files"
# allow_extra_args: false
# parameters:
#  - name: defaults-file
#    type: str
#    label: Config file
#    description: Path to ProxySQL admin config file
#    default: /etc/proxysql-admin.cnf
#  - name: files
#    type: bool
#    label: Show files
#    description: Display contents of proxysql-admin related files
#    default: false
#  - name: main
#    type: bool
#    label: Main tables
#    description: Display main tables (both on-disk and runtime)
#    default: false
#  - name: monitor
#    type: bool
#    label: Monitor tables
#    description: Display monitor tables
#    default: false
#  - name: runtime
#    type: bool
#    label: Runtime data
#    description: Restrict the main-table dump to the runtime_* tables.
#    default: false
#  - name: stats
#    type: bool
#    label: Stats tables
#    description: Display stats tables
#    default: false
#  - name: output
#    type: str
#    description: Where to send the output
#    label: Output destination
#    default: stdout
#    choices:
#      - value: stdout
#        label: Print to the terminal
#      - value: file
#        label: Write the output to a file named by the timestamp
# diagnostic_categories:
#  - SERVER_CRASHED_RESTART_NOT_SUCCESSFUL
#  - NOT_RESPONDING
#  - PERFORMANCE_OTHER
# service_type: proxysql
# alerts:
#   - name: ProxySQLNotRunning
#     service_type: mysql
#   - name: MySQLTooManyConnections
#     service_type: mysql
# ---

declare DEFAULTS_FILE="/etc/proxysql-admin.cnf"
declare USER=""
declare PASSWORD=""
declare HOST=""
declare PORT=""
declare RUNTIME_OPTION=""
declare DUMP_ALL=1
declare DUMP_MAIN=0
declare DUMP_STATS=0
declare DUMP_MONITOR=0
declare DUMP_FILES=0
declare TABLE_FILTER=""
declare OUTPUT_MODE="stdout"
declare OUTPUT_FILE=""
declare ERR_FILE=""

function usage() {
    cat << EOF
Usage: $0 [OPTIONS]

Options:
  --defaults-file <file> Config file (default: /etc/proxysql-admin.cnf)
  --files                Show config files
  --main                 Show main tables
  --monitor              Show monitor tables
  --runtime              Show runtime data
  --stats                Show stats tables
  --table <name>         Filter by table name
  --output <file|stdout> Output destination (default: stdout)
  --help                 Show this message
EOF
    exit 1
}

#
# Runs an SQL query, leaving both of mysql's streams to the caller
#
# Globals:
#   USER
#   PASSWORD
#   HOST
#   PORT
#
# Arguments:
#   1: arguments to be passed to mysql
#   2: the query
#
function mysql_client() {
    local args=$1
    local query=$2
    # Credentials go in on stdin so they never show up in the process list.
    # shellcheck disable=SC2086
    printf "[client]\nuser=%s\npassword=\"%s\"\nhost=%s\nport=%s" "${USER}" "${PASSWORD}" "${HOST}" "${PORT}" |
        mysql --defaults-file=/dev/stdin --protocol=tcp \
            ${args} -e "${query}"
}

#
# Prints stdin without the client's mylogin.cnf notices
#
function drop_mylogin_notice() {
    # grep -v exits 1 when it drops every line, which is still a success here.
    grep -v "mylogin.cnf" || true
}

#
# Prints the captured client output followed by a newline, or nothing if empty
#
# Arguments:
#   1: the captured output
#
function print_output() {
    if [[ -n $1 ]]; then
        printf '%s\n' "$1"
    fi
}

#
# Executes an SQL query and prints its output merged with mysql's stderr
#
# Arguments:
#   1: arguments to be passed to mysql
#   2: the query
#
function mysql_exec() {
    local args=$1
    local query=$2
    local retvalue
    local retoutput
    # Filter the mylogin.cnf notice only after capturing mysql's own status:
    # grep -v exits 1 when it emits nothing, which a successful empty result set
    # also produces, so folding it into the same pipeline reports failure for a
    # query that worked.
    retoutput=$(mysql_client "${args}" "${query}" 2>&1)
    retvalue=$?
    retoutput=$(printf "%s" "${retoutput}" | drop_mylogin_notice)

    print_output "${retoutput}"
    return $retvalue
}

#
# Executes an SQL query whose output is parsed, printing mysql's stderr only
# when the query fails
#
# The client can write a notice to stderr on a query that succeeds, and a
# merged stream would turn it into table names or a corrupt path.
#
# Globals:
#   ERR_FILE
# Arguments:
#   1: arguments to be passed to mysql
#   2: the query
#
function mysql_stdout() {
    local args=$1
    local query=$2
    local retvalue
    local retoutput
    local reterror

    # Without a place to park stderr, a merged value beats losing the error text.
    if [[ -z $ERR_FILE ]]; then
        mysql_exec "${args}" "${query}"
        return
    fi
    retoutput=$(mysql_client "${args}" "${query}" 2> "${ERR_FILE}")
    retvalue=$?
    if ((retvalue != 0)); then
        reterror=$(drop_mylogin_notice < "${ERR_FILE}")
        if [[ -n $retoutput && -n $reterror ]]; then
            retoutput+=$'\n'
        fi
        retoutput+=$reterror
    fi

    print_output "${retoutput}"
    return $retvalue
}

function parse_args() {
    local go_out=""

    # TODO: kennt, what happens if we don't have a functional getopt()?
    # Check if we have a functional getopt(1)
    if ! getopt --test; then
        if ! go_out="$(getopt --options=h --longoptions=defaults-file:,runtime,main,stats,monitor,files,table:,output:,help \
            --name="$(basename "$0")" -- "$@")"; then
            # no place to send output
            echo "Script error: getopt() failed" >&2
            exit 1
        fi
        eval set -- "$go_out"
    fi

    for arg; do
        case "$arg" in
            --)
                shift
                break
                ;;
            --defaults-file)
                if [[ -z $2 || $2 == --* ]]; then
                    echo "Error: --defaults-file requires an argument."
                    usage
                fi
                DEFAULTS_FILE=$2
                shift 2
                ;;
            --runtime)
                shift
                RUNTIME_OPTION=" LIKE 'runtime_%'"
                DUMP_ALL=0
                DUMP_MAIN=1
                ;;
            --main)
                shift
                DUMP_ALL=0
                DUMP_MAIN=1
                ;;
            --stats)
                shift
                DUMP_ALL=0
                DUMP_STATS=1
                ;;
            --monitor)
                shift
                DUMP_ALL=0
                DUMP_MONITOR=1
                ;;
            --files)
                shift
                DUMP_ALL=0
                DUMP_FILES=1
                ;;
            --table)
                if [[ -z $2 || $2 == --* ]]; then
                    echo "Error: --table requires an argument."
                    usage
                fi
                TABLE_FILTER=$2
                shift 2
                ;;
            --output)
                if [[ -z $2 || $2 == --* ]]; then
                    echo "Error: --output requires an argument (file|stdout)."
                    usage
                fi
                OUTPUT_MODE=$2
                shift 2
                ;;
            -h | --help)
                usage
                ;;
        esac
    done

    if [[ ! -r $DEFAULTS_FILE ]]; then
        echo "Cannot find or read the config file (check --defaults-file): $DEFAULTS_FILE."
        exit 1
    fi

    # Load credentials from config file
    # shellcheck disable=SC1090
    source "$DEFAULTS_FILE"
    USER=${PROXYSQL_USERNAME:-}
    PASSWORD=${PROXYSQL_PASSWORD:-}
    HOST=${PROXYSQL_HOSTNAME:-127.0.0.1}
    PORT=${PROXYSQL_PORT:-6032}

    # Validate output mode
    if [[ $OUTPUT_MODE != "stdout" && $OUTPUT_MODE != "file" ]]; then
        echo "Error: --output must be either 'stdout' or 'file'."
        usage
    fi

    # If output is file, create filename with timestamp
    if [[ $OUTPUT_MODE == "file" ]]; then
        OUTPUT_FILE="proxysql_status_$(date +%s).log"
    fi
}

parse_args "$@"

# One file serves every parsed query, and the EXIT trap removes it even when a
# timeout kills the run mid-query. GNU rm reports an empty operand even with -f.
trap '[[ -z $ERR_FILE ]] || rm -f "${ERR_FILE}"' EXIT
ERR_FILE=$(mktemp 2> /dev/null) || ERR_FILE=""

# Function to execute the main script logic
function run_dumps() {
    if [[ $DUMP_ALL -eq 1 || $DUMP_MAIN -eq 1 ]]; then
        echo "............ DUMPING MAIN DATABASE ............"
        if ! TABLES=$(mysql_stdout -BN "SHOW TABLES $RUNTIME_OPTION"); then
            echo "Could not list the main tables from the ProxySQL admin interface (check --defaults-file): $TABLES"
            TABLES=""
        fi
        for table in $TABLES; do
            if [[ -n $TABLE_FILTER && $table != *${TABLE_FILTER}* ]]; then
                continue
            fi
            echo "***** DUMPING $table *****"
            if ! table_dump=$(mysql_exec -t "SELECT * FROM $table"); then
                echo "Could not dump $table (check --defaults-file): $table_dump"
            else
                printf '%s\n' "$table_dump"
            fi
            echo "***** END OF DUMPING $table *****"
            echo ""
        done
        echo "............ END OF DUMPING MAIN DATABASE ............"
        echo ""
    fi

    if [[ $DUMP_ALL -eq 1 || $DUMP_STATS -eq 1 ]]; then
        echo "............ DUMPING STATS DATABASE ............"
        if ! TABLES=$(mysql_stdout -BN "SHOW TABLES FROM stats"); then
            echo "Could not list the stats tables from the ProxySQL admin interface (check --defaults-file): $TABLES"
            TABLES=""
        fi
        for table in $TABLES; do
            if [[ -n $TABLE_FILTER && $table != *${TABLE_FILTER}* ]]; then
                continue
            fi
            echo "***** DUMPING stats.$table *****"
            if ! table_dump=$(mysql_exec "-t --database=stats" "SELECT * FROM $table"); then
                echo "Could not dump stats.$table (check --defaults-file): $table_dump"
            else
                printf '%s\n' "$table_dump"
            fi
            echo "***** END OF DUMPING stats.$table *****"
            echo ""
        done
        echo "............ END OF DUMPING STATS DATABASE ............"
        echo ""
    fi

    if [[ $DUMP_ALL -eq 1 || $DUMP_MONITOR -eq 1 ]]; then
        echo "............ DUMPING MONITOR DATABASE ............"
        if ! TABLES=$(mysql_stdout -BN "SHOW TABLES FROM monitor"); then
            echo "Could not list the monitor tables from the ProxySQL admin interface (check --defaults-file): $TABLES"
            TABLES=""
        fi
        for table in $TABLES; do
            if [[ -n $TABLE_FILTER && $table != *${TABLE_FILTER}* ]]; then
                continue
            fi
            echo "***** DUMPING monitor.$table *****"
            if ! table_dump=$(mysql_exec "-t --database=monitor" "SELECT * FROM $table"); then
                echo "Could not dump monitor.$table (check --defaults-file): $table_dump"
            else
                printf '%s\n' "$table_dump"
            fi
            echo "***** END OF DUMPING monitor.$table *****"
            echo ""
        done
        echo "............ END OF DUMPING MONITOR DATABASE ............"
        echo ""
    fi

    if [[ $DUMP_ALL -eq 1 || $DUMP_FILES -eq 1 ]]; then
        if [[ -z $TABLE_FILTER ]]; then
            if ! DATADIR=$(mysql_stdout -BN "SELECT variable_value FROM global_variables WHERE variable_name='admin-datadir'"); then
                echo "Could not read admin-datadir from the ProxySQL admin interface (check --defaults-file): $DATADIR"
                DATADIR=""
            fi
            if [[ -z $DATADIR ]]; then
                DATADIR="/var/lib/proxysql"
            fi

            HOST_PRIORITY_CONTENT=$(cat "${DATADIR}/host_priority.conf" 2> /dev/null)
            if [[ -n $HOST_PRIORITY_CONTENT ]]; then
                echo "............ DUMPING HOST PRIORITY FILE ............"
                echo "$HOST_PRIORITY_CONTENT"
                echo "............ END OF DUMPING HOST PRIORITY FILE ............"
                echo ""
            fi

            ADMIN_CNF_CONTENT=$(cat "$DEFAULTS_FILE" 2> /dev/null)
            if [[ -n $ADMIN_CNF_CONTENT ]]; then
                echo "............ DUMPING PROXYSQL ADMIN CNF FILE ............"
                echo "$ADMIN_CNF_CONTENT"
                echo "............ END OF DUMPING PROXYSQL ADMIN CNF FILE ............"
                echo ""
            fi
        fi
    fi
}

if [[ $OUTPUT_MODE == "file" ]]; then
    run_dumps > "$OUTPUT_FILE" 2>&1
    echo "Output written to $OUTPUT_FILE"
else
    run_dumps
fi
