#!/usr/bin/env bash
# Aggregate container health for the consolidated PMM Extensions side-car.
# Exit 0 only if the entrypoint resolved the Grafana token, every non-one-shot
# supervisord program is RUNNING, all four schema steps completed, all three API
# /health endpoints return 200, and the bundled Valkey broker answers PING.
#
# An API program still inside its wait_for_schema.sh gate reports RUNNING, so
# the process-state loop below cannot distinguish waiting from serving: the HTTP
# probe is what keeps a healthy container meaning the APIs answer.
set -o errexit -o nounset -o pipefail

conf=/home/extensions/app/supervisord.conf

# Asserted first, because it is the answer for both states it covers: an
# entrypoint still waiting for PMM, where no program below exists yet, and
# programs that started after a mint no retry could clear, where every check
# below passes while sign-in, the PMM client and the task executor hold no
# credential.
if [[ ! -f /tmp/grafana-token.ok ]]; then
    echo 'unhealthy: PMM Extensions holds no Grafana service-account token;' \
        'the container log says what the start is waiting for, or why the mint failed' >&2
    exit 1
fi

# Only the migration one-shots may sit in EXITED; every other program must be
# RUNNING. A failed upgrade also lands in EXITED and is indistinguishable here,
# so completion is asserted separately via the sentinels each one-shot writes.
# Each program is asserted positively rather than by filtering status output, so
# a supervisorctl that cannot reach the socket reports unhealthy instead of
# yielding an empty diff.
status="$(supervisorctl -c "$conf" status || true)"
mapfile -t programs < <(sed -n 's/^\[program:\(.*\)\]$/\1/p' "$conf" | grep -v '^migrate-')
if [[ ${#programs[@]} -eq 0 ]]; then
    echo "unhealthy: no programs declared in $conf" >&2
    exit 1
fi
for prog in "${programs[@]}"; do
    if ! grep -qE "^${prog}[[:space:]]+RUNNING([[:space:]]|$)" <<< "$status"; then
        printf 'unhealthy: %s is not RUNNING:\n%s\n' "$prog" "$status" >&2
        exit 1
    fi
done

for svc in extensions inventory tasks beat; do
    if [[ ! -f /tmp/migrate-$svc.ok ]]; then
        echo "unhealthy: $svc schema step did not complete successfully" >&2
        exit 1
    fi
done

# curl is absent from the slim base, so probe over loopback with urllib instead.
python3 - << 'PY'
import sys
import urllib.request

for port in (9000, 9001, 9002):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as resp:
            if resp.status != 200:
                sys.exit(f"unhealthy: :{port}/health -> HTTP {resp.status}")
    except Exception as exc:
        sys.exit(f"unhealthy: :{port}/health -> {exc}")
PY

# The generated broker config is the only channel for the credential: this runs
# in a fresh process that sees the image's ENV, not what the entrypoint exported.
# REDISCLI_AUTH rather than --pass keeps it out of this probe's own argv.
valkey_conf=/tmp/valkey.conf
REDISCLI_AUTH="$(sed -n 's/^requirepass //p' "$valkey_conf" 2> /dev/null || true)"
if [[ -z $REDISCLI_AUTH ]]; then
    echo "unhealthy: no broker credential in $valkey_conf" >&2
    exit 1
fi
export REDISCLI_AUTH

if [[ "$(valkey-cli -p 6379 ping 2> /dev/null)" != "PONG" ]]; then
    echo 'unhealthy: valkey broker did not answer PING' >&2
    exit 1
fi

echo 'healthy'
