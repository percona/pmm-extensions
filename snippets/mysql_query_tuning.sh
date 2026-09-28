#!/usr/bin/env bash

# ---
# title: "mysql_query_tuning"
# description: "Collects data for MySQL query tuning."
# allow_extra_args: false
# parameters:
#  - name: defaults-file
#    type: str
#    label: MySQL defaults file
#    description: MySQL option file the client reads for connection settings.
#  - name: query
#    type: str
#    label: Query to tune
#    description: The SQL query to analyze for performance tuning
#    default: ""
#  - name: file
#    type: str
#    label: File, containing the query
#    description: Path to a file containing the SQL query to analyze for performance tuning
#    default: ""
#  - name: database
#    type: str
#    label: Database to use
#    description: Database to use for the query tuning
#    default: ""
#  - name: profile
#    type: bool
#    label: Profile the query
#    description: Run the query and collect profiling information as well. Not allowed for DML queries; a CTE is profiled only when "Force profiling" is on.
#    default: false
#  - name: force
#    type: bool
#    label: Force profiling
#    description: If enabled, the query will be profiled even if it is a CTE expression. It is your responsibility to ensure that the query only selects data and does not modify it.
#    default: false
#  - name: execute
#    type: bool
#    label: Execute diagnostic queries
#    description: If enabled, the diagnostic queries will be executed
#    default: false
#  - name: help
#    type: bool
#    label: Show help message
#    description: Print the script's usage text instead of running it.
#    default: false
# diagnostic_categories:
#  - QUERY_TUNING_OPTIMIZATION
# service_type: mysql
# alerts:
#   - MySQLSlowQueries
#   - MySQLTooManyConnections
#   - MySQLTooManyThreadsRunning
#   - MySQLHistoryListLengthHigh
# ---

# Usage: ./mysql_query_tuning.sh [--defaults-file=path] --query=query_string|--file=path [--database=name] [--dest=path] [--execute] [--help]
# Example: ./mysql_query_tuning.sh --query="SELECT first_name, last_name FROM actor" --execute --dest=/tmp

declare DEFAULTS_FILE=""
declare QUERY=""
declare FILE=""
declare DATABASE=""
declare DEST=
declare PROFILE=0
declare FORCE=0
declare EXECUTE=0

usage() {
    local -i exit_code="${1:-0}"
    cat << EOS
Usage: $(basename "${0}") [OPTIONS]
Collects data for MySQL query tuning.

This snippet runs set of MySQL queries to collect data for query tuning.
It takes a SQL query as input, either directly or from a file, and writes
diagnostic queries to a file "queries.sql" in the specified destination directory.
If option --execute is set, it will execute the diagnostic queries and write
the results to a file "results.txt". Finally, it will compress the results into a tar.gz file.

Command line options:

   --defaults-file  Path to MySQL defaults-file
   -q, --query      SQL query to analyze for performance tuning
   -f, --file       Path to a file containing the SQL query to analyze
   -D, --database   Database to use for the query tuning
   -d, --dest       Destination directory for the diagnostic queries file and results.
                    Default: $(pwd)/$(hostname)-$(date +%Y-%m-%d-%H-%M-%S)
   -p, --profile    Profile the query.
                    Default: 0
   -F, --force      Force profiling of CTE expressions.
                    Default: 0
   -e, --execute    Execute diagnostic queries.
                    Default: 0
   -h, --help       Show this help message

EOS
    exit ${exit_code}
}

compress_data() {
    tar czf "${DEST}.tar.gz" -C "$(dirname "${DEST}")" "$(basename "${DEST}")"
}

if ! OPTS=$(getopt --options -q:f:D:d:pFeh --longoptions 'defaults-file:,query:,file:,database:,dest:,profile,force,execute,help' -- "$@"); then
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
        -q | --query)
            QUERY="$2"
            shift 2
            ;;
        -f | --file)
            FILE="$2"
            shift 2
            ;;
        -D | --database)
            DATABASE="$2"
            shift 2
            ;;
        -d | --dest)
            DEST="$2"
            shift 2
            ;;
        -e | --execute)
            EXECUTE=1
            shift 1
            ;;
        -p | --profile)
            PROFILE=1
            shift 1
            ;;
        -F | --force)
            FORCE=1
            shift 1
            ;;
        -h | --help)
            usage
            ;;
        --)
            shift 1
            break
            ;;
        # Need this to catch options mess up that getopt does not recognize
        *)
            echo "Unrecognized option '$1'"
            usage 1
            ;;
    esac
done

test -n "${DEST}" || DEST="$(pwd)/$(hostname)-$(date +%Y-%m-%d-%H-%M-%S)"

if [[ -z $QUERY && -z $FILE ]] || [[ -n $QUERY && -n $FILE ]]; then
    echo "Error: Either --query or --file must be specified."
    usage 1
fi

mkdir "${DEST}"

if [[ -z $QUERY ]]; then
    if ! QUERY=$(cat "$FILE"); then
        echo "Error reading query from file (check --file): $FILE, exiting."
        exit 1
    fi
fi

