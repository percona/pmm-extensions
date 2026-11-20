#!/usr/bin/env bash
# Expand the documented per-deployment inputs into the canonical __-nested
# settings variables, leaving unexported any name a file under SECRETS_DIR
# already supplies. Sourced by entrypoint.sh; kept separate so it can be
# exercised under `env -i` without starting a container.
#
# Assumes the caller's `set -o nounset`, hence ${VAR:-} on every optional read.

# Names lowercased because the settings source matches file names
# case-insensitively; an exact-match lookup here would miss a mounted file and
# then shadow it with a derived export, which outranks every secret file.
# Entries resolving outside the directory are skipped, matching the walk in
# NestedSecretsSettingsSource -- reading one here would suppress an export the
# settings classes then never fill.
#
# A function because the SECRET_KEY gate below reads the directory again while it
# waits: PMM publishes into it after this script has read it once.
declare -A secret_files=()
secrets_root=""
scan_secret_files() {
    local secret_entry secret_base
    secret_files=()
    secrets_root=""
    [[ -n ${SECRETS_DIR:-} && -d ${SECRETS_DIR} ]] || return 0
    secrets_root="$(readlink -f -- "${SECRETS_DIR}")"
    for secret_entry in "$secrets_root"/*; do
        [[ -f $secret_entry ]] || continue
        [[ "$(readlink -f -- "$secret_entry")" == "$secrets_root"/* ]] || continue
        secret_base="${secret_entry##*/}"
        secret_files["${secret_base,,}"]="$secret_entry"
    done
}
scan_secret_files

# Keyed on the file existing rather than on its contents: pydantic-settings
# resolves an empty secret file to the empty string instead of falling through to
# the next source.
secret_file_supplies() {
    [[ -n ${secret_files["${1,,}"]:-} ]]
}

