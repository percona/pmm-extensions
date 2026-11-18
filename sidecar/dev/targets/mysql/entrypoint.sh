#!/usr/bin/env bash
#
# Entrypoint of the local environment's MySQL host. It initialises the data
# directory, creates the accounts the backup tools sign in with, imports the
# sample data, then registers the node and its MySQL service with PMM and keeps
# pmm-agent running beside mysqld. pmm-agent supervises the Nomad client, which
# is what makes this host selectable for a task.

set -o nounset
set -o pipefail

if [[ ${DEBUG:-0} == "1" ]]; then
    set -o xtrace
fi

error() { printf '✗ %s\n' "$*" >&2; }
success() { printf '✓ %s\n' "$*" >&2; }
info() { printf 'ℹ %s\n' "$*" >&2; }
debug() { [[ ${DEBUG:-0} == "1" ]] && printf '[DEBUG] %s\n' "$*" >&2 || true; }

# Runs a command with xtrace suspended, restoring it on return (local -).
# DEBUG=1 traces every expansion, and the passwords reach the commands wrapped
# below as arguments and as option-file contents.
without_xtrace() {
    local -
    set +o xtrace
    "$@"
}

need_cmd() {
    command -v "$1" > /dev/null 2>&1 || {
        error "Missing required command: $1"
        exit 2
    }
}

need_cmd mysqld
need_cmd mysql
need_cmd mysqladmin
need_cmd pmm-agent
need_cmd pmm-admin
need_cmd install
need_cmd curl
need_cmd awk

usage() {
    cat << 'EOF'
entrypoint.sh

Container entrypoint of the MySQL host. Takes no arguments; every input arrives
as an environment variable.

Options:
  -h, --help    Show this help

Environment (required):
  MYSQL_ROOT_PASSWORD    root@localhost password
  MYSQL_BACKUP_PASSWORD  extensions_backup@% password, the account the backup
                         tools sign in with through /root/.my.cnf
  MYSQL_PMM_PASSWORD     pmm@127.0.0.1 password, used by the PMM exporters

Environment (optional):
  PMM_SERVICE_NAME       Name of the MySQL service in PMM (default: dev-mysql)
  PMM_AGENT_CONFIG_FILE  pmm-agent config path (default:
                         /usr/local/percona/pmm/config/pmm-agent.yaml)
  DEBUG=1                Trace execution
EOF
    exit "${1:-0}"
}

[[ $# -eq 0 ]] || case "$1" in
    -h | --help) usage 0 ;;
    *)
        error "Unexpected argument: $1"
        usage 2
        ;;
esac

DATADIR=/var/lib/mysql
DEFAULTS_FILE=/root/.my.cnf
SEED_DIR=/opt/test_db
SOCKET=/var/lib/mysql/mysql.sock
# Both markers live on the data volume beside what they describe, and each is
# written only after its step completes. The data directory existing says only
# that the step started.
SEED_MARKER=/var/lib/mysql/.dev-seed-imported
USERS_MARKER=/var/lib/mysql/.dev-users-created
# The image's health check reads this file. /run survives a restart, so it is
# removed first.
READY_MARKER=/run/dev-host-ready
SERVICE_NAME="${PMM_SERVICE_NAME:-dev-mysql}"
PMM_CONFIG_FILE="${PMM_AGENT_CONFIG_FILE:-/usr/local/percona/pmm/config/pmm-agent.yaml}"

# Wrapped rather than expanded at top level: ${VAR:?} puts the value itself
# into the xtrace line.
require_secrets() {
    : "${MYSQL_ROOT_PASSWORD:?is not set}" \
        "${MYSQL_BACKUP_PASSWORD:?is not set}" \
        "${MYSQL_PMM_PASSWORD:?is not set}"
}

without_xtrace require_secrets

mysqld_pid=0

# Arguments: attempts, seconds between them, then the command and its arguments
# Returns: 0 as soon as the command succeeds, 1 once the attempts are spent
retry_cmd() {
    local attempts="$1" delay="$2"
    shift 2
    local attempt
    for ((attempt = 1; attempt <= attempts; attempt++)); do
        if "$@"; then
            return 0
        fi
        debug "attempt ${attempt}/${attempts} failed: $*"
        ((attempt < attempts)) && sleep "${delay}"
    done
    return 1
}