# Remove trailing spaces and semicolon from $QUERY
# We do not care about comments here, so if someone passed
# a query like "SELECT * FROM table; -- comment"
# or "SELECT * FROM table; /* comment */", the trailing comment will not be removed
# As a result, EXPLAIN output will be printed in the horizontal format and other
# formatting issues may occur.
# We may fix this later, but for now we will just remove trailing spaces and semicolon
# and leave comments as they are.
QUERY="${QUERY%%[;[:space:]]}"
QUERY="${QUERY%%;}"
QUERY="${QUERY%%[[:space:]]}"

[ "$EXTENSIONS_DEBUG" ] && echo "Using query: '${QUERY}'"

# We can only define MYSQL command after we have defaults-file
MYSQL="mysql $DEFAULTS_FILE -B"

# 1. Check if the query starts with "SELECT"
# 2. Else, check if the query is DML (INSERT, UPDATE, DELETE)
IS_SELECT=0
IS_DML=0
IS_CTE=0

if echo "$QUERY" | head -n 1 | grep -qiP '^\s*(/\*.*?\*/)*\s*select(\s|(/\*.*?\*/))'; then
    IS_SELECT=1
elif echo "$QUERY" | head -n 1 | grep -qiP '^\s*(/\*.*?\*/)*\s*(insert|delete|update)(\s|(/\*.*?\*/))'; then
    IS_DML=1
elif echo "$QUERY" | head -n 1 | grep -qiP '^\s*(/\*.*?\*/)*\s*with(\s|(/\*.*?\*/))'; then
    IS_CTE=1
else
    echo "Error: The query must start with SELECT, INSERT, UPDATE, DELETE, or WITH."
    exit 1
fi

# These are time-consuming tasks, so we will implement them later
# 3. TODO: Check if we have only one query
# 4. TODO: Check if the query is valid
# 5. TODO: Collect all tables used in the query

# 6. Write safe statements to the query file
QUERY_FILE="${DEST}/queries.sql"
[ "$EXTENSIONS_DEBUG" ] && echo "Writing diagnostic queries to: ${QUERY_FILE}"
{
    echo "-- Diagnostic queries for: ${QUERY}"
    if [[ -n $DATABASE ]]; then
        echo "USE ${DATABASE};"
    fi
    echo "EXPLAIN ${QUERY}\G"
    echo "SHOW WARNINGS \G"
    echo "EXPLAIN FORMAT=JSON ${QUERY}\G"
    echo "SHOW WARNINGS \G"
} > "${QUERY_FILE}"

# TODO: Enable this when everyone is on MySQL 8.0.18+
#echo "EXPLAIN FORMAT=TREE ${QUERY}\G" >> "${QUERY_FILE}"
#echo "SHOW WARNINGS \G" >> "${QUERY_FILE}"

# 7. If query is SELECT, write unsafe statements to the query file
if [[ $PROFILE -eq 1 ]]; then
    if [[ $IS_SELECT -eq 1 ]] || [[ $IS_CTE -eq 1 && $FORCE -eq 1 ]]; then
        {
            echo "FLUSH STATUS;"
            echo "SET optimizer_trace='enabled=on';"
            echo "SET optimizer_trace_max_mem_size=1024*1024*16;"
            # We need to manipulate with the session variable profiling_history_size,
            # so that we can guess correct query number in SHOW PROFILES
            echo "SET profiling_history_size=0;"
            echo "SET profiling=1;"
            echo "SET profiling_history_size=5;"
            echo "PAGER md5sum;"
            echo "${QUERY};"
            echo "NOPAGER;"
            echo "SHOW STATUS LIKE 'Handler%';"
            echo "SELECT * FROM INFORMATION_SCHEMA.OPTIMIZER_TRACE\G"
            echo "SET optimizer_trace='enabled=off';"
            echo "SHOW PROFILES;"
            echo "SHOW PROFILE FOR QUERY 2;"
        } >> "${QUERY_FILE}"
    else
        if [[ $IS_CTE -eq 1 && $FORCE -eq 0 ]]; then
            echo "Warning: The query is a CTE expression. Profiling of CTE expressions is only allowed if option --force is set. Skipping profiling." >&2
        elif [[ $IS_DML -eq 1 ]]; then
            echo "Warning: The query is a DML statement. Profiling of DML statements is not allowed. Skipping profiling." >&2
        else
            echo "Warning: The query is not a SELECT statement. Profiling is only allowed for SELECT statements. Skipping profiling." >&2
        fi
    fi
fi

# 8. TODO: Collect table statistics and definitions

# 9. If --execute is set, execute the diagnostic queries and write results to results.txt
if [ $EXECUTE -eq 1 ]; then
    [ "$EXTENSIONS_DEBUG" ] && echo "Writing results to: ${DEST}/results.txt"
    if ! $MYSQL < "${QUERY_FILE}" > "${DEST}/results.txt" 2>&1; then
        echo "Error executing diagnostic queries, check results.txt for details."
        exit 1
    fi
fi

# 10. Compress the results into a tar.gz file
[ "$EXTENSIONS_DEBUG" ] && echo "Compressing results to: ${DEST}.tar.gz"
compress_data