# Stripped the way the settings source strips it, which `$(<file)` alone is not:
# that trims trailing newlines only.
read_secret_file() {
    local raw
    raw="$(< "${secret_files["${1,,}"]}")"
    raw="${raw#"${raw%%[![:space:]]*}"}"
    printf '%s' "${raw%"${raw##*[![:space:]]}"}"
}

# The script's own resolution order, applied to one canonical name: an explicitly
# set variable, then a mounted file -- left unexported so the settings class reads
# it -- then the derived value.
export_canonical() {
    local name="$1" derived="$2" current
    current="${!name:-}"
    if [[ -n $current ]]; then
        export "$name=$current"
    elif secret_file_supplies "$name"; then
        # Cleared rather than merely left alone: a name inherited as the empty
        # string counts as supplied on the environment side, so leaving it in
        # place would rank it above the file this branch defers to.
        unset "$name"
    else
        export "$name=$derived"
    fi
}

# Settings.SECRET_KEY defaults to secrets.token_urlsafe(32) evaluated at
# class-definition time, so without an explicit value each supervisord child
# resolves a different key -- breaking session-cookie and CSRF validation across
# processes, and giving each process a different HMAC-derived EXTENSIONS_INTERNAL_TOKEN.
# The one name whose file must hold a value rather than merely exist: the
# settings classes refuse an empty key outright, so admitting one here would
# replace this gate's single actionable line with five crashing children.
mounted_secret_key() {
    secret_file_supplies SECRET_KEY && [[ -r ${secret_files[secret_key]} ]] &&
        read_secret_file SECRET_KEY
}

# A set SECRETS_DIR means the key is PMM's to publish, and PMM writes it while it
# starts -- which Compose's plain depends_on does not order ahead of this
# container. So the gate waits rather than exits, and without a bound: a bound
# gives up on a slow PMM, and recovering from that takes a restart policy this
# script cannot assume. The schema one-shots wait for PMM's PostgreSQL the same
# way.
#
# The last read comes one poll after the key appears. PMM publishes the key and
# the database passwords as consecutive renames, key first, so the instant the
# key appears is the one instant the other files may still be missing.
wait_for_published_secret_key() {
    local next_log=$SECONDS
    until [[ -n "$(mounted_secret_key)" ]]; do
        if ((SECONDS >= next_log)); then
            # A file that is there and unreadable is not one PMM has yet to
            # write, so the line names what an operator has to change instead.
            if secret_file_supplies SECRET_KEY && [[ ! -r ${secret_files[secret_key]} ]]; then
                echo "[entrypoint] Waiting for SECRET_KEY under ${SECRETS_DIR}:" \
                    "the file is there, but uid $(id -u) with groups $(id -G)" \
                    "cannot read it. PMM writes it for group 0." >&2
            else
                echo "[entrypoint] Waiting for PMM to publish SECRET_KEY under" \
                    "${SECRETS_DIR}. PMM writes it there when it starts with" \
                    "PMM_ENABLE_EXTENSIONS=1." >&2
            fi
            next_log=$((SECONDS + 10))
        fi
        sleep 1
        scan_secret_files
    done
    echo "[entrypoint] SECRET_KEY is published under ${SECRETS_DIR}; starting." >&2
    sleep 1
    scan_secret_files
}

if [[ -z ${SECRET_KEY:-} ]]; then
    if [[ -z "$(mounted_secret_key)" ]]; then
        if [[ -z ${SECRETS_DIR:-} ]]; then
            echo "[entrypoint] SECRET_KEY is required. Generate one with" \
                "'openssl rand -hex 32' and pass it to the container, or mount it as a" \
                "file named SECRET_KEY under SECRETS_DIR." >&2
            exit 1
        fi
        wait_for_published_secret_key
    fi
    # Cleared for the same reason export_canonical clears a deferred name: an
    # inherited empty string outranks the file every child then reads.
    unset SECRET_KEY
fi

# No gate and no export_canonical fan-out: the key sits on the shared core
# Settings class, so it has no EXTENSIONS__/INVENTORY__/TASKS__ spellings, and two
# further sources sit below this file -- a key an earlier start persisted, and
# one entrypoint.sh mints. Only the blank clear is owed here, and it is
# load-bearing rather than cosmetic: an inherited empty string counts as
# supplied, and was measured shadowing a valid mounted file into a refusal to
# start.
if [[ -z ${ENCRYPTION_KEY:-} ]] && secret_file_supplies ENCRYPTION_KEY; then
    unset ENCRYPTION_KEY
fi

# The migrate wait loops read EXTENSIONS_DB_HOST/EXTENSIONS_DB_PORT below, so a mounted host or
# port has to seed them before their defaults apply -- and seeding all three
# services off it is why a mounted host is not confined to PMM Extensions the way a mounted
# password is.
if [[ -z ${EXTENSIONS__DATABASE__HOST:-} ]] && secret_file_supplies EXTENSIONS__DATABASE__HOST; then
    EXTENSIONS_DB_HOST="$(read_secret_file EXTENSIONS__DATABASE__HOST)"
fi
if [[ -z ${EXTENSIONS__DATABASE__PORT:-} ]] && secret_file_supplies EXTENSIONS__DATABASE__PORT; then
    EXTENSIONS_DB_PORT="$(read_secret_file EXTENSIONS__DATABASE__PORT)"
fi

# Exported unconditionally: supervisord.conf expands these via %(ENV_...)s, which
# reads supervisord's own environment.
export EXTENSIONS_DB_HOST="${EXTENSIONS_DB_HOST:-pmm-server}"
export EXTENSIONS_DB_PORT="${EXTENSIONS_DB_PORT:-5432}"

export_canonical EXTENSIONS__DATABASE__HOST "$EXTENSIONS_DB_HOST"
export_canonical INVENTORY__DATABASE__HOST "$EXTENSIONS_DB_HOST"
export_canonical TASKS__DATABASE__HOST "$EXTENSIONS_DB_HOST"
export_canonical EXTENSIONS__DATABASE__PORT "$EXTENSIONS_DB_PORT"
export_canonical INVENTORY__DATABASE__PORT "$EXTENSIONS_DB_PORT"
export_canonical TASKS__DATABASE__PORT "$EXTENSIONS_DB_PORT"

# The four guards below skip calling export_canonical -- and so skip its blank
# clear -- whenever their raw input is absent; EXTENSIONS_INTERNAL_TOKEN, BASE_URL and
# CELERY__BEAT_DBURI have no guard and no export_canonical call at all. A blank
# inherited value on any of these still outranks the file or default below it,
# and for the URL-typed ones an inherited blank fails settings validation
# outright, so all of them are cleared unconditionally before any guard decides.
blank_cleared_names=(
    DATABASE__PASSWORD
    EXTENSIONS__DATABASE__PASSWORD INVENTORY__DATABASE__PASSWORD TASKS__DATABASE__PASSWORD
    AUTH__PROVIDER__GRAFANA__SERVICE_ACCOUNT_TOKEN PMM__API_KEY
    PMM__ENDPOINT AUTH__PROVIDER__GRAFANA__ENDPOINT
    TASKS__NOMAD__ENDPOINT TASKS__NOMAD__API_KEY
    EXTENSIONS_INTERNAL_TOKEN BASE_URL
    CELERY__BEAT_DBURI
)
for name in "${blank_cleared_names[@]}"; do
    if [[ -z ${!name:-} ]]; then
        unset "$name"
    fi
done
unset name

# The outer guard stays: without a raw input and without a file, export_canonical
# alone would export the canonical name empty.
if [[ -n ${EXTENSIONS_DB_PASSWORD:-} ]]; then
    export_canonical EXTENSIONS__DATABASE__PASSWORD "$EXTENSIONS_DB_PASSWORD"
    export_canonical INVENTORY__DATABASE__PASSWORD "$EXTENSIONS_DB_PASSWORD"
    export_canonical TASKS__DATABASE__PASSWORD "$EXTENSIONS_DB_PASSWORD"
fi

# A function rather than inline exports in the guard below because entrypoint.sh
# calls it a second time, with a token minted after this file has finished, and
# every destination name has to keep resolving from one place.
export_grafana_token() {
    export_canonical AUTH__PROVIDER__GRAFANA__SERVICE_ACCOUNT_TOKEN "$1"
    export_canonical PMM__API_KEY "$1"
    export_canonical TASKS__NOMAD__API_KEY "$1"
}

if [[ -n ${EXTENSIONS_GRAFANA_TOKEN:-} ]]; then
    export_grafana_token "$EXTENSIONS_GRAFANA_TOKEN"
fi

if [[ -n ${EXTENSIONS_PMM_ENDPOINT:-} ]]; then
    # Trailing slash trimmed so the appended Grafana prefix cannot double it.
    pmm_endpoint="${EXTENSIONS_PMM_ENDPOINT%/}"
    export_canonical PMM__ENDPOINT "$pmm_endpoint"
    export_canonical AUTH__PROVIDER__GRAFANA__ENDPOINT "${pmm_endpoint}/graph"
fi

if [[ -n ${EXTENSIONS_NOMAD_ENDPOINT:-} ]]; then
    export_canonical TASKS__NOMAD__ENDPOINT "$EXTENSIONS_NOMAD_ENDPOINT"
fi
