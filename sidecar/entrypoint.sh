#!/usr/bin/env bash
# Side-car PID 1: expand the per-deployment inputs into canonical settings
# variables, mint the bundled Valkey broker's credential for this container run,
# resolve PMM Extensions' Grafana service-account token, then hand off to supervisord.
# It waits, without a bound, for the two things PMM supplies while it starts: the
# SECRET_KEY file and a Grafana that can mint the token.
#
# The credential reaches valkey-server through a generated config file rather
# than a --requirepass flag, and reaches healthcheck.sh by being read back out of
# that file -- a HEALTHCHECK command runs in a fresh process that sees only the
# image's ENV, never what this script exports.
set -o errexit -o nounset -o pipefail

# Resolved from this script rather than written absolute so the start path can be
# exercised outside an image; in the image it is $APP_HOME either way.
app_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# Published once the Grafana token step below ends with a token, or with nothing
# to resolve; healthcheck.sh requires it. Cleared ahead of everything that can
# wait -- the key gate in settings-env.sh first of all -- because /tmp survives a
# container restart, and the previous run's marker would report this start as
# holding a token it has not resolved yet.
grafana_token_sentinel=/tmp/grafana-token.ok
rm -f "$grafana_token_sentinel"

# Sourced first, so a misconfigured container's first log line is the actionable
# message rather than a Valkey mint that was never going to be reached.
# shellcheck source=sidecar/settings-env.sh
. "$app_dir/settings-env.sh"

# Ahead of both steps below, because the Grafana helper resolves the settings
# stack and that refuses to build without this key -- and its failure is
# absorbed, so a key arriving after it would degrade auth with no diagnostic.
#
# Guarded on the file channel as well as the variable: the helper resolves a
# SECRETS_DIR file too, and exporting that answer would put a mounted secret
# into every supervised program's environment, which is the one thing mounting
# it avoids. Unlike the Grafana mint, the failure is not absorbed -- a non-zero
# exit means minting could not be proven safe, and starting anyway is what
# silently reverts every encrypted override to its YAML default.
if [[ -z ${ENCRYPTION_KEY:-} ]] && ! secret_file_supplies ENCRYPTION_KEY; then
    ENCRYPTION_KEY="$(python3 "$app_dir/encryption_key.py")"
    export ENCRYPTION_KEY
fi

valkey_conf=/tmp/valkey.conf

valkey_password="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"

# A password in argv is readable by every process in the container's PID
# namespace -- the same population the loopback bind fails to exclude when the
# side-car shares a namespace with PMM, which is the case this guards. umask
# rather than a follow-up chmod so the value is never briefly world-readable.
(
    umask 077
    # /tmp survives a container restart, so a leftover path could be a symlink
    # the redirection would follow -- and umask does not apply to an existing
    # target.
    rm -f "$valkey_conf"
    cat > "$valkey_conf" << EOF
port 6379
bind 127.0.0.1
maxmemory 256mb
maxmemory-policy noeviction
save ""
appendonly no
requirepass ${valkey_password}
EOF
)

# The environment outranks the mounted settings.yaml in the config-priority
# chain, so a profile carrying a password-less broker URL keeps working.
export CELERY__BROKER_URL="redis://:${valkey_password}@127.0.0.1:6379/0"
export CELERY__RESULT_BACKEND="redis://:${valkey_password}@127.0.0.1:6379/1"

# The helper waits for a Grafana that is still starting, so a non-zero exit is a
# fault no wait clears, such as a rejected admin credential. Absorbing it is
# load-bearing under errexit: a PID 1 that died on it would be restarted into
# the same failed sign-in, which is how Grafana comes to lock the admin account.
# The programs start without the token instead, and the sentinel left unwritten
# is what keeps that container from reporting healthy.
# The value arrives by command substitution for the same reason the Valkey
# password above does -- an argv is readable across the whole PID namespace.
if grafana_token="$(python3 "$app_dir/grafana_service_account.py")"; then
    if [[ -n $grafana_token ]]; then
        export_grafana_token "$grafana_token"
    fi
    touch "$grafana_token_sentinel"
fi

# The admin pair is more privileged than the token it mints and only the step
# above reads it, so leaving it exported would hand every supervised program a
# Grafana administrator credential for the life of the container.
unset grafana_token GF_SECURITY_ADMIN_USER GF_SECURITY_ADMIN_PASSWORD

# /tmp survives a container restart, and each schema one-shot clears its own
# sentinel only after being spawned -- concurrently with the API programs now
# gated on it, which could therefore read a previous run's marker. PID 1 runs
# before supervisord starts anything, so clearing here covers every container
# start and restart. A `supervisorctl restart` does not re-enter this script, so
# re-running a schema step inside a running container clears its sentinel first
# through clear_sentinels.sh -- the sequence sidecar/README.md documents.
rm -f /tmp/migrate-extensions.ok /tmp/migrate-inventory.ok /tmp/migrate-tasks.ok \
    /tmp/migrate-beat.ok

exec supervisord -c "$app_dir/supervisord.conf" "$@"
