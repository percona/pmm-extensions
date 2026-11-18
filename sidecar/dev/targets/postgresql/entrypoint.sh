#!/usr/bin/env bash
#
# Entrypoint of the local environment's PostgreSQL host. It initialises the data
# directory, turns on WAL archiving through pgBackRest, creates the stanza and
# the sample data, then registers the node and its PostgreSQL service with PMM
# and keeps pmm-agent running beside postgres. pmm-agent supervises the Nomad
# client, which is what makes this host selectable for a task.

set -o nounset
set -o pipefail

if [[ ${DEBUG:-0} == "1" ]]; then
    set -o xtrace
fi

error() { printf '✗ %s\n' "$*" >&2; }
success() { printf '✓ %s\n' "$*" >&2; }
info() { printf 'ℹ %s\n' "$*" >&2; }
debug() { [[ ${DEBUG:-0} == "1" ]] && printf '[DEBUG] %s\n' "$*" >&2 || true; }

# Runs a command with xtrace suspended, restoring it on return (local -), so
# DEBUG=1 does not write the password into the container log.
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

need_cmd postgres
need_cmd initdb
need_cmd psql
need_cmd pg_isready
need_cmd pgbench
need_cmd pgbackrest
need_cmd pmm-agent
need_cmd pmm-admin
need_cmd sudo
need_cmd install

usage() {
    cat << 'EOF'
entrypoint.sh

Container entrypoint of the PostgreSQL host. Takes no arguments; every input
arrives as an environment variable.

Options:
  -h, --help    Show this help

Environment (required):
  POSTGRESQL_PMM_PASSWORD  Password of the pmm role, used by the PMM exporters

Environment (optional):
  PGBACKREST_STANZA        Name of the pgBackRest stanza (default: dev)
  PMM_SERVICE_NAME         Name of the PostgreSQL service in PMM (default:
                           dev-postgresql)
  PMM_AGENT_CONFIG_FILE    pmm-agent config path (default:
                           /usr/local/percona/pmm/config/pmm-agent.yaml)
  DEBUG=1                  Trace execution
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

PGDATA=/var/lib/pgsql/17/data
# sudo resets PATH, so the server's own programs are called by full path.
PGBIN=/usr/pgsql-17/bin
BACKUP_REPO=/var/lib/pgbackrest
PGBACKREST_CONFIG=/etc/pgbackrest.conf
# The image's health check reads this file. /run survives a restart, so it is
# removed first.
READY_MARKER=/run/dev-host-ready
STANZA="${PGBACKREST_STANZA:-dev}"
SERVICE_NAME="${PMM_SERVICE_NAME:-dev-postgresql}"
PMM_CONFIG_FILE="${PMM_AGENT_CONFIG_FILE:-/usr/local/percona/pmm/config/pmm-agent.yaml}"

# Wrapped rather than expanded at top level: ${VAR:?} puts the value itself
# into the xtrace line.
require_secrets() {
    : "${POSTGRESQL_PMM_PASSWORD:?is not set}"
}

without_xtrace require_secrets

postgres_pid=0

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

as_postgres() {
    sudo -u postgres "$@"
}

shutdown_postgres() {
    info 'Stopping postgres'
    kill -TERM "${postgres_pid}" 2> /dev/null || true
    wait "${postgres_pid}" 2> /dev/null || true
}

init_datadir() {
    install -d -o postgres -g postgres -m 0700 "${PGDATA}" || return 1
    as_postgres "${PGBIN}/initdb" -D "${PGDATA}" > /dev/null || return 1
    cat >> "${PGDATA}/postgresql.conf" << EOF || return 1
listen_addresses = '*'
wal_level = replica
archive_mode = on
archive_command = 'pgbackrest --stanza=${STANZA} archive-push %p'
shared_preload_libraries = 'pg_stat_statements'
EOF
    echo 'host all all 0.0.0.0/0 scram-sha-256' >> "${PGDATA}/pg_hba.conf"
}

# /etc is an image layer, not a volume, so the file is written on every start.
write_pgbackrest_config() {
    install -d -o postgres -g postgres -m 0750 "${BACKUP_REPO}" /var/log/pgbackrest || return 1
    cat > "${PGBACKREST_CONFIG}" << EOF || return 1
[global]
repo1-path=${BACKUP_REPO}
repo1-retention-full=2

[${STANZA}]
pg1-path=${PGDATA}
EOF
    chown postgres:postgres "${PGBACKREST_CONFIG}"
}

# Converges, because this may run again on a data volume that already has the role.
create_pmm_role() {
    as_postgres psql -v ON_ERROR_STOP=1 -q << SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'pmm') THEN
    CREATE ROLE pmm LOGIN SUPERUSER PASSWORD '${POSTGRESQL_PMM_PASSWORD}';
  ELSE
    ALTER ROLE pmm PASSWORD '${POSTGRESQL_PMM_PASSWORD}';
  END IF;
END \$\$;
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
SQL
}

