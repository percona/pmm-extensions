# PMM Extensions consolidated side-car image

The side-car image is frontend-less, built for embedding PMM Extensions inside a
PMM deployment. One `supervisord` runs the five side-car programs plus a bundled Valkey
broker under a single PID 1, so the whole product ships as one container with
one log stream.

Build it with `make image` (tag `extensions:${RELEASE_VER}`, no suffix). This is the
only image PMM Extensions ships; Jenkins builds and publishes that one tag to the internal
registry, and to Docker Hub when the build's `pushImageDocker` parameter is set.

The image is **app-restricted**: the app strip is switched on, so it ships only
the app packages the settings profile activates — see [App set](#app-set).

## What it contains

| Input | Role |
|---|---|
| `Containerfile.sidecar` | Final stage; ships the backend only, with no frontend-builder stage, and reuses the shared `extensions:builder` wheel image. |
| `entrypoint.sh` | PID 1. Waits for the `SECRET_KEY` file when PMM has not published it yet, resolves `ENCRYPTION_KEY`, mints the broker credential for the container run, resolves the side-car's Grafana service-account token — waiting for a Grafana that is still starting — then hands off to `supervisord`. See [Starting before PMM is ready](#starting-before-pmm-is-ready). |
| `supervisord.conf` | Runs `valkey`, four `migrate-*` one-shots, the `extensions`/`inventory`/`tasks` APIs, and the Celery worker and beat. |
| `wait_for_api.py` | Run by `supervisord` ahead of the beat command; holds beat until the three APIs answer `/health`, then starts it whatever the outcome. |
| `wait_for_schema.sh` | Run by `supervisord` ahead of each API command; holds the API until all four schema one-shots have published their sentinel, and fails rather than starting it if they do not. |
| `clear_sentinels.sh` | Never run by `supervisord`; an operator runs it before a `supervisorctl` re-run of a schema step, to invalidate that step's sentinel. See [Re-running a schema step inside a running container](#re-running-a-schema-step-inside-a-running-container). |
| `healthcheck.sh` | Aggregate probe wired as the image `HEALTHCHECK`. |
| `settings-env.sh` | Sourced by `entrypoint.sh`; expands the per-deployment inputs into the canonical `__`-nested settings variables, leaving unexported any name a file under `SECRETS_DIR` already supplies. |
| `encryption_key.py` | Run by `entrypoint.sh` before `supervisord`; resolves `ENCRYPTION_KEY`, minting and persisting one only where no service database holds encrypted values — under either the `extensions.enc.v1.` envelope or the bare-token shape predating it. |
| `grafana_service_account.py` | Run by `entrypoint.sh` before `supervisord`; resolves the side-car's Grafana service-account token, minting one when no source supplies it. |
| `runtime.py` | Imported by `encryption_key.py` and `grafana_service_account.py`; resolves `EXTENSIONS_STATE_DIR`, the retry interval and the positive-timeout inputs, and writes their diagnostics. Copied under `sidecar/` rather than beside the two scripts, so their `from sidecar.runtime import ...` resolves when `entrypoint.sh` runs each one standalone. |
| `settings.yaml` | The PMM-embedded settings profile, baked at `/home/extensions/app/settings.yaml`. |
| `restrict_apps.py` | Build-step strip: removes every app package the baked profile does not activate. Deleted in the same `RUN`, so `make image`'s squashed build ships no copy of it. |
| `verify_image_apps.py` | Post-build assertion that an image's app set matches its own baked profile. Piped into the image, never copied into it. |
| `verify_image_apps.sh` | Runs `verify_image_apps.py` inside an already-built image; used by both CI and the Jenkins build. |

The image is built in **docker** manifest format rather than OCI, because OCI
silently discards the `HEALTHCHECK` instruction.

## Runtime configuration

`sidecar/settings.yaml` is baked into the image at
`/home/extensions/app/settings.yaml`, so the container comes up on a working
PMM-embedded profile with no mount. It carries no secrets: the values that vary
per deployment arrive as environment variables, which outrank the file.

**Only one YAML file is ever loaded.** `PreEnvSettings.SETTINGS_FILE` names a
single file and `YamlPrefixConfigSettingsSource` reads only that one — there is
no baked-file-plus-overlay merge. So a **partial** override is
environment-variable-only, and a **full** override is a bind mount at
`/home/extensions/app/settings.yaml`, which replaces the baked profile wholesale.

**Mounted secret files reach this image's settings.** The side-car reads settings from
files in the directory `SECRETS_DIR` names, and `settings-env.sh` consults the
same directory before it derives anything: a canonical name a file supplies is
left *unexported*, so each settings class resolves it from the file. The
deployment inputs in the table below — the `EXTENSIONS_*` names — are shell inputs that
script expands, not settings fields, so none of them is mountable under its own
name. What a file supplies is a *canonical destination*:

| Canonical name | Mountable? |
|---|---|
| `SECRET_KEY` | **Yes.** The gate accepts a file and the script never exports the key, so each process reads it from the file. |
| `ENCRYPTION_KEY` | **Yes.** A file suppresses the mint below it and is never exported, so each process reads it from the file. Mount it only carrying a value: the deferral is on the file *existing*, so a blank one pins the key empty and the container refuses to start. |
| `DATABASE__PASSWORD` | **Yes.** One file supplies all three services. A per-service `{EXTENSIONS,INVENTORY,TASKS}__DATABASE__PASSWORD` file or variable overrides it for that service only. |
| `{EXTENSIONS,INVENTORY,TASKS}__DATABASE__HOST` / `__PORT` | **Yes.** Per-service names; host and port reach every service through the `EXTENSIONS_DB_HOST` / `EXTENSIONS_DB_PORT` shell inputs (see below), not through a global name in this image. |
| `AUTH__PROVIDER__GRAFANA__SERVICE_ACCOUNT_TOKEN`, `PMM__API_KEY` | **Yes.** A file supplies that mint-gate name and suppresses exporting a derived value *over it*. It does not block the Grafana helper from resolving the mounted value and exporting it to every other unset destination among the three. An explicitly-set variable of the same name still wins over the file. |
| `TASKS__NOMAD__API_KEY` | **Yes.** Destination only: a file (or explicit variable) suppresses the derived export for Nomad itself. Mounting a mint-gate token does *not* prevent the other unset destinations — including Nomad — from receiving that export. |
| `PMM__ENDPOINT`, `AUTH__PROVIDER__GRAFANA__ENDPOINT`, `TASKS__NOMAD__ENDPOINT` | **Yes.** A file suppresses the derived export. An explicitly-set variable of the same name still wins over both. |
| `EXTENSIONS_INTERNAL_TOKEN`, `BASE_URL` | **Yes.** Already canonical; the script clears only a blank inherited value and otherwise leaves either alone. |
| `CELERY__BEAT_DBURI` | **Yes.** The script only clears a blank inherited value, which would otherwise outrank the file; the setting itself carries a default derived from the resolved Extensions service database, which a mounted `DATABASE__PASSWORD` or `EXTENSIONS__DATABASE__PASSWORD` outranks. |
| `CELERY__BROKER_URL`, `CELERY__RESULT_BACKEND` | **No.** `entrypoint.sh` mints the bundled Valkey credential per container run and exports both unconditionally, so a file has nothing to supply. |
| The `EXTENSIONS_*` deployment inputs | **No.** Shell inputs, not settings fields. |

The resolution order is: an explicitly-set canonical environment variable, then a
file of that name, then the value derived from the raw `EXTENSIONS_*` input. Within each
of those tiers, a per-service `{EXTENSIONS,INVENTORY,TASKS}__*` name outranks the
unprefixed global spelling of the same destination — so a deployment that sets
both `DATABASE__PASSWORD` and `EXTENSIONS__DATABASE__PASSWORD` always resolves the
per-service value for the Extensions service, regardless of ordering.

**To keep the database password out of the environment, mount one
`DATABASE__PASSWORD` file.** The settings classes resolve that global name for
the Extensions, Inventory, and Tasks services alike. Mount a per-service
`{EXTENSIONS,INVENTORY,TASKS}__DATABASE__PASSWORD` file only when one service needs a
different password. The `EXTENSIONS_DB_PASSWORD` shell input remains
available: it fans out to all three per-service names as environment variables,
which is the path to avoid when keeping the password out of the process
environment.

**Host and port reach every service through the shell inputs, not a global
name.** `settings-env.sh` reads `EXTENSIONS_DB_HOST` and `EXTENSIONS_DB_PORT` (defaulting to
`pmm-server` and `5432`) and derives `{EXTENSIONS,INVENTORY,TASKS}__DATABASE__HOST` and
`__PORT` for all three services; the migrate wait loops read the same inputs.
Mounting `EXTENSIONS__DATABASE__HOST` or `EXTENSIONS__DATABASE__PORT` seeds those inputs before
the defaults apply, so the fan-out still reaches Inventory and Tasks. There is
no global `DATABASE__HOST` mount path in this image: the script exports the three
per-service names unconditionally, and an environment variable outranks every
file. To point one service at a different host or port, set that service's
canonical variable explicitly.

The celery-beat store needs no file of its own: it follows the resolved Extensions
service database, so a mounted `DATABASE__PASSWORD` or `EXTENSIONS__DATABASE__PASSWORD`
reaches it through the same settings resolution the services use. Mount
`CELERY__BEAT_DBURI` only to point beat at a *different* store.

Constraints on the directory: entries must be regular files directly inside it (a
name in a subdirectory matches no setting). File names are matched
case-insensitively, by both the script and the settings classes — but two files
whose names differ **only** in case are an unsupported mount, and which of them
wins is unspecified. A symlink whose target resolves outside the directory is
ignored by both; the Kubernetes `..data` projected layout resolves normally.
`SECRETS_DIR` has no baked default, so a side-car that mounts nothing is
unaffected.

Leaving a mounted name **unset** and leaving it **empty** both work: a
canonical variable inherited as the empty string would otherwise outrank the
file, since a blank environment variable still counts as supplied, so the
script clears the blank for every canonical name it manages — `SECRET_KEY`,
every name it derives, and every name an `EXTENSIONS_*` guard would otherwise leave
untouched when that guard is inactive (`PMM__API_KEY` with no
`EXTENSIONS_GRAFANA_TOKEN` set, say). `ENCRYPTION_KEY` is cleared on the narrower
condition that a file of that name is also mounted, since that is the only case
where a blank would shadow something; `entrypoint.sh` overwrites a surviving
blank on every other path.

### App set

The app-restricted image ships exactly the apps `settings.yaml`'s
`EXTENSIONS.APPS` activates, plus `framework` and `shared`, which shipped modules reach
and which the activation list never names. Nothing else declares the set: changing
which apps the image ships is an edit to `EXTENSIONS.APPS` and nothing else, and the
build fails if an activated app has no package to keep.

The strip is driven by the `EXTENSIONS_RESTRICT_APPS` build argument, which `image`
passes as `1`. Only the exact value `1` strips anything; the argument defaults
to `0`, and any other value leaves the image unrestricted.

The set is asserted on the **published artifact**, not on a rebuild of it:
`verify_image_apps.sh` pipes `verify_image_apps.py` into an already-built
image's own interpreter, and both CI and the Jenkins build call it before
anything is pushed. The check re-derives the expected set from the profile the
image itself bakes rather than importing `restrict_apps.py` — which the build
deletes from the image anyway, and which could only ever agree with the tree it
produced. A run prints the set it verified, because a checker that silently
never ran would otherwise be indistinguishable from a passing one.

The strip removes an app's directory and leaves its `version_locations` entry in
`alembic.ini` alone. That combination is load-bearing, not incidental. Each app
owning migrations is an independent Alembic branch recorded in the shared
`alembic_version_extensions` table, so a database a full image migrated carries head
rows for apps this image does not ship. `skip_unresolvable_heads` in
`app/extensions/migrations/_orphan_heads.py` drops those rows from the heads it hands
Alembic — but only when a configured `version_locations` entry contributes no
revisions (absent from disk or present and empty), which is what a stripped
app looks like. With every configured location present and populated, an
unresolvable revision means version skew instead and the upgrade hard-fails by
design. So pruning a stripped app's entry — regenerating the list in an
already-stripped tree, or rewriting the file during the build — turns a
working upgrade into a failed one. `tests/sidecar/test_app_strip.py` asserts the
entries survive for every app the strip removes that owns migrations; an app
owning none needs no entry, and must not carry one.

On this image an `EXTENSIONS.APPS` override can therefore only **narrow** the baked
set, never widen it. Registry construction imports each activated module, so
activating a package the image does not ship raises a pydantic `ValidationError`
(wrapping `No module named app.extensions.apps.<name>`) and the container fails to
start. The two surfaces that reach `EXTENSIONS.APPS` are a bind
mount at `/home/extensions/app/settings.yaml` (which, per above, replaces the profile
wholesale — so its `EXTENSIONS.APPS` must be a subset of the baked one) and the
`EXTENSIONS__APPS` environment variable; the runtime settings-override API cannot,
because `EXTENSIONS.APPS` is absent from `SETTINGS_OVERRIDE.ALLOWED_KEYS`.

### Deployment inputs

Expanded by `settings-env.sh` into the canonical settings variables. The `EXTENSIONS_*`
names are shell inputs rather than settings fields, so none of them is mountable
under its own name — the canonical destinations they expand to are.
`SECRET_KEY` and `ENCRYPTION_KEY` are the exceptions, being already canonical:
each is both an input here and mountable. See the note under
[Runtime configuration](#runtime-configuration):

| Input | Required | Default | Canonical destinations |
|---|---|---|---|
| `SECRET_KEY` | **yes** | — (waits for the file when `SECRETS_DIR` is set, otherwise fails fast) | already canonical (global `Settings`, no prefix) |
| `ENCRYPTION_KEY` | no | minted into `pmm-extensions-state` when the databases are provably fresh | already canonical (global `Settings`, no prefix) |
| `EXTENSIONS_DB_PASSWORD` | yes in practice | none | `EXTENSIONS__DATABASE__PASSWORD`, `INVENTORY__DATABASE__PASSWORD`, `TASKS__DATABASE__PASSWORD` |
| `EXTENSIONS_DB_HOST` | no | `pmm-server` | `EXTENSIONS__DATABASE__HOST`, `INVENTORY__DATABASE__HOST`, `TASKS__DATABASE__HOST`, and the three supervisord wait loops |
| `EXTENSIONS_DB_PORT` | no | `5432` | same as `EXTENSIONS_DB_HOST` |
| `EXTENSIONS_GRAFANA_TOKEN` | no | none | `AUTH__PROVIDER__GRAFANA__SERVICE_ACCOUNT_TOKEN`, `PMM__API_KEY`, `TASKS__NOMAD__API_KEY` |
| `EXTENSIONS_PMM_ENDPOINT` | no | `https://pmm-server:8443` | `PMM__ENDPOINT`, `AUTH__PROVIDER__GRAFANA__ENDPOINT` (with `/graph` appended) |
| `EXTENSIONS_NOMAD_ENDPOINT` | no | the profile's credential-free URL | `TASKS__NOMAD__ENDPOINT`. The address only — the executor's credential is `TASKS__NOMAD__API_KEY`, which the Grafana fan-out above supplies |

`SECRET_KEY` is the only input with no fallback of any kind: one has to be
supplied, as an environment variable or as a mounted file. With `SECRETS_DIR`
unset, the container exits at once without one. With it set, the container
waits for the file instead — see
[Starting before PMM is ready](#starting-before-pmm-is-ready).
(`ENCRYPTION_KEY` comes close, and its fallback is conditional: see
[The encryption key is minted, but only onto a fresh deployment](#the-encryption-key-is-minted-but-only-onto-a-fresh-deployment).)
It signs the
framework's cookies and CSRF tokens and, when
`EXTENSIONS_INTERNAL_TOKEN` is unset, derives that token by HMAC, so it has to be both
identical across the supervisord children and stable across restarts. The class
default (`secrets.token_urlsafe(32)`) satisfies neither: it is evaluated per
process, so each child would resolve a different key. Minting one per container
run the way the bundled Valkey credential is minted would fix that and still
break the second half — every session would be signed out and the inter-service
token would rotate on each restart. Generate it once per deployment, persist it
alongside the other deployment secrets, and either pass it in or mount it as a
file named `SECRET_KEY` under `SECRETS_DIR` — the mount keeps it out of every
process's environment.

`EXTENSIONS_GRAFANA_TOKEN` is optional, and the container no longer stays inert without
it: two further sources sit below it — a token the side-car persisted on an earlier start,
and one it mints against Grafana. See
[The Grafana token is minted, not required](#the-grafana-token-is-minted-not-required).
Supplying either canonical name, here or as a file under `SECRETS_DIR`, skips
minting altogether. The profile ships an empty `service_account_token`, which is
a valid `SecretStr`, so a *misspelled* token still yields a silently inert
provider rather than a startup error.

Read by that mint step rather than expanded into a canonical name, so none of
them is mountable and all are optional:

| Input | Default | What it controls |
|---|---|---|
| `GF_SECURITY_ADMIN_USER` | `admin` | The Grafana admin login the mint authenticates as. Used only when the side-car holds no valid token. |
| `GF_SECURITY_ADMIN_PASSWORD` | `admin` | Its password. A blank value falls back to the default rather than being sent empty. |
| `EXTENSIONS_STATE_DIR` | `/home/extensions/state` | Where the minted token is persisted. Mount a volume here to carry it across a container recreate. |

Already canonical, so they are passed straight through with no expansion:

| Input | Required | Notes |
|---|---|---|
| `EXTENSIONS_INTERNAL_TOKEN` | no | Authenticates internal service-to-service calls, such as the scheduled inventory sync. Derived from `SECRET_KEY` by HMAC when unset, so every process sharing the key resolves the same token. Set it explicitly only to rotate it independently of `SECRET_KEY`. |
| `BASE_URL` | no* | The side-car's address as reachable from Nomad task executors, including its URL prefix — `https://pmm-server:8443/extensions`, not `https://pmm-server:8443`. Download URLs are joined onto its path rather than replacing it, so a value omitting the prefix yields a well-formed URL that no longer routes to PMM Extensions; startup warns when it does. *Required when tasks download scripts or artifacts. |

Any canonical variable can also be set directly — an explicit
`TASKS__DATABASE__HOST` outranks the one derived from `EXTENSIONS_DB_HOST`. It overrides
only itself, though: setting `EXTENSIONS__DATABASE__PASSWORD` by hand leaves the other
two services on whatever `EXTENSIONS_DB_PASSWORD` supplied, so prefer `DATABASE__PASSWORD`
(or the `EXTENSIONS_DB_PASSWORD` input) when you want one password to reach every
service. `CELERY__BEAT_DBURI` needs no input at all: it defaults to the resolved
Extensions service database connection, so set it explicitly only to keep the
beat schedule in a store separate from that database.

### Not deployment inputs

| Setting | Why it is fixed |
|---|---|
| Database user and name | PMM's `PMM_ENABLE_EXTENSIONS` provisions exactly the `pmm_extensions` role and database. |
| Celery broker and result-backend URLs | Minted per container start — see below. |
| Uvicorn hosts and ports | `healthcheck.sh` probes loopback `:9000`/`:9001`/`:9002`, so they are image contract. |
| TLS certificate and key files | TLS is off inside the container; the probe speaks plain HTTP on loopback and PMM's nginx terminates TLS. |

### The broker credential is generated, not configured

`entrypoint.sh` mints a random password per container start, writes it into a
mode-`0600` Valkey config at `/tmp/valkey.conf`, and exports
`CELERY__BROKER_URL` / `CELERY__RESULT_BACKEND` carrying it. Environment
outranks the baked `settings.yaml`, so the password-less
`redis://127.0.0.1:6379` the profile carries keeps working unchanged; the
exported value wins. It also supersedes a `CELERY__BROKER_URL` passed to
`docker run`, since only the generated credential opens the bundled broker —
this is the one input the deployment-input table above deliberately excludes.
Nothing external supplies the credential and nothing needs to know it.

The password never reaches the command line, of either `valkey-server` or the
`healthcheck.sh` probe (which reads it back from the config file and passes it
through `REDISCLI_AUTH`), because argv is readable by every process in the
container's PID namespace. A container restart mints a fresh one, which is safe:
the broker runs with `save ""` and `appendonly no`, so no broker state crosses
restarts.

### The encryption key is minted, but only onto a fresh deployment

`ENCRYPTION_KEY` keys the Fernet ciphertext the side-car stores in its
`settingoverride` rows, so every secret-typed override — an alert provider's
routing key, a delivery input's API key — is readable only under the key that
wrote it. Four channels supply it, tried in order:

1. An explicit `ENCRYPTION_KEY` environment variable. Wins outright.
2. A file named `ENCRYPTION_KEY` under `SECRETS_DIR`. Never exported, so each
   process reads the file.
3. A key a previous start persisted at `$EXTENSIONS_STATE_DIR/ENCRYPTION_KEY`.
4. A freshly minted key, persisted mode `0600` at that same path.

Only channels 1 and 2 are the operator's; `entrypoint.sh` runs
`encryption_key.py` for the other two, and only when neither of the first two
supplied anything. Mount a volume at `/home/extensions/state` for a minted key to
survive a container *recreate* — without one, the key is lost with the
container, and with it every override encrypted under it.

Minting is guarded, because minting the wrong key is silently destructive
rather than loud. A row the configured key cannot decrypt is logged and
skipped, not treated as an error, so the setting falls back to its YAML value
and the container comes up green with the operator's configuration quietly
reverted. The state volume and the databases have independent lifecycles — the
databases live in pmm-server's postgres, the state volume belongs to the
side-car — so recreating the side-car against a surviving database is an
ordinary path, not an exotic one.

So before minting, the helper reads the `settingoverride` table in all three
service databases (`extensions`, `inventory` and `tasks`, whose endpoints may differ),
walking each stored value's JSON *leaves* rather than the row — the ciphertext
sits inside lists and nested mappings, where a check against the row's own
value finds nothing. It mints only if none of the three holds ciphertext under
either at-rest shape: a leaf carrying the `extensions.enc.v1.` envelope marker, or a
bare Fernet token from before that envelope shipped. Testing only the bare shape
would read a deployment whose overrides were all written under the envelope as
holding none, because the marker's leading `.` puts the value outside base64 and
so outside the structural check. Anything else refuses: ciphertext found, or a
value it cannot parse — freshness unproven is treated exactly like freshness
disproven.
The probe runs *only* on the mint path, so an ordinary restart opens no database
connection and pays no startup latency.

A database that does not answer is **waited on**, without a bound, because a
first start routinely runs while pmm-server's postgres is still coming up — the
same condition the supervised migration steps wait out. Each attempt is given up
after 30 seconds and retried 3 seconds later, and an `[encryption-key]` line names
the database and the last error at most every 30 seconds. The key is minted once
all three databases have answered and passed the check, and passed it again in a
second pass right after: a side-car that already holds the key may have written to
a database that passed while this one waited for another. A database that answers
with a fault no wait clears, such as a rejected password, is waited on in the
same way, with that fault in the line. `EXTENSIONS_ENCRYPTION_PROBE_TIMEOUT` is
not read; a start that has to mint, with it set, logs one line saying so.

Two containers sharing one state volume serialise on a lock in it rather than
mint beside each other. A start waits up to 120 seconds for a peer that holds
the lock, then refuses and exits. A peer that is itself waiting for the
databases holds the lock until they answer.

Note that a minted key is exported into every supervised program's environment,
where a key mounted under `SECRETS_DIR` deliberately is not. Mount the key
instead of letting it be minted if that difference matters to you. A mounted key
stays where you mounted it under the mode you gave it; only a minted key is
written to the state directory, at `0600`.

A refusal that found unreadable data names the state path to restore. **Losing
the key is unrecoverable:**
there is no way to read those values back without it, and the only remedies are
restoring the key from a backup or deleting the affected overrides so they can
be re-entered.

Generate one by hand with `make encryption-key`, or:

```bash
openssl rand -base64 32
```

Use `-base64`, not `-hex`: a hex string is 64 characters and Fernet requires 32
bytes of URL-safe base64, so a hex key is rejected. The output of `-base64` will
usually contain `+` and `/` rather than the `-` and `_` of the URL-safe alphabet
Fernet's own documentation shows. That is fine and not worth "fixing" — the
decoder translates `-_` to `+/` and passes `+/` through untouched, so both
alphabets are accepted.

### The Grafana token is minted, not required

`EXTENSIONS_GRAFANA_TOKEN` is the last value an operator supplies. Below it,
`entrypoint.sh` runs `grafana_service_account.py` once, before supervisord, and
feeds its answer through the same `export_grafana_token` the `EXTENSIONS_GRAFANA_TOKEN`
guard uses. That helper fills each unset destination among the Grafana
provider's `AUTH__PROVIDER__GRAFANA__SERVICE_ACCOUNT_TOKEN`, the PMM client's
`PMM__API_KEY`, and the Nomad executor's `TASKS__NOMAD__API_KEY`; an explicit or
mounted value already present for a name is left alone. When every destination
was empty, all five programs therefore inherit one resolved value and nothing in
the application copies one setting into the other.

`TASKS__NOMAD__API_KEY` is what lets the executor reach PMM's `/nomad/` location,
whose server-level `auth_request` the embedded profile's credential-free endpoint
cannot otherwise satisfy. The executor sends it as
`Authorization: Bearer <key>`, and it takes precedence over any `user:password`
embedded in `TASKS__NOMAD__ENDPOINT`: while a key is set the endpoint's userinfo
is stripped, because both HTTP clients would otherwise derive basic auth from it
and override the header.

The helper skips minting when either the Grafana service-account token or
`PMM__API_KEY` already resolves, from an explicit variable or from a file under
`SECRETS_DIR`, or when the active auth provider is not Grafana. A blank value
counts as absent at every rank the helper reads.

**Those two names are the mint gate only.** Supplying either of them suppresses
minting, but `entrypoint.sh` still calls `export_grafana_token` with the
already-resolved value, so every unset destination among the three — the sibling
mint-gate name and `TASKS__NOMAD__API_KEY` — is set to the same credential
without a fresh Grafana request. When that value came from a `SECRETS_DIR`-mounted
mint-gate file, the sibling has no value and no file of its own, so it takes the
derived export too: mounting only
`AUTH__PROVIDER__GRAFANA__SERVICE_ACCOUNT_TOKEN` leaves `PMM__API_KEY` and
`TASKS__NOMAD__API_KEY` both set to the mounted value, and mounting only
`PMM__API_KEY` does the mirror image. That turns on the PMM client and PMM
annotations where a mount-only mint-gate deployment previously got neither; it
also means a mounted credential reaches every supervised program's environment
under names it was not mounted as — the reason the `ENCRYPTION_KEY` path never
exports a file-supplied value. When both mint-gate names resolve to different
values, `AUTH__PROVIDER__GRAFANA__SERVICE_ACCOUNT_TOKEN` wins.
`TASKS__NOMAD__API_KEY` is a destination only: mounting *it* alone does not
suppress minting, and an explicit or mounted Nomad key is left alone by the
export. A non-Grafana deployment mints nothing and must set
`TASKS__NOMAD__API_KEY` itself if its Nomad requires a credential.

One caveat on the rank above it: `settings-env.sh` defers to a `SECRETS_DIR` file
on the file *existing*, not on it holding a value, because the settings source
resolves an empty secret file to the empty string rather than falling through. So
a mounted-but-empty file named for any of the three pins that name to the empty
string, and a token minted below it cannot displace it. Mount a file only when it
carries a value; to leave a name to the mint, do not mount it at all.

Otherwise it finds or creates a service account named `pmm-extensions` with the `Admin` org
role and asks it for a non-expiring token, authenticating as Grafana's admin.
Lookup is by that fixed name, so repeated starts leave Grafana with one `pmm-extensions`
account rather than one per start. The minted token is written mode `0600` to
`$EXTENSIONS_STATE_DIR/grafana_service_account_token` and re-read on the next start:
the admin credential is what minting needs, not what restarting needs. Mount a
volume at `/home/extensions/state` for the token to survive a container *recreate* as
well. Without one, every recreate mints a further token on the same account, and
because minted tokens are asked not to expire, the ones earlier containers
resolved stay valid in Grafana until an operator deletes them.

A persisted token is revalidated with one short, bounded call. Rejected, it is
re-minted and the file replaced; accepted, it is used as-is and nothing is
minted; and when Grafana cannot be reached it is used unvalidated, without
waiting out the retry bound, because an outage must never retract a working
credential.

Only the states holding no token wait: a first start, and a re-mint after an
actual rejection. That wait has no bound — see
[Starting before PMM is ready](#starting-before-pmm-is-ready).

A fault no wait can clear is handled differently. A rejected admin credential,
an endpoint that does not carry Grafana's API, or a Grafana certificate that
fails verification is reported once in a `[grafana-mint]` line and not retried.
Repeating a failed admin sign-in can lock that account in Grafana, and none of
the three changes by waiting. The five programs then start with sign-in and
the PMM syncer inert, and the container reports unhealthy — see
[Health](#health) — until it is started again with a credential that works.

`GF_SECURITY_ADMIN_*` is unset before `exec supervisord`, so the admin pair
reaches the mint step and nothing else: it is more privileged than the token it
mints, and no supervised program reads it.

Like the broker credential, the token reaches the programs through the process
environment, so it appears in no image- or compose-declared environment and
therefore in no `docker inspect` output. Unlike a mounted secret it is readable
through `/proc` by processes running as the same user in the container;
`SECRETS_DIR` is not available as an alternative, being PMM-owned and mounted
read-only, so the side-car cannot write the file its own settings source reads.

Two limitations follow from minting through Grafana's admin API:

- **The admin credential is needed whenever the side-car holds no valid token, not only on
  first boot.** A Grafana volume wipe, or a restore predating the service
  account, forces a re-mint. If the admin password was changed through
  `change-admin-password` rather than through the environment, the value the
  side-car holds is stale and the re-mint fails. Set `GF_SECURITY_ADMIN_PASSWORD` and give
  the side-car the same value; relying on the `admin`/`admin` default means the
  side-car can mint
  only until that password is first changed.
- **A Grafana with admin-user bootstrap disabled cannot be minted against at
  all.** Supply `EXTENSIONS_GRAFANA_TOKEN` on such a deployment, or mount either
  canonical name under `SECRETS_DIR`.

### Starting before PMM is ready

A Compose file that starts the side-car with a plain `depends_on: pmm-server`
starts it when the `pmm-server` container has started, not when PMM is healthy.
The side-car copes with that by itself: `entrypoint.sh` waits for the things
PMM supplies while it starts, says what it is waiting for, and hands off to
`supervisord` only when it has them.

| It waits for | When | What the container log says |
|---|---|---|
| The `SECRET_KEY` file | `SECRETS_DIR` is set, and neither the environment nor a file under it supplies a key | `[entrypoint] Waiting for PMM to publish SECRET_KEY under <directory>.`, every 10 seconds. When the file is there and the side-car cannot read it, the line says so instead and names the uid and groups it runs as |
| The three service databases in PMM's PostgreSQL | The start has to mint `ENCRYPTION_KEY`: no key is supplied by the environment, a file under `SECRETS_DIR` or the state directory | `[encryption-key] Waiting for the <service> database to answer before the ENCRYPTION_KEY is minted (last error: …).`, at least 30 seconds apart |
| A Grafana that can mint the token | The start has to mint: no token is configured or mounted, and none that Grafana still accepts is persisted | `[grafana-mint] Waiting for Grafana at <endpoint> to accept the service-account mint (last answer: …).`, at least 30 seconds apart |

None of these waits has a bound. PMM owns the key, its PostgreSQL and Grafana, so a bound could
only give up on a slow PMM, and the five programs read the token once, from
`supervisord`'s environment: a token minted after they started could not reach
them without a restart. The schema one-shots wait for PMM's PostgreSQL the same
way. While the entrypoint waits, the container's health is `starting` through
the `HEALTHCHECK` start period and five failed probes after it, about three and
a half minutes in all, and `unhealthy` from then on. It turns healthy without a
restart when the wait ends. A stop signal during any of these waits ends the
entrypoint within a second, with exit status 143.

The Grafana wait covers what a server that is still starting answers: a refused
or timed-out connection, and HTTP 500, 502, 503 or 504. A certificate that fails
verification is not among them, and ends the token step at once. Each attempt is
given up after 30 seconds and retried 3 seconds later. A start that already
holds a token does not wait: a configured or mounted token skips the mint, and a
persisted token is used unvalidated while Grafana is unreachable.

When the key file appears, the gate reads the directory once more a second
later before it continues. PMM publishes the key and the database passwords as
consecutive renames with the key first, so the moment the key appears is the one
moment the other files may still be missing.

`EXTENSIONS_GRAFANA_MINT_TIMEOUT` is not read. A deployment that sets it gets
one `[grafana-mint]` line saying so.

A start that has to mint `ENCRYPTION_KEY` waits for the service databases the
same way, and a stop signal during that wait ends the entrypoint the same way —
see
[The encryption key is minted, but only onto a fresh deployment](#the-encryption-key-is-minted-but-only-onto-a-fresh-deployment).

## What the settings API will and will not change

The image bakes `SETTINGS_OVERRIDE.ALLOWED_KEYS` — the exhaustive list of
settings an administrator may change from the settings UI or API. Everything
this container provisions is refused with `422`: the loopback endpoints and
ports from the table above, the PMM connection and its API key, the whole Nomad
subtree, the snippets source, sessions, security headers, and auth. What stays
tunable is product behaviour: log level, PMM annotations, sync cadence, the
footer and message-level display options, alerting policy and retention,
anonymizer entities, the diagnostics-delivery inputs, and the task
connectivity-check and log-retention settings.

Diagnostics delivery is the one tunable that is off until you configure it. The
image bakes the receiver plan — its resolution steps, its upload spec, and the
*names* of the credentials it needs — but ships those credentials empty, so no
bundle leaves the container until an operator supplies them through
`ExtensionsSettings.DIAGNOSTICS_DELIVERY_INPUTS`. That is a single whole-object PATCH
carrying every declared secret name at once: a per-leaf write such as
`DIAGNOSTICS_DELIVERY_INPUTS__secrets` is refused with `422`, and so is a
payload naming a secret the baked plan does not declare or omitting one it
does. The same key optionally carries an `endpoint` that replaces the baked
receiver; omit it to keep the shipped one. Stored secrets read back as
`**********`, and resubmitting that mask preserves the stored value.

If an image upgrade ships a plan that renames a declared secret, the inputs you
stored against the previous plan stop matching it. Delivery does not silently
fall back to the never-configured state: the Send action, the send endpoint, and
any send that does get dispatched all report that the stored inputs no longer
match this deployment's plan and must be re-supplied. Nothing guesses which old
name maps to which new one — PATCH the key again, naming the secrets the new
plan declares.

Rows written before the restriction applied — by a standalone deployment whose
database was carried over, or by direct table access — are **inert**: the
snapshot builder skips them, so the baked value is what the services read. They
remain deletable through `DELETE /settings/<class>/<key>`, which is how an
operator clears one; deleting a locked key that has no row answers `409`
instead, since there is nothing to remove.

`SETTINGS_OVERRIDE.ALLOWED_KEYS` is a general capability, not a side-car
special case: any deployment can set it (bare env var
`SETTINGS_OVERRIDE__ALLOWED_KEYS`, or a nested `SETTINGS_OVERRIDE:` block in
`settings.yaml`) to harden its own override surface. Leaving it unset — the
default everywhere else — keeps every overridable setting overridable. It can
never be changed through the API, only through the deployment's own
configuration. This image carries it under `SETTINGS_OVERRIDE:` in
`settings.yaml`, so the bind mount that replaces that file is what
changes the list. A replacement that omits the key does not preserve the
shipped list — it lifts the restriction entirely, since an absent key reads the
same as a deployment that never set one. An environment variable of the same
name still outranks the profile, as it does for every other setting here.

## Volumes

Nothing the image ships can be changed by the `extensions` user every process runs as.
`/home/extensions` and `$APP_HOME` are both `0750 root:extensions`, so `extensions` can
neither rename nor delete an entry in them nor create a new one, and nothing under `$APP_HOME`
is writable by group or other. That holds with the supplementary group 0 too, which PMM's
compose file adds so the side-car can read the secrets PMM writes for that group. The one
exception is `$APP_HOME/var` (`0700 extensions:extensions`), where the Celery worker keeps its
state file (`CELERY.WORKER_STATE_DB`) and the `atw` app stages a diagnostics bundle while it
uploads (`EXTENSIONS.ATW.bundle_dir`); it lives in the container and is lost when the container
is recreated. Any other path a program writes under `$APP_HOME` at run time needs a writable
volume mounted there — at minimum `EXTENSIONS.artifact_dir`, which defaults to
`data/health-reports`, for a deployment that enables the `report` app:

```
-v report-artifacts:/home/extensions/app/data/health-reports
```

`/home/extensions/state` is the one path the image creates for the side-car to write its own
files into (`0700 extensions:extensions`). It holds the minted
Grafana token and the minted `ENCRYPTION_KEY`; mounting it is what makes both
survive a container recreate rather than only a restart.

**Mount it.** The two have very different stakes. A lost Grafana token is
re-minted on the next start at no cost; a lost `ENCRYPTION_KEY` is
unrecoverable, and every setting override encrypted under it becomes
unreadable. See
[The encryption key is minted, but only onto a fresh deployment](#the-encryption-key-is-minted-but-only-onto-a-fresh-deployment)
— on a deployment that already holds encrypted values, recreating without this
volume does not start the container at all.

```
-v pmm-extensions-state:/home/extensions/state
```

## Health

`healthcheck.sh` exits 0 only when the entrypoint has resolved the Grafana
token, every non-one-shot program is `RUNNING`, all
four `migrate-*` one-shots have written their `/tmp/migrate-<step>.ok` sentinel,
the three `/health` endpoints return 200, and the bundled Valkey answers `PING`.
The sentinels matter because a failed `alembic upgrade` ends in `EXITED` — the
same state a successful one reaches — so program state alone cannot distinguish
them.

The same four sentinels gate the API programs: each runs `wait_for_schema.sh extensions
inventory tasks beat` before `exec`-ing its app, so no API starts against a
database whose schema has not been applied. The gate is uniform — `inventory`
reads no beat table, but container health already requires all three APIs to
answer, so gating them alike costs nothing and cannot drift out of step with
which service seeds what. A step that never completes holds the APIs for
`WAIT_BUDGET_SECONDS` (300) and then fails them — and because they keep
`autorestart=true`, each one restarts into a fresh gate rather than stopping, so
`supervisorctl status` shows the three APIs cycling every five minutes for as
long as the sentinel is missing. The container is reported unhealthy well before
the first budget expires, because its one-shot's sentinel is already missing.

Each one-shot waits for its own store without a bound — the three alembic steps
in the shell, `migrate-beat` inside its bootstrap, against whatever
`CELERY__BEAT_DBURI` resolves to. None of them is re-run once it exits, so a
step that gave up could never publish its sentinel and would hold every gated
program for the life of the container; waiting instead means the sentinel still
lands whenever the store appears.

The token assertion reads `/tmp/grafana-token.ok`. `entrypoint.sh` removes that
file at every container start, and writes it when the token step ends with a
token, or with nothing to resolve because the auth provider is not Grafana.
Settings the token step could not read count as neither, so the file is not
written. It is the probe's first assertion, so `docker inspect` shows
`unhealthy: PMM Extensions holds no Grafana service-account token; …` in both
states it covers: an entrypoint still waiting for PMM, and programs that started
after a mint that failed for good. It does not detect a token that is held but
empty — a mounted credential file with no value, described under
[The Grafana token is minted, not required](#the-grafana-token-is-minted-not-required).

An API still inside its gate reports `RUNNING`, so the program-state assertion
is weaker than it reads for those three — the `/health` probe is what keeps a
healthy container meaning the APIs answer. `entrypoint.sh` clears the four
sentinels before any program is spawned, so a *container* restart cannot release
a gate on the previous run's markers. A `supervisorctl` restart does not re-enter
it; see below.

`HEALTHCHECK` is configured `--interval=15s --timeout=15s --start-period=150s
--retries=5`, so a program going down surfaces as an unhealthy container after
roughly 75-80s.

### Re-running a schema step inside a running container

To re-apply one schema step without restarting the container, clear its sentinel
first, then restart that one-shot together with the API programs:

```
docker exec <container> /home/extensions/app/clear_sentinels.sh extensions
docker exec <container> supervisorctl -c /home/extensions/app/supervisord.conf \
    restart migrate-extensions extensions inventory tasks
```

If the step owns tables Celery reads, **stop the Celery programs before that
clear** and start them again only once `/tmp/migrate-extensions.ok` has reappeared:

```
docker exec <container> supervisorctl -c /home/extensions/app/supervisord.conf \
    stop celery-worker celery-beat
# ... the clear and restart above ...
docker exec <container> supervisorctl -c /home/extensions/app/supervisord.conf \
    start celery-worker celery-beat
```

**Clear first, then restart.** `supervisorctl` starts the programs it is given
one at a time in argument order, and each API blocks for its `startsecs` (8s)
before the next is started — so without clearing, whether a restarted API's gate
reads the previous run's sentinel depends on argument order and on how quickly
the one-shot reaches the `rm -f` at the head of its own command. Once the
sentinel is cleared there is nothing stale left to observe and the order stops
mattering.

**Name every step you are re-running, in one call** — `clear_sentinels.sh extensions
tasks`, then `restart migrate-extensions migrate-tasks extensions inventory tasks`. The script
takes the bare step names the gate takes, not `supervisord` program names:
`migrate-extensions` is refused.

**Continue only if the clear exits 0.** A non-zero exit on an unrecognized name
has removed nothing at all, so fix the name and re-run. A non-zero exit from `rm`
is different: it stops at that step, so the steps named before it are already
cleared while it and every step named after it may still hold a marker. Those
cleared markers have no re-run coming, and `healthcheck.sh` asserts all four, so
the container stays unhealthy until they are republished — resolve whatever
blocked the `rm`, then re-run the clear and restart the one-shots for every step
it had already cleared, or restart the container, which clears and re-runs all
four through PID 1.

**Restart the one-shot together with the API programs.** An API restarted
without its one-shot waits out the 300s gate budget and then cycles, as above; an
API left unrestarted keeps running and never re-enters its gate, so it goes on
serving against the schema it started with. The APIs are safe to name in the same
call as the one-shot precisely because each re-enters `wait_for_schema.sh` and
holds there until the sentinel is republished.

**Stop Celery before the clear, start it after the sentinel — never name it in
the restart.** `celery-worker` carries no schema gate at all, and `celery-beat`
is gated only on the APIs answering, so neither re-checks the schema on its own.
That cuts both ways. A worker left running consumes tasks against the tables
throughout the re-run, which is why it is stopped up front rather than merely
restarted at the end. And naming it in the restart call would start it too early:
supervisord flips a gated API to `RUNNING` once its `startsecs` (8s) elapses even
though its shell is still inside the gate, so supervisorctl would reach the
Celery programs roughly 24 seconds in — while a slow or failed migration is still
running — and the ungated worker would begin consuming tasks against the
incomplete schema. Queued tasks are held in the broker while Celery is stopped,
so nothing is lost; in-flight ones are interrupted by the stop, exactly as they
would be by a restart.

**The container reports unhealthy until the step republishes its sentinel**,
because `healthcheck.sh` asserts all four. A re-run taking longer than roughly
75-80s therefore surfaces as an unhealthy container until it finishes.

**Expected output.** `migrate-extensions: ERROR (not running)` from the stop half is
normal for a one-shot that has already exited, and leaves the command's exit
status unchanged. The one-shot's own `started` or `ERROR (abnormal termination)`
line is not its verdict either — a step that exits promptly is reported that way
whether it succeeded or failed. Nor is `supervisorctl`'s own exit status: the
start half *does* fault on `ERROR (abnormal termination)`, so the command exits
non-zero on exactly the line above that you are being told to ignore. Do not gate
a script on `$?` here. The sentinel is the verdict, exactly as it is for the
healthcheck.

`-c /home/extensions/app/supervisord.conf` is passed explicitly, as `healthcheck.sh`
does. `docker exec` runs as the image's `extensions` user, which owns both the sentinels
and the `0700` supervisor socket, and needs nothing from PID 1's exported
environment.

## Deployment caveat

The bundled Valkey binds `127.0.0.1:6379` inside the container's own network
namespace. If the side-car is ever deployed sharing a namespace with PMM (a
Kubernetes pod, or `--network container:pmm-server`), that port collides with
PMM's own Valkey, and Celery then fails to reach its own broker. Give the
side-car its own network namespace — the generated broker URL is not
deployer-overridable, so re-pointing Celery elsewhere is not an option.

Authentication is what keeps that shared-namespace case from also being an
exposure: the loopback bind is no boundary there, so the broker requires the
generated `requirepass` credential described above rather than relying on the
bind alone. The collision remains — a neighbouring process cannot use the
broker, but it can still occupy the port first.
