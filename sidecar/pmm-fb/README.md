# PMM + PMM Extensions feature-build harness

Compose topology pairing the PMM feature build (PMM Extensions frontend, PostgreSQL
exposure, secret provisioning and the native `/extensions` proxy,
[Percona-Lab/pmm-submodules#4500] = percona/pmm `main` at
`7a20dfff5`, where [percona/pmm#5992] merged, on top of the exposure from
[percona/pmm#5653] + [percona/pmm#5700]) with the app-restricted PMM Extensions side-car:
supervisord running the three APIs + Celery worker/beat + bundled Valkey,
shipping only the `inventory`, `mysql_backups` and `atw` apps. The snippets
management app is not shipped — the builtin snippet library is ingested and
auto-approved at boot (SEP-1627) so atw can execute it, with no periodic or
manual re-sync.

## Try the Tech Preview

This is the fastest way to see PMM's management pages today. It runs a PMM
*feature build* (a pre-release image cut from a pull request) next to the PMM Extensions
side-car, on one machine, with Docker Compose. It is a preview, not an install:
the PMM image is rebuilt without notice, nothing here upgrades in place, and
both services listen on loopback only.

You need Docker (or Podman) with Compose. On an arm64 host `pmm-server` and the
side-car run under x86-64 emulation, while the MySQL target below is built
natively instead ([Caveats](#caveats)). Then:

```bash
git clone -b pmm https://github.com/percona/SEP.git
cd SEP/sidecar/pmm-fb
docker compose up -d
```

First boot takes a couple of minutes; `extensions-sidecar` stays in `Created` until
pmm-server reports healthy. Then sign in at <https://127.0.0.1:8443> as
`admin` / `admin` and open **Management** in the sidebar. There is no second
login: the management pages use your PMM session.

What you can do from there:

- **MySQL Backups** needs a MySQL server that PMM monitors *and* that runs a
  PMM Client, because the backup executes on that node. The `mysql` profile
  adds one, seeded with the `employees` sample database, so there is something
  to back up:

  ```bash
  ./bootstrap.sh                                # generates the profile's .env
  docker compose --profile mysql up -d --build
  ```

  The build and the seed import take several minutes; wait for `Imported the
  employees seed dataset` in `docker compose logs -f extensions-mysql`.
- **Support diagnostics** collects troubleshooting output and sends it to a
  Percona Support case. Its landing page asks for that connection before it
  opens, so without a Percona Support subscription this app stays on its setup
  screen.

To stop and remove everything, including data:
`docker compose --profile mysql down -v`.

### Using your own MySQL instead

The seeded container is a convenience, not a requirement. PMM Extensions does no
scheduling: it pins the backup job to the node you pick and runs it there as a
plain process, so any host qualifies once it carries all five of

- a PMM Client 3.2 or newer, registered to *this* pmm-server — the Nomad
  binary the executor needs ships with the client from that version
- a reachable server address: `compose.yaml` publishes 8443 on loopback and
  sets `PMM_PUBLIC_ADDRESS` to a compose service name, so a client outside
  this host needs both changed to an address it can resolve
- the MySQL service added to PMM (`pmm-admin add mysql`), which PMM Extensions'
  inventory sync picks up within 15 minutes
- XtraBackup or Mydumper installed on that host — PMM Extensions runs them, it does not
  ship them
- credentials for a backup user in a defaults file the executor can read,
  since the create form takes a host and options but no password

The host then appears in the form's executor picker, which lists only nodes
whose Nomad `raw_exec` driver reports healthy.

**Your own PMM server is a different matter, and the preview cannot use one.**
Everything PMM Extensions relies on — the `PMM_ENABLE_EXTENSIONS` switch, the `/extensions` proxy, the
PostgreSQL role, the secrets channel, the session exchange, the frontend — is
on percona/pmm `main` at `7a20dfff5` but in no released PMM yet. Point the
side-car at an existing PMM 3.x and none of it is there.

Everything below this line is the developer reference for the harness itself.

## Which image to pin

The restricted image is built on `main`, not on this branch, and the build emits
a **single** artifact under a suffix-less tag — the `-sidecar` / `-embedded`
variants no longer exist. `main`'s `image` target builds it with
`EXTENSIONS_RESTRICT_APPS=1`, deriving the shipped app set from `sidecar/settings.yaml`'s
`EXTENSIONS.APPS`, and publishes it as `percona/percona-sep:<commit-sha>`.

`compose.yaml`'s `extensions-sidecar` service therefore pins a main-line commit SHA.
Repin to a newer one by picking a tag published from `main` — the tag list on
Docker Hub is ordered by publish date.

Three properties of the pinned image are load-bearing, and all three are worth
checking on the **artifact** rather than on the commit that built it:

```bash
TAG=<the tag you are pinning>

# It must read secrets from a directory (SECRETS_DIR); expect a non-zero count.
docker run --rm --entrypoint sh docker.io/percona/percona-sep:$TAG \
  -c 'grep -c SECRETS_DIR /home/extensions/app/settings-env.sh'

# It must carry the Grafana token mint; expect the helper and a 0700 state dir.
docker run --rm --entrypoint sh docker.io/percona/percona-sep:$TAG \
  -c 'ls /home/extensions/app/grafana_service_account.py; ls -ld /home/extensions/state'

# It must carry a HEALTHCHECK; expect a Test naming healthcheck.sh.
skopeo inspect --config --raw docker://docker.io/percona/percona-sep:$TAG \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["config"]["Healthcheck"])'
```

`skopeo inspect --config` **without** `--raw` normalizes to OCI, whose
image-config spec has no `Healthcheck` field, so it reports `null` for every PMM Extensions
tag including ones that carry the instruction. Only the raw config blob answers
the question. Every side-car recipe builds in docker format for the same reason:
OCI discards the instruction. The 150 s start period keeps the side-car out of
`unhealthy` while PMM provisioning and PMM Extensions migrations finish.

The mint is the property a backwards repin loses most quietly. An image without
it satisfies the other two checks, comes up healthy, and mounts the `pmm-extensions-state`
volume over a directory nothing ever writes to — the only symptom is that
Grafana-backed sign-in and the PMM syncer are inert, with nothing logged.

The pmm-server pin is subject to its own constraint — see
[Caveats](#caveats).

[Percona-Lab/pmm-submodules#4500]: https://github.com/Percona-Lab/pmm-submodules/pull/4500
[percona/pmm#5653]: https://github.com/percona/pmm/pull/5653
[percona/pmm#5700]: https://github.com/percona/pmm/pull/5700
[percona/pmm#5992]: https://github.com/percona/pmm/pull/5992

## Bring-up

```bash
git clone -b pmm git@github.com:percona/SEP.git  # already cloned: git checkout pmm && git pull
cd SEP/sidecar/pmm-fb

docker compose up -d                          # pmm-server + extensions-sidecar
```

That is the whole prerequisite list. PMM publishes the four secrets PMM Extensions reads
from disk, and the side-car resolves its own Grafana token and its own
encryption key, so nothing has to be chosen or seeded in advance. `pmm-server`
and the side-car are x86-64 only, so an arm64 engine runs those two under
emulation; the `extensions-mysql` executor is built natively there instead — see
[Caveats](#caveats).

`./bootstrap.sh` is needed **only** for the `mysql` profile: it generates that
profile's three test-fixture passwords and, on an arm64 engine, the two executor
slots described under [Caveats](#caveats):

```bash
./bootstrap.sh                                # generate .env
docker compose --profile mysql up -d --build  # or: podman compose ...
```

`--profile mysql` adds the `extensions-mysql` task-execution target — a MySQL server,
a PMM Client and the employees seed in one container, documented in
[mysql-target.md](mysql-target.md). Omitting it is the supported fast path —
you get the same two services this harness has always had, and auth or UI work
does not pay for a MySQL build:

```bash
docker compose up -d        # pmm-server + extensions-sidecar only
```

Note that `docker compose build` obeys the profile too, and reports `No
services to build` rather than an error when you forget it.

First boot takes a couple of minutes. The side-car does not start until
pmm-server reports healthy, which now means "PMM Extensions' secrets are published" as
well as "PMM is up" — so expect `extensions-sidecar` to sit in `Created` for a while
before it runs. `extensions-mysql` initialises a datadir and imports the employees
dataset on its first boot; the import runs for several minutes. Its healthcheck
reports `healthy` as soon as MySQL answers — about a minute in, while the import
is still running — because health here means "the database is up" and nothing
more. Watch `docker compose logs -f extensions-mysql` for `Imported the employees seed
dataset` before expecting a backup to have data to copy. Then:

- PMM UI: https://127.0.0.1:8443 (admin / admin) — PMM Extensions' pages are part of it
- PMM Extensions' API through PMM's nginx: https://127.0.0.1:8443/extensions/api/… . The side-car
  serves no UI of its own, so `/extensions/` itself answers `{"detail":"Not Found"}`;
  that is the embedded topology working, not a routing fault.
- PMM Extensions APIs directly: http://127.0.0.1:9000-9002 (extensions / inventory / tasks)
- With the `mysql` profile up, `extensions-mysql` is a working executor:
  `GET /api/extensions/hosts/` lists it alongside `pmm-server`. This is independent of
  the PMM syncer — that endpoint sources its list from Nomad node status, not
  from Inventory, so no inventory sync is needed for `extensions-mysql` to appear here
  (see [mysql-target.md](mysql-target.md) for the separate, syncer-gated
  question of when the MySQL *service* shows up as a backup source).

There is no manual token-minting step. The side-car obtains its own Grafana
service-account token at container start when no token reaches it through
`SECRETS_DIR`, so Grafana-backed sign-in, the PMM syncer and task-lifecycle PMM
annotations all work on a first boot. It persists that token in the `pmm-extensions-state`
volume, so a recreate reuses it rather than minting a second one. A PMM build
that publishes the two Grafana token names into the secrets directory still
outranks the mint, which then does nothing — the side-car resolves each
canonical name from its own file first.

To probe the API by hand, exchange your PMM browser session for a short-lived
PMM Extensions bearer rather than looking for a static token — an unauthenticated request
is answered `401`, because nothing injects a bearer server-side any more:

```bash
curl -sk -c /tmp/pmm-cookies -X POST https://127.0.0.1:8443/graph/login \
  -H 'Content-Type: application/json' -d '{"user":"admin","password":"admin"}'
TOKEN=$(curl -sk -b /tmp/pmm-cookies -X POST \
  https://127.0.0.1:8443/extensions/api/oauth/session/exchange | python3 -c \
  'import json,sys; print(json.load(sys.stdin)["access_token"])')
curl -sk -H "Authorization: Bearer $TOKEN" https://127.0.0.1:8443/extensions/api/apps/
```

## How the pieces connect

- `bootstrap.sh` generates the gitignored `.env`, which holds the three
  `extensions-mysql` passwords and, on an arm64 engine, the two executor slots
  described under Caveats — test-fixture credentials for the `mysql` profile, not
  anything the pair needs, so a bring-up without that profile can skip it
  entirely. `extensions-mysql`'s entrypoint refuses to start without them; `compose.yaml`
  deliberately does not, because Compose interpolates every service at parse time
  regardless of the active profile, and a guard there would make the script a
  prerequisite of every bring-up. PMM generates the secrets it publishes itself,
  including the PostgreSQL role's password. Nothing secret is committed;
  re-running keeps an existing `.env`, appends any password slot it predates,
  and repoints the two executor slots it owns at the current engine — adding
  them on an arm64 engine, and reclaiming them if the deployment later moves to
  an amd64 one.
- **PMM owns the four secrets PMM Extensions reads from disk.** With `PMM_ENABLE_EXTENSIONS=1` it
  writes four files into the `pmm-extensions` volume, which pmm-server mounts at
  `/srv/extensions` and the side-car mounts read-only at `/run/secrets/extensions`.
  `SECRETS_DIR` points PMM Extensions at that directory and it reads each file as the
  canonical setting the filename names:

  | File | Written by | When |
  |---|---|---|
  | `SECRET_KEY` | the entrypoint | seconds after container start |
  | `EXTENSIONS__DATABASE__PASSWORD`, `INVENTORY__DATABASE__PASSWORD`, `TASKS__DATABASE__PASSWORD` | the entrypoint | seconds after container start |

  None of the four reaches the side-car as environment, so none appears in
  `docker inspect` or in the process environment. Nomad needs no credential
  here either: the side-car sends its minted Grafana token to PMM's `/nomad/`
  as a bearer, so the baked profile's credential-free endpoint is used as-is.
- **The Grafana service-account token is the side-car's own.** It is the one PMM Extensions
  credential this pin does not get from PMM: the side-car mints it against
  Grafana at start and persists it in the `pmm-extensions-state` volume. A PMM build that
  publishes the two canonical token names into `SECRETS_DIR` still outranks the
  mint, which is what makes the two owners compatible rather than competing.
- **`extensions-sidecar` waits on `condition: service_healthy`** because PMM Extensions builds its
  settings once, at process start, and never re-reads them: a side-car released
  before the files exist comes up with those settings permanently unset. Health
  means the current provisioning run has published them.
- **Group 0, not a matching uid.** The side-car runs as uid/gid 1001 and PMM
  writes the files mode 0640 owned by group `root` under a setgid `02770`
  directory. `group_add: ["0"]` is what makes them readable, and it keeps
  working if PMM's runtime uid changes.
- `PMM_ENABLE_EXTENSIONS=1` alone also makes pmm-server's entrypoint
  expose its embedded PostgreSQL on the compose network and provision the
  low-privilege `pmm_extensions` role owning the `pmm_extensions` database (percona/pmm#5700).
  Nothing is published on the host. `PMM_ENABLE_NOMAD=1` + `PMM_PUBLIC_ADDRESS`
  start PMM's embedded Nomad, which PMM Extensions task execution dispatches through
  (Nomad silently stays down if the public address is unset).
- All three PMM Extensions services **and** the Celery beat store share that single
  `pmm_extensions` database — the exposure provisions exactly one db/role, and the three
  Alembic tracks use distinct version tables with non-colliding table names.
  The side-car's migration one-shots wait for `pmm-server:5432` and migrate
  on first boot.
- **PMM proxies PMM Extensions natively.** pmm-server ships its own `location /extensions/`
  block; `PMM_EXTENSIONS_ADDRESS: extensions-sidecar:9000` points it at the side-car, and the
  baked settings profile sets `EXTENSIONS.ROOT_PATH: /extensions` to match. The value is a
  bare `host:port` — a scheme or a trailing path is rejected, and the failure
  surfaces as a routing fault rather than a config error. Nothing is bind-mounted
  over PMM's nginx config.
- **Neither container needs a fixed address.** PMM's PMM Extensions location proxies to a
  *variable* under a location-scoped `resolver ... valid=10s`, which defers
  resolution to request time, so the config loads with the side-car absent and
  picks up a new address within the TTL when it is recreated. `extensions-mysql`
  (`172.28.9.40`) is still fixed, for an unrelated reason: PMM Extensions never updates an
  existing inventory node's address, and the Mydumper path connects to exactly
  that address, so an address that moved across recreates would leave PMM Extensions
  pointing at a stale one.
- `BASE_URL` is `http://extensions-sidecar:9000/extensions` — a compose service name, which
  `../README.md` explicitly tells you not to use because it defines `BASE_URL`
  as the side-car's address *as reachable from Nomad task executors*. That
  prescription is for a real deployment, where PMM Client nodes may sit on a
  network that cannot resolve a compose service name. Here every executor —
  the Nomad client inside `pmm-server` and, with the `mysql` profile up,
  `extensions-mysql` — is a container on the same compose network, so both resolve
  `extensions-sidecar` through Docker's embedded DNS regardless of which one a task
  lands on. The `/extensions` suffix is required either way — download URLs are
  joined onto `BASE_URL`'s path rather than replacing it.

## Caveats

- **`pmm-server` and the side-car are `linux/amd64` only and pin it; the
  executor's platform follows the engine.** No arm64 variant is published for
  the pmm-server or pmm-client feature builds, for the released
  `percona/pmm-server`, or for the side-car image — whose manifest carries no
  platform index at all. Those two pins are what let the services come up on an
  arm64 engine, under emulation, rather than failing the pull; keep them across
  a repin. `extensions-mysql` is different: it is *built*, Oracle Linux 9 ships arm64,
  and so do Percona Server 8.4, XtraBackup 8.4, mydumper and percona-toolkit.
  So on an arm64 engine `bootstrap.sh` writes `EXTENSIONS_MYSQL_PLATFORM=linux/arm64`
  and `EXTENSIONS_MYSQL_PMM_CLIENT_IMAGE=docker.io/percona/pmm-client:3.9.1` into
  `.env`, and the executor builds and runs natively — no Docker Desktop setting
  is involved. It decides by the engine's architecture, not the shell's.

  **Why the executor must not be emulated.** Nomad's `raw_exec` spawns a task
  with `clone3(CLONE_INTO_CGROUP)` only where it places that task into a
  cgroup *at spawn time* — the unified v2 hierarchy's path. On v1 it still
  places the task, by writing PIDs after a plain `clone`, and where it places
  none it spawns with plain `clone` too; neither issues the syscall at all. Go
  has no fallback on the placing path, and QEMU 7.0 and later leave `clone3`
  unimplemented. So what decides whether the syscall matters is the host's
  cgroup layout, and Nomad's own mode detection asks two things of it: the
  unified `cgroup2fs` hierarchy at `/sys/fs/cgroup`, *and* all of
  `cpuset cpu io memory pids` offered in its `cgroup.controllers`.
  What `extensions-mysql` gets is that host layout, not one of its own —
  `privileged: true` with `cgroup: host`, which is what lets the client
  fingerprint and place at all — so where the host answers both, the syscall
  is load-bearing in the container too, and under QEMU every dispatch dies
  inside the executor with `fork/exec /usr/bin/sh: function not implemented`
  before any script output exists — the run shows as failed with an empty log —
  while the node still fingerprints `raw_exec` healthy and stays in PMM Extensions'
  executor list, because that check reads only `enabled = true`. Measured on an
  M3 Pro (2026-09-08): five for five backups and diagnostics failed exactly so,
  and so did PMM Extensions' own background `mysql-sync` and `system-facts-sync`
  dispatches. Verified the same day on another arm64 Mac with a default Docker
  Desktop: as shipped, a diagnostic and an XtraBackup both failed that way; with
  this harness both succeeded on the native executor, the released client having
  reconnected under the registration the feature-build client had created.
  `pmm-server`'s own Nomad client is the same rule seen from the other side — it
  runs as uid 1000, Nomad places no cgroup then, and it spawns with plain
  `clone` — but it remains the non-root, sudo-less node it always was, where the
  builtin snippets that require sudo end `unlaunchable`; it is no substitute for
  `extensions-mysql`.

  **What the released client does and does not stand in for.** Its aarch64
  `tools/nomad` is meant to be the feature build's own Nomad version, and three
  distinct things bear on that. The build compares this client's Nomad to
  `NOMAD_VERSION` — but a released client's Nomad cannot move with `PMM_FB_TAG`,
  so here that comparison only re-confirms a pairing nobody repinned. What
  refuses a stale pin locally is `NOMAD_VERSION_FB_TAG`, naming the feature
  build `NOMAD_VERSION` was read from: a build pinning a different `PMM_FB_TAG`
  is turned away. What establishes that the version is right in the first place
  is CI, which reads the real feature-build client on an amd64 runner
  (`.github/workflows/pmm-fb-nomad-pin.yaml`). It warns and skips in one case
  only: the PR left both `PMM_FB_TAG` and `NOMAD_VERSION` exactly as the base
  branch carried them and that inherited build has since been collected. A tag
  or version the PR itself moved must be verifiable, or the job fails. Its
  `pmm-agent` is the released one, not the feature build's, so a change on the
  client side of the feature build — `pmm-agent`, the Nomad client
  configuration — is **not** exercised on an arm64 engine. Validate those on
  amd64.

  **Running the amd64 feature-build client under emulation instead.** Set
  `EXTENSIONS_MYSQL_PLATFORM=linux/amd64` in `.env` and re-run `./bootstrap.sh`: it
  blanks the client-image slot so the feature-build client is used, and probes
  the emulator for `clone3` before the build, refusing under QEMU. Rosetta is
  the emulation to reach for, since the host kernel serves the syscall rather
  than the translator — though only the QEMU path (refuses) and the native one
  (passes) are measured here. Docker Desktop → **Settings → General**:
  Virtual Machine Manager = *Apple Virtualization framework* (Docker VMM does
  not support Rosetta), then *Use Rosetta for x86_64/amd64 emulation on Apple
  Silicon*, Apply & restart. `extensions-mysql`'s entrypoint probes again at container
  start, since Docker's default seccomp profile also answers `clone3` with
  `ENOSYS` on an unprivileged container. That second probe refuses only where
  the syscall is load-bearing by the two-part test above; on a root that is not
  `cgroup2fs`, or one whose `cgroup.controllers` withholds any of the five, it
  names the condition and starts the node anyway. `EXTENSIONS_FB_SKIP_CLONE3_CHECK=1`,
  in the shell or in `.env`, skips both probes. Either way, treat an emulated
  `pmm-server` as evidence for functional behaviour only: nothing timing-shaped
  survives translation, so the start periods set here and any Nomad scheduling
  race are not measurable on such a host.
- **pmm-server's start period is set by this compose file, not by the image.**
  The image ships 25 s with 3 retries at 4 s, so it is marked `unhealthy` around
  37 s while a cold start needs appreciably longer to first pass `readyz` — and
  because `extensions-sidecar` depends on `service_healthy`, compose aborts the
  dependent instead of waiting. A build-side gate (PMM-15331) once widened this,
  but the PR was closed unmerged, so `compose.yaml` sets a 300 s start period of
  its own. That is the only healthcheck field it sets — Docker merges the rest
  field by field, so the probe and the other timings keep tracking whatever the
  pinned image ships. Keep that override whenever you repin: dropping it
  reintroduces the aborted bring-up, and switching `extensions-sidecar` to
  `service_started` instead trades it for a side-car that exits on a missing
  `SECRET_KEY` when it wins the race.
- **`PMM_ENABLE_EXTENSIONS` unset takes PMM Extensions down**, it does not degrade it. All four
  files are removed on pmm-server's next start, and the side-car exits 1 with a
  single actionable `SECRET_KEY is required` rather than coming up
  half-configured. A side-car already running is unaffected until it restarts,
  because its settings are in memory.
- **`ENCRYPTION_KEY` is the side-car's own, and `pmm-extensions-state` is what keeps it.**
  PMM publishes four files and this is not one of them; the side-car resolves it
  itself, over four channels tried in order — an explicit `ENCRYPTION_KEY`
  variable, a file of that name under `SECRETS_DIR`, a key persisted by an
  earlier start, then a mint. So a first bring-up needs nothing supplied: it
  mints and persists to `pmm-extensions-state`, and later starts re-read that. This harness
  therefore sets nothing, deliberately — an explicit variable wins outright and
  would mean the mint path never runs here, which is the path the embedded
  topology actually uses. It minted its own key on the pin this repin moved to.
  Two properties make it unlike the other secrets. It must be stable across
  restarts and identical across processes: rotating or losing it makes every
  already-encrypted row permanently unreadable, and there is no rotation
  tooling — which is why **dropping `pmm-extensions-state` while a database still holds
  encrypted overrides refuses to start** rather than minting over them. And
  `openssl rand -hex 32`, the generator `SECRET_KEY` uses, produces a value
  Fernet **rejects**; it wants 32 url-safe base64-encoded bytes, so use
  `openssl rand -base64 32` or `make encryption-key` if you supply one by hand.
- Both ports 8443 and 9000-9002 bind to loopback only. `extensions-mysql` publishes
  nothing; it is reachable only on the compose network.
- Under **rootless podman**, `group_add: ["0"]` maps through the user namespace
  differently and may need `--group-add keep-groups`. If the side-car exits
  reporting it cannot read `/run/secrets/extensions`, that is the first thing to check.
- PMM generates the PostgreSQL password on first start and persists it at
  `/srv/.extensions_postgres_password`; set `PMM_EXTENSIONS_POSTGRES_PASSWORD` on pmm-server to
  supply your own instead. Rotating it takes two restarts, in order: pmm-server
  first, so it moves the database password and rewrites the files, then the
  side-car, which reads those files only at process start.
- `extensions-mysql` runs `privileged: true` with `cgroup: host` and a read-write
  `/sys/fs/cgroup` mount. A containerised Nomad client needs it — the
  fingerprinter reads cgroups and `raw_exec` places tasks into cgroups the
  client creates — but it is a harness-only concession, not something the
  side-car topology does.
- The settings mount is gone, so editing a rendered YAML file is no longer a
  way to try something out. Partial overrides are environment-variable-only; a
  full override means bind-mounting over `/home/extensions/app/settings.yaml`, which
  replaces the baked profile wholesale. Add an ignore rule for any local
  filename you mount that is not already in `.gitignore` — such a file holds the
  deployment's secrets in cleartext.
- **Upgrading a harness bootstrapped before PMM took over the secrets:** the
  supported path is `docker compose down -v`, then `./bootstrap.sh` and a fresh
  `up -d`. Dropping the volumes is what gets the secrets volume initialised with
  PMM's `02770` permissions — Docker only initialises a *new* named volume from
  the image, never an existing one. Then delete the leftover `pmm.conf` and
  `settings.yaml`: both are inert now, both hold secrets in cleartext, and both
  stay gitignored only to keep them out of a commit until you do. The retired
  `SEP_SECRET_KEY`, `SEP_INTERNAL_TOKEN` and `SEP_GRAFANA_TOKEN` slots in an
  existing `.env` are inert too — nothing reads them — but delete them for the
  same reason.
- **Upgrading a harness brought up before the PMM Extensions rename:** the
  compose project is now `pmm-extensions-fb`, so the old `sep-pmm-fb` containers
  are not replaced — they keep holding ports 8443 and 9000-9002 — and none of
  their volumes are reused. Stop them
  with `docker compose -p sep-pmm-fb down --remove-orphans` (`--remove-orphans`
  is load-bearing: the current `compose.yaml`'s service names no longer match
  the old containers', so a plain `down` under this project name stops nothing
  and leaves them running), remove its volumes with
  `docker volume rm $(docker volume ls -q --filter label=com.docker.compose.project=sep-pmm-fb)`,
  and delete the `SEP_MYSQL_*` lines from `.env` — `./bootstrap.sh` appends the
  `EXTENSIONS_MYSQL_*` slots that replace them.