seed_sample_data() {
    as_postgres psql -Atqc "SELECT 1 FROM pg_database WHERE datname='bench'" | grep -q 1 ||
        as_postgres "${PGBIN}/createdb" bench || return 1
    if ! as_postgres psql -d bench -Atqc 'SELECT 1 FROM pgbench_accounts LIMIT 1' > /dev/null 2>&1; then
        info 'Creating the pgbench sample data'
        as_postgres "${PGBIN}/pgbench" -i -q -s 10 bench || return 1
    fi
}

# The PMM Client image ships an empty pmm-agent.yaml, so the file existing says
# nothing. The agent id inside it says the node was set up. Keeping that id
# across a recreate matters: PMM Extensions matches inventory nodes by it.
agent_is_registered() {
    grep -qE '^id: *[^"[:space:]]' "${PMM_CONFIG_FILE}" 2> /dev/null
}

add_postgresql_service() {
    pmm-admin add postgresql \
        --username=pmm --password="${POSTGRESQL_PMM_PASSWORD}" \
        --host=127.0.0.1 --port=5432 --query-source=pgstatements \
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
        info "PostgreSQL service ${SERVICE_NAME} is already registered"
    else
        without_xtrace add_postgresql_service || {
            error 'pmm-admin add postgresql failed'
            exit 1
        }
        success "Registered PostgreSQL service ${SERVICE_NAME}"
    fi
}

main() {
    rm -f "${READY_MARKER}"

    if [[ ! -s ${PGDATA}/PG_VERSION ]]; then
        info 'Initialising the PostgreSQL data directory'
        init_datadir || {
            error 'Could not initialise the data directory'
            exit 1
        }
    fi

    write_pgbackrest_config || {
        error "Could not write ${PGBACKREST_CONFIG}"
        exit 1
    }

    # A restarted container keeps /run, so the socket lock of the previous run
    # is still there.
    rm -f /run/postgresql/.s.PGSQL.5432 /run/postgresql/.s.PGSQL.5432.lock "${PGDATA}/postmaster.pid"
    as_postgres "${PGBIN}/postgres" -D "${PGDATA}" &
    postgres_pid=$!
    trap shutdown_postgres INT TERM

    retry_cmd 60 1 pg_isready -h 127.0.0.1 -q || {
        error 'postgres never became ready'
        exit 1
    }
    success 'postgres is up'

    without_xtrace create_pmm_role || {
        error 'Could not create the pmm role'
        exit 1
    }

    seed_sample_data || {
        error 'Could not create the sample data'
        exit 1
    }

    as_postgres pgbackrest --stanza="${STANZA}" stanza-create || {
        error 'pgbackrest stanza-create failed'
        exit 1
    }
    success "pgBackRest stanza ${STANZA} is ready"

    register_with_pmm

    : > "${READY_MARKER}"
    success 'The host is ready to run tasks'

    wait "${postgres_pid}"
}

main "$@"