# "Access denied" still means the server is up, which is all this waits for.
mysqld_is_answering() {
    local output
    output="$(mysqladmin --protocol=socket --user=root ping 2>&1)"
    [[ ${output} == *"mysqld is alive"* || ${output} == *"Access denied"* ]]
}

shutdown_mysqld() {
    info 'Stopping mysqld'
    kill -TERM "${mysqld_pid}" 2> /dev/null || true
    wait "${mysqld_pid}" 2> /dev/null || true
}

# Connects as root over the socket and ignores every option file. Option files
# outrank MYSQL_PWD, so a /root/.my.cnf left by an earlier start would supply
# the backup account's password for these root connections. --no-defaults also
# drops /etc/my.cnf, hence the explicit socket.
mysql_as_root() {
    MYSQL_PWD="$1" mysql --no-defaults --protocol=socket --socket="${SOCKET}" \
        --user=root "${@:2}"
}

# A fresh data directory leaves root without a password, and bootstrap_users
# changes that last. Reporting which state holds lets an interrupted bootstrap
# be entered again from either one.
root_needs_password() {
    mysql_as_root '' --execute='SELECT 1' > /dev/null 2>&1 && return 1
    return 0
}

# Every statement converges, because this may run again after an interrupted
# bootstrap. Deleting the anonymous user is what keeps the backup account's '%'
# host matching a socket connection.
bootstrap_users() {
    local password=''
    root_needs_password && password="${MYSQL_ROOT_PASSWORD}"
    mysql_as_root "${password}" << SQL
DELETE FROM mysql.user WHERE User='';
DROP DATABASE IF EXISTS test;
CREATE USER IF NOT EXISTS 'extensions_backup'@'%' IDENTIFIED WITH caching_sha2_password BY '${MYSQL_BACKUP_PASSWORD}';
ALTER USER 'extensions_backup'@'%' IDENTIFIED WITH caching_sha2_password BY '${MYSQL_BACKUP_PASSWORD}';
GRANT ALL ON *.* TO 'extensions_backup'@'%' WITH GRANT OPTION;
CREATE USER IF NOT EXISTS 'pmm'@'127.0.0.1' IDENTIFIED WITH caching_sha2_password BY '${MYSQL_PMM_PASSWORD}';
ALTER USER 'pmm'@'127.0.0.1' IDENTIFIED WITH caching_sha2_password BY '${MYSQL_PMM_PASSWORD}'
    WITH MAX_USER_CONNECTIONS 10;
GRANT SELECT, RELOAD, PROCESS, REPLICATION CLIENT ON *.* TO 'pmm'@'127.0.0.1';
FLUSH PRIVILEGES;
ALTER USER 'root'@'localhost' IDENTIFIED BY '${MYSQL_ROOT_PASSWORD}';
SQL
}

# /root is an image layer, not the data volume, so the file is written on every
# start. [client] serves PyMySQL and mydumper; [xtrabackup] and [mysqld] serve
# xtrabackup. [client] carries no socket on purpose: PyMySQL would prefer it
# over the host it was given.
write_defaults_file() {
    install -m 0600 /dev/null "${DEFAULTS_FILE}" || return 1
    cat > "${DEFAULTS_FILE}" << EOF || return 1
[client]
user=extensions_backup
password=${MYSQL_BACKUP_PASSWORD}

[mysqld]
datadir=${DATADIR}
socket=${SOCKET}

[xtrabackup]
user=extensions_backup
password=${MYSQL_BACKUP_PASSWORD}
socket=${SOCKET}
EOF
}

# employees.sql sources its dump files by relative path, so an import from any
# other directory loads the schema, skips the data, and still exits 0.
import_seed() {
    (cd "${SEED_DIR}" && mysql < employees.sql)
}

# The PMM Client image ships an empty pmm-agent.yaml, so the file existing says
# nothing. The agent id inside it says the node was set up. Keeping that id
# across a recreate matters: PMM Extensions matches inventory nodes by it.
agent_is_registered() {
    grep -qE '^id: *[^"[:space:]]' "${PMM_CONFIG_FILE}" 2> /dev/null
}

