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
declare CNF_OPEN=""

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

#
# Print what a config line leaves open for the next one: the closers of its
# unclosed quotes, innermost last; '<' once it leaves anything else open, after
# which no line is shown; or nothing
#
# Arguments:
#   1: the line
#   2: what the previous line left open
#
function cnf_open_state() {
    local line=$1
    local state=$2
    local rest="<"
    local quotes_only="^[\"'\$]*\$"
    local case_word='(^|[^A-Za-z0-9_])case([[:space:]]|$)'
    local char next top
    local escaped=-1
    local i

    # Substitutions, expansions, arrays, continuations and here-documents end
    # by rules too wide to track (a case pattern's ")" closes nothing, a
    # continuation can split an opener), so nothing after one is trusted.
    if [[ $state == "$rest" || $line =~ $case_word ]]; then
        printf '%s' "$rest"
        return
    fi

    for ((i = 0; i < ${#line}; i++)); do
        char=${line:i:1}
        next=${line:i+1:1}
        top=${state#"${state%?}"}
        if [[ $top == "'" ]]; then
            [[ $char == "'" ]] && state=${state%?}
        elif [[ $char == $'\\' ]]; then
            if ((i == ${#line} - 1)); then
                printf '%s' "$rest"
                return
            fi
            escaped=$((++i))
        elif [[ $top == '$' ]]; then
            [[ $char == "'" ]] && state=${state%?}
        elif [[ $char == '$' && $next == '(' ]]; then
            state+=")"
            ((i++))
        elif [[ $char == '$' && $next == '{' ]]; then
            state+="}"
            ((i++))
        elif [[ $char == '`' && $top == '`' ]]; then
            state=${state%?}
        elif [[ $char == '`' ]]; then
            state+='`'
        elif [[ $top == '"' ]]; then
            [[ $char == '"' ]] && state=${state%?}
        elif [[ $char == '$' && $next == "'" ]]; then
            state+='$'
            ((i++))
        elif [[ $char == "'" || $char == '"' ]]; then
            state+=$char
        elif [[ $char == '(' ]]; then
            state+=")"
        elif [[ -n $top && $char == "$top" ]]; then
            state=${state%?}
        elif [[ $char == '<' && $next == '<' ]]; then
            if [[ ${line:i+2:1} != '<' ]]; then
                printf '%s' "$rest"
                return
            fi
            ((i += 2))
        elif [[ $char == '#' ]] && { ((i == 0)) || { [[ ${line:i-1:1} == [[:space:]\;\&\|\(\)\<\>] ]] && ((escaped != i - 1)); }; }; then
            break
        fi
    done
    [[ $state =~ $quotes_only ]] || state=$rest
    printf '%s' "$state"
}

#
# Print a shell-style config file with every credential masked
#
# The file is free-form shell, so only blank lines, comments and plain
# single KEY=value assignments are shown: any other line could hold a secret
# the masking cannot find, and is replaced by a marker instead. The body runs
# in a subshell so nocasematch does not leak into the rest of the script.
#
# Arguments:
#   1: the config file
#
function print_redacted_cnf() (
    local cnf=$1
    local line
    local state=""
    local hidden="# [REDACTED: line not shown]"
    local assignment='^([[:space:]]*(export[[:space:]]+)?)([A-Za-z_][A-Za-z0-9_]*)=(.*)$'
    local single_quoted="'[^']*'"
    local double_quoted='"[^"\\$`]*"'
    local bare=$'[^[:space:]\'"\\\\$`;&|<>()]*'
    local plain_value="^(${single_quoted}|${double_quoted}|${bare})([[:space:]]+#.*)?[[:space:]]*\$"
    local empty_value="^(''|\"\")?\$"
    local secret_key='PASS|PW|SECRET|TOKEN|KEY|AUTH|CRED'
    # A password may hold "/" or a space, and a DSN such as
    # "user:pass@tcp(host)" has no scheme, so any "user:...@" counts.
    local url_credentials='[^[:space:]@:/]+:[^@]*@'
    local credential_text="(${secret_key})[A-Za-z0-9_]*(\\[[^]]*])?[[:space:]]*\\+?=|${url_credentials}"
    local comment='^([[:space:]]*#+[[:space:]]*)(.*)$'
    local comment_lead export_lead key value token trailing shown

    shopt -s nocasematch
    while IFS= read -r line || [[ -n $line ]]; do
        # A line after one left open is part of that value, however much it
        # looks like its own assignment or comment.
        if [[ -n $state ]]; then
            printf '%s\n' "$hidden"
            state=$(cnf_open_state "$line" "$state")
            continue
        fi

        comment_lead=""
        if [[ $line =~ $comment ]]; then
            comment_lead=${BASH_REMATCH[1]}
            line=${BASH_REMATCH[2]}
        fi

        if [[ -z $comment_lead && $line =~ ^[[:space:]]*$ ]]; then
            printf '%s\n' "$line"
            continue
        fi

        key=""
        if [[ $line =~ $assignment ]]; then
            export_lead=${BASH_REMATCH[1]}
            key=${BASH_REMATCH[3]}
            value=${BASH_REMATCH[4]}
        fi

        shown=$hidden
        if [[ -n $key && $value =~ $plain_value ]]; then
            token=${BASH_REMATCH[1]}
            trailing=${BASH_REMATCH[2]}
            # A credential's trailing comment is dropped, since it may note
            # the old value.
            if [[ $key =~ $secret_key || $token =~ $credential_text ]]; then
                [[ $token =~ $empty_value ]] || token="[REDACTED]"
                shown="${comment_lead}${export_lead}${key}=${token}"
            elif [[ ! $trailing =~ $credential_text ]]; then
                shown="${comment_lead}${line}"
            fi
        elif [[ -n $comment_lead ]]; then
            [[ $line =~ $credential_text ]] || shown="${comment_lead}${line}"
        else
            state=$(cnf_open_state "$line" "")
        fi
        printf '%s\n' "$shown"
    done < "$cnf"
)

#
# Print a config value as the literal string a shell assignment gives it
#
# Return 1, printing nothing, for a value that only shell evaluation could
# complete or that goes on past the end of its line.
#
# Arguments:
#   1: the text after the "="
#
function cnf_unquote() {
    local raw=$1
    local value=""
    local rest=""
    local char
    local closed=0
    local prev
    local i

    case ${raw:0:1} in
        "'")
            [[ $raw =~ ^\'([^\']*)\'(.*)$ ]] || return 1
            value=${BASH_REMATCH[1]}
            rest=${BASH_REMATCH[2]}
            ;;
        '"')
            for ((i = 1; i < ${#raw}; i++)); do
                char=${raw:i:1}
                if [[ $char == $'\\' ]]; then
                    char=${raw:i+1:1}
                    [[ $char == [\"\\\$\`] ]] || value+=$'\\'
                    value+=$char
                    ((i++))
                elif [[ $char == '"' ]]; then
                    closed=1
                    rest=${raw:i+1}
                    break
                elif [[ $char == [\$\`] ]]; then
                    return 1
                else
                    value+=$char
                fi
            done
            ((closed)) || return 1
            ;;
        *)
            # The shell expands a tilde that starts an assignment value or
            # follows an unquoted colon in it.
            prev=":"
            for ((i = 0; i < ${#raw}; i++)); do
                char=${raw:i:1}
                if [[ $char == [[:space:]] ]]; then
                    rest=${raw:i}
                    break
                elif [[ $char == $'\\' ]]; then
                    ((i == ${#raw} - 1)) && return 1
                    value+=${raw:i+1:1}
                    prev=""
                    ((i++))
                elif [[ $char == [\$\`\'\"\;\&\|\<\>\(\)] || ($char == '~' && $prev == ':') ]]; then
                    return 1
                else
                    value+=$char
                    prev=$char
                fi
            done
            ;;
    esac
    [[ $rest =~ ^([[:space:]]+#.*|[[:space:]]*)$ ]] || return 1
    printf '%s' "$value"
}

#
# Update what the config lines read so far leave open for the next one: the
# quote opened, a backslash when a line ends in a continuation, or nothing
#
# Globals:
#   CNF_OPEN
# Arguments:
#   1: the next line
#
function cnf_open_quote() {
    local line=$1
    local open=$CNF_OPEN
    local word_start=1
    local q="'"
    local in_single="^[^${q}]*${q}(.*)\$"
    local in_double='^([^"\\]*)(.*)$'
    local in_word="^([^\\\\${q}\"#]*)(.*)\$"
    local plain

    if [[ $open == $'\\' ]]; then
        open=""
        word_start=0
    fi
    # Jumps from one quoting character to the next: a character-by-character
    # scan takes quadratic time on a long line.
    while [[ -n $line ]]; do
        case $open in
            "'")
                [[ $line =~ $in_single ]] || break
                line=${BASH_REMATCH[1]}
                open=""
                ;;
            '"')
                [[ $line =~ $in_double ]]
                line=${BASH_REMATCH[2]}
                [[ -n $line ]] || break
                if [[ ${line:0:1} == '"' ]]; then
                    open=""
                    line=${line:1}
                else
                    line=${line:2}
                fi
                ;;
            *)
                [[ $line =~ $in_word ]]
                plain=${BASH_REMATCH[1]}
                line=${BASH_REMATCH[2]}
                [[ -n $line ]] || break
                if [[ ${plain:${#plain}-1} == [[:space:]\;\&\|\(\)] ]]; then
                    word_start=1
                elif [[ -n $plain ]]; then
                    word_start=0
                fi
                case ${line:0:1} in
                    $'\\')
                        ((${#line} == 1)) && open=$'\\'
                        line=${line:2}
                        ;;
                    '#')
                        ((word_start)) && break
                        line=${line:1}
                        ;;
                    *)
                        open=${line:0:1}
                        line=${line:1}
                        ;;
                esac
                word_start=0
                ;;
        esac
    done
    CNF_OPEN=$open
}

#
# Set the credential global that a ProxySQL admin setting names
#
# Globals:
#   USER
#   PASSWORD
#   HOST
#   PORT
# Arguments:
#   1: the setting's name
#   2: the value
#
function set_credential() {
    case $1 in
        PROXYSQL_USERNAME) USER=$2 ;;
        PROXYSQL_PASSWORD) PASSWORD=$2 ;;
        PROXYSQL_HOSTNAME) HOST=$2 ;;
        PROXYSQL_PORT) PORT=$2 ;;
    esac
}

#
# Print, space-separated, the credential settings a config line assigns
# under the shell but not as the KEY=value that opens it
#
# The shell assigns every word of a line made only of assignments, and the
# declaring builtins assign theirs too, so such a line can set a credential
# the plain KEY=value reading never sees.
#
# Arguments:
#   1: the line, with any lines it continues onto joined on
#
function cnf_unread_credentials() {
    local rest=$1
    local q="'"
    local builtin='^[[:space:]]*(export|readonly|declare|typeset)(([[:space:]]+-[A-Za-z]+)*)[[:space:]]+(.*)$'
    local word="^([A-Za-z_][A-Za-z0-9_]*)(\\+?=)([^[:space:]${q}\"\\\\]|\\\\.|${q}[^${q}]*${q}|\"([^\"\\\\]|\\\\.)*\")*([[:space:]]+(.*))?\$"
    local credential='^PROXYSQL_(USERNAME|PASSWORD|HOSTNAME|PORT)$'
    local read_first=1
    local name op

    if [[ $rest =~ $builtin ]]; then
        [[ ${BASH_REMATCH[1]} == export && -z ${BASH_REMATCH[2]} ]] || read_first=0
        rest=${BASH_REMATCH[4]}
    else
        rest=${rest#"${rest%%[![:space:]]*}"}
    fi
    while [[ $rest =~ $word ]]; do
        name=${BASH_REMATCH[1]}
        op=${BASH_REMATCH[2]}
        rest=${BASH_REMATCH[6]}
        if [[ $name =~ $credential ]]; then
            ((read_first)) && [[ $op == "=" ]] || printf '%s ' "$name"
        fi
        read_first=0
    done
}

#
# Set the ProxySQL admin credentials from a proxysql-admin.cnf style file
#
# The file is read as KEY=value text and never run: --defaults-file can name
# any readable file, and running it would execute whatever it holds with the
# snippet's privileges. A setting the file lacks takes its default, never a
# same-named environment variable, so the file alone decides.
#
# Globals:
#   USER
#   PASSWORD
#   HOST
#   PORT
#   CNF_OPEN
# Arguments:
#   1: the config file
#
function load_credentials() {
    local cnf=$1
    local q="'"
    local assignment='^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$'
    local credential='^PROXYSQL_(USERNAME|PASSWORD|HOSTNAME|PORT)$'
    # Spares most lines the character scan, which is slow in bash.
    local closed="^([^${q}\"\\\\]|${q}[^${q}]*${q}|\"[^\"\\\\]*\")*\$"
    local line key raw value status
    local logical=""
    local continued=0

    CNF_OPEN=""

    while IFS= read -r line || [[ -n $line ]]; do
        line=${line%$'\r'}
        case $CNF_OPEN in
            "'" | '"')
                # A line inside a quoted value belongs to that value, however
                # much it looks like a setting of its own.
                cnf_open_quote "$line"
                continue
                ;;
            $'\\')
                logical=${logical%$'\\'}$line
                continued=1
                ;;
            *)
                logical=$line
                continued=0
                ;;
        esac
        if [[ -n $CNF_OPEN || ! $line =~ $closed ]]; then
            cnf_open_quote "$line"
        fi
        [[ $CNF_OPEN == $'\\' || $logical != *PROXYSQL_* ]] && continue

        if [[ $logical =~ $assignment ]]; then
            key=${BASH_REMATCH[2]}
            raw=${BASH_REMATCH[3]}
            if [[ $key =~ $credential ]]; then
                status=1
                value=""
                # Joining hides the line break, but a value that ran on past
                # it is no plain string.
                if ((!continued)); then
                    value=$(cnf_unquote "$raw")
                    status=$?
                fi
                # A rejected value still overrides an earlier one, as the
                # last assignment does under the shell, and so falls back to
                # the default.
                set_credential "$key" "$value"
                # Never the value: it may be the password.
                ((status == 0)) || echo "Ignoring ${key} in ${cnf}: its value is not a plain string."
            fi
        fi
        for key in $(cnf_unread_credentials "$logical"); do
            set_credential "$key" ""
            echo "Ignoring ${key} in ${cnf}: its line is not a plain KEY=value setting."
        done
    done < "$cnf"

    HOST=${HOST:-127.0.0.1}
    PORT=${PORT:-6032}
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

    if [[ ! -r $DEFAULTS_FILE || -d $DEFAULTS_FILE ]]; then
        echo "Cannot find or read the config file (check --defaults-file): $DEFAULTS_FILE."
        exit 1
    fi

    load_credentials "$DEFAULTS_FILE"

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

            ADMIN_CNF_CONTENT=$(print_redacted_cnf "$DEFAULTS_FILE" 2> /dev/null)
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