# pmm-agent starts the Nomad client last, and PMM Extensions lists a host only
# once that client has joined PMM's Nomad. The client answers over HTTPS on the
# port that pmm-admin shows for it.
nomad_client_is_ready() {
    local port
    port="$(pmm-admin list 2> /dev/null | awk '$1 == "nomad_agent" && $2 == "Running" {print $NF}')"
    [[ -n ${port} ]] || return 1
    curl -sk --max-time 3 "https://127.0.0.1:${port}/v1/agent/health?type=client" |
        grep -q '"ok":true'
}

add_mysql_service() {
    pmm-admin add mysql \
        --username=pmm --password="${MYSQL_PMM_PASSWORD}" \
        --host=127.0.0.1 --port=3306 --query-source=perfschema \
        --service-name="${SERVICE_NAME}"
}

register_with_pmm() {
    if agent_is_registered; then
        info 'Node already registered; keeping the existing pmm-agent config'
    else
        # PMM reports healthy before it accepts a registration, hence the retry.
        info 'Registering this node with PMM'
        retry_cmd 30 2 pmm-agent setup || {
            error 'pmm-agent setup failed'
            exit 1
        }
        success 'Node registered'
    fi

    # Restarting the agent keeps the Nomad client it supervises alive. Without
    # it the node drops out of the execution hosts with no message.
    while true; do
        pmm-agent run --config-file="${PMM_CONFIG_FILE}"
        info 'pmm-agent exited; restarting it in 5s'
        sleep 5
    done &

    # `pmm-admin list` succeeds only once the agent's channel to PMM is up.
    retry_cmd 90 2 pmm-admin list > /dev/null || {
        error 'pmm-agent never connected to PMM'
        exit 1
    }

    if pmm-admin list 2> /dev/null | grep -qw "${SERVICE_NAME}"; then
        info "MySQL service ${SERVICE_NAME} is already registered"
    else
        without_xtrace add_mysql_service || {
            error 'pmm-admin add mysql failed'
            exit 1
        }
        success "Registered MySQL service ${SERVICE_NAME}"
    fi
}

main() {
    rm -f "${READY_MARKER}"

    if [[ ! -d ${DATADIR}/mysql ]]; then
        info 'Initialising the MySQL data directory'
        mysqld --initialize-insecure --user=mysql --datadir="${DATADIR}" || {
            error 'Could not initialise the data directory'
            exit 1
        }
        # A data directory this fresh cannot match a credential file left by an
        # earlier start, and the stale file would outrank the command-line user.
        rm -f "${DEFAULTS_FILE}"
    fi

    mysqld --user=mysql --datadir="${DATADIR}" &
    mysqld_pid=$!
    trap shutdown_mysqld INT TERM

    retry_cmd 120 1 mysqld_is_answering || {
        error 'mysqld never started answering on its socket'
        exit 1
    }
    success 'mysqld is up'

    if [[ ! -f ${USERS_MARKER} ]]; then
        without_xtrace bootstrap_users || {
            error 'Could not create the MySQL users'
            exit 1
        }
        : > "${USERS_MARKER}" || {
            error "Could not record the user bootstrap at ${USERS_MARKER}"
            exit 1
        }
        success 'Created the extensions_backup and pmm users'
    fi

    without_xtrace write_defaults_file || {
        error "Could not write ${DEFAULTS_FILE}"
        exit 1
    }
    success "Wrote ${DEFAULTS_FILE}"

    if [[ ! -f ${SEED_MARKER} ]]; then
        info 'Importing the employees sample data (this takes a few minutes)'
        import_seed || {
            error 'Could not import the employees sample data'
            exit 1
        }
        : > "${SEED_MARKER}" || {
            error "Could not record the import at ${SEED_MARKER}"
            exit 1
        }
        success 'Imported the employees sample data'
    fi

    register_with_pmm

    retry_cmd 90 2 nomad_client_is_ready || {
        error 'The Nomad client never joined PMM'
        exit 1
    }
    success 'The Nomad client has joined PMM'

    : > "${READY_MARKER}"
    success 'The host is ready to run tasks'

    wait "${mysqld_pid}"
}

main "$@"
