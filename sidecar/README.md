# The sidecar image

PMM Extensions ships as one image, which runs as a sidecar beside PMM Server. This page is the reference for that image. It lists what the image contains, the inputs it reads and the settings an administrator may change. It also lists the image's volumes, its health check and its ports.

For why the image works this way, see [How the sidecar runs inside PMM](../docs/explanation/architecture.md). To run it, see [Run PMM Extensions in a local PMM](../docs/how-to/run-in-local-pmm.md).

## Image

| Property | Value |
|---|---|
| Published name | `percona/pmm-extensions` on Docker Hub |
| Tags | The full version, such as `3.10.0`, and the moving tags `3.10`, `3` and `latest`. The version is PMM's. |
| Architecture | amd64 |
| Local build | `make builder`, then `make image`. The result is tagged `extensions:<RELEASE_VER>`. See [Get an app into a PMM release](../docs/how-to/get-an-app-into-a-pmm-release.md). |
| Manifest format | docker. The OCI format drops the `HEALTHCHECK` instruction. |
| User | `extensions` |
| Frontend | None. PMM's UI holds the pages. |

## What the image contains

| File | Role |
|---|---|
| `Containerfile.sidecar` | The final build stage. It ships the backend only and starts from the `extensions:builder` image. |
| `entrypoint.sh` | Process 1. It resolves `ENCRYPTION_KEY`, generates the broker password, resolves the Grafana token, and then starts `supervisord`. |
| `supervisord.conf` | Runs `valkey`, the four `migrate-*` programs, the `extensions`, `inventory` and `tasks` APIs, the Celery worker and Celery beat. |
| `settings-env.sh` | Sourced by `entrypoint.sh`. It expands the deployment inputs into settings variables. |
| `settings.yaml` | The settings profile, placed at `/home/extensions/app/settings.yaml`. |
| `encryption_key.py` | Run before `supervisord`. It resolves `ENCRYPTION_KEY`, and generates one only when no database holds encrypted values. |
| `grafana_service_account.py` | Run before `supervisord`. It resolves the Grafana token, and requests one when no source supplies it. |
| `runtime.py` | Shared by the two scripts above: the state directory, the retry interval and the timeouts. |
| `wait_for_schema.sh` | Run before each API. It holds the API until all four schema steps have written their marker. |
| `wait_for_api.py` | Run before Celery beat. It holds beat until the three APIs answer `/health`. |
| `clear_sentinels.sh` | Run by an operator before a schema step is rerun by hand. |
| `healthcheck.sh` | The image's health check. |
| `restrict_apps.py` | A build step. It removes every app package that the settings profile does not list, and then itself. |
| `verify_image_apps.py`, `verify_image_apps.sh` | A check, run after the build, that an image's apps match its own settings profile. The script is piped into the image and is not part of it. |

## App set

The image holds the apps that `EXTENSIONS.APPS` in `settings.yaml` lists, plus the `framework` and `shared` packages.

| Fact | Detail |
|---|---|
| What sets the list | `EXTENSIONS.APPS` in `sidecar/settings.yaml`, and nothing else. |
| Build argument | `EXTENSIONS_RESTRICT_APPS`. Only the value `1` removes anything. `make image` passes `1`. |
| A listed app with no package | The build fails. |
| Changing the list at runtime | It can only be narrowed. An app whose package is not in the image stops the container at startup, with a validation error that names the module. |
| Where the list can be changed at runtime | A file mounted over `/home/extensions/app/settings.yaml`, or the `EXTENSIONS__APPS` variable. The settings API cannot change it. |
| Migrations of a removed app | Its `version_locations` entry stays in `alembic.ini`. An upgrade then skips that app's revisions in a database that an image with more apps migrated. The entry is what makes that possible. |

`verify_image_apps.sh` prints the app set that it verified.

## Where settings come from

The image reads one YAML file. There is no second file that is merged over it.

| To change | Use |
|---|---|
| A few settings | Environment variables. They take precedence over the file. |
| The whole profile | A file mounted over `/home/extensions/app/settings.yaml`. It replaces the profile completely. |
| A secret, without putting it in the environment | A file in the directory that `SECRETS_DIR` names. The file's name is the setting's name. |

For one setting, the order of precedence is:

1. An environment variable with the setting's full name.
2. A file with that name in `SECRETS_DIR`.
3. The value derived from an `EXTENSIONS_*` deployment input.
4. The YAML file.

Within each of the first three, a name with a service prefix (`EXTENSIONS__`, `INVENTORY__` or `TASKS__`) takes precedence over the same name without one.

### The secrets directory

Inside PMM, PMM Server writes four files into a volume, and the sidecar mounts that volume read-only at the path in `SECRETS_DIR`. The four files are `SECRET_KEY`, `EXTENSIONS__DATABASE__PASSWORD`, `INVENTORY__DATABASE__PASSWORD` and `TASKS__DATABASE__PASSWORD`. The sidecar must be a member of group 0 to read them.

| Rule | Detail |
|---|---|
| Entries | Regular files directly in the directory. A file in a subdirectory matches no setting. |
| Names | Matched without regard to case. Two files whose names differ only in case are not supported. |
| Symbolic links | A link whose target is outside the directory is ignored. The layout that Kubernetes projects is read normally. |
| An empty file | It sets the value to the empty string. So an empty file is not the same as no file. |
| Default | `SECRETS_DIR` has none. A sidecar that mounts nothing is not affected. |

A file can supply these names:

| Name | Notes |
|---|---|
| `SECRET_KEY` | Never exported. Each process reads the file. |
| `ENCRYPTION_KEY` | Never exported. A file stops the sidecar from generating a key. |
| `DATABASE__PASSWORD` | One file serves all three services. A file with a service prefix overrides it for that service. |
| `{EXTENSIONS,INVENTORY,TASKS}__DATABASE__HOST` and `__PORT` | One name for each service. There is no name without a prefix. |
| `AUTH__PROVIDER__GRAFANA__SERVICE_ACCOUNT_TOKEN`, `PMM__API_KEY` | Either one stops the sidecar from requesting a token. The value is also exported to each of the three token settings that has no value. |
| `TASKS__NOMAD__API_KEY` | Sets Nomad's key only. It does not stop the token request. |
| `PMM__ENDPOINT`, `AUTH__PROVIDER__GRAFANA__ENDPOINT`, `TASKS__NOMAD__ENDPOINT` | A file replaces the derived value. |
| `EXTENSIONS_INTERNAL_TOKEN`, `BASE_URL`, `CELERY__BEAT_DBURI` | Read as they are. |

A file cannot supply `CELERY__BROKER_URL` or `CELERY__RESULT_BACKEND`: the entrypoint sets both at each start. A file cannot supply an `EXTENSIONS_*` deployment input either, because those are inputs to a script and not settings.

## Deployment inputs

`settings-env.sh` expands these inputs into settings variables.

| Input | Required | Default | Becomes |
|---|---|---|---|
| `SECRET_KEY` | Yes | None. The container exits without it. Inside PMM, PMM supplies it as a file. | Itself |
| `ENCRYPTION_KEY` | No | Generated into the state volume, on a deployment with no encrypted values | Itself |
| `EXTENSIONS_DB_PASSWORD` | In practice, unless a file supplies the password | None | The database password of all three services |
| `EXTENSIONS_DB_HOST` | No | `pmm-server` | The database host of all three services, and of the schema steps |
| `EXTENSIONS_DB_PORT` | No | `5432` | The database port, as above |
| `EXTENSIONS_GRAFANA_TOKEN` | No | Requested from Grafana at startup | `AUTH__PROVIDER__GRAFANA__SERVICE_ACCOUNT_TOKEN`, `PMM__API_KEY`, `TASKS__NOMAD__API_KEY` |
| `EXTENSIONS_PMM_ENDPOINT` | No | `https://pmm-server:8443` | `PMM__ENDPOINT`, and `AUTH__PROVIDER__GRAFANA__ENDPOINT` with `/graph` added |
| `EXTENSIONS_NOMAD_ENDPOINT` | No | `https://pmm-server:8443/nomad` | `TASKS__NOMAD__ENDPOINT` |
| `EXTENSIONS_INTERNAL_TOKEN` | No | Derived from `SECRET_KEY` | Itself. It authenticates calls between the services. |
| `BASE_URL` | When tasks download scripts | None | Itself. The sidecar's address as a task host reaches it, with the `/extensions` prefix. |

`SECRET_KEY` must be the same in every process and must not change between restarts. It signs sessions.

`ENCRYPTION_KEY` is 32 bytes in URL-safe base64. `make encryption-key` or `openssl rand -base64 32` produces one. A hexadecimal value is rejected.

A setting variable can also be set directly. It then overrides only itself: `EXTENSIONS__DATABASE__PASSWORD` leaves the other two services as they were.

### Inputs of the token request

| Input | Default | Meaning |
|---|---|---|
| `GF_SECURITY_ADMIN_USER` | `admin` | The Grafana administrator that requests the token. Used only when the sidecar holds no valid token. |
| `GF_SECURITY_ADMIN_PASSWORD` | `admin` | Its password. Removed from the environment before the services start. |
| `EXTENSIONS_STATE_DIR` | `/home/extensions/state` | Where the token and a generated encryption key are stored. |
| `EXTENSIONS_GRAFANA_MINT_TIMEOUT` | `60` | Seconds to keep trying a Grafana that has not started. After that, the services start without a token. |
| `EXTENSIONS_ENCRYPTION_PROBE_TIMEOUT` | `60` | Seconds to wait for the databases before a key is generated. |

### What is not an input

| Setting | Why it is fixed |
|---|---|
| Database user and name | PMM creates exactly the `pmm_extensions` role and database. |
| Celery broker and result backend | The entrypoint generates the broker's password at each start. |
| The services' hosts and ports | The health check uses `127.0.0.1` and ports 9000 to 9002. |
| TLS certificate and key | The services speak plain HTTP. PMM's nginx ends TLS. |

## Generated credentials

| Credential | Sources, in order | Stored |
|---|---|---|
| Broker password | Generated at each start. Nothing else can supply it. | In `/tmp/valkey.conf`, mode 0600, for the life of the container |
| `ENCRYPTION_KEY` | The variable. A file in `SECRETS_DIR`. A key stored by an earlier start. A new key. | `<state>/ENCRYPTION_KEY`, mode 0600, when generated |
| Grafana token | `EXTENSIONS_GRAFANA_TOKEN`, or one of the two token settings. A token stored by an earlier start. A new token. | `<state>/grafana_service_account_token`, mode 0600, when requested |

A new encryption key is generated only when none of the three service databases holds an encrypted value. If one does, or if a database cannot be read within the timeout, the container does not start. A lost key cannot be recovered.

A new Grafana token belongs to a service account named `pmm-extensions` with the `Admin` role. A stored token is checked with one call at startup. If Grafana rejects it, a new one is requested. If Grafana does not answer, the stored token is used.

## Settings an administrator may change

`SETTINGS_OVERRIDE.ALLOWED_KEYS` in `settings.yaml` lists every setting that the settings API accepts. A request for any other setting answers `422`.

| Setting class | Keys |
|---|---|
| `Settings` | `LOGGING`, `PMM__annotations_enabled` |
| `ExtensionsSettings` | `SYNC_REFRESH_TIME`, `CONNECTIVITY_CHECK_DEFAULT`, `FOOTER_TEMPLATE`, `DIAGNOSTICS_DELIVERY_INPUTS` |
| `AlertSettings` | `PROVIDERS`, `SOURCE_PREFIX`, `SOURCE_SUFFIX` |
| `AnonymizerSettings` | `DEFAULT_ENTITIES` |
| `TasksSettings` | `PRE_EXECUTION_CONNECTIVITY_CHECK`, `LOG_RETENTION_DAYS`, `PENDING_ALLOCATION_TIMEOUT_SECONDS` |
| `OmInventorySettings` | `AUTO_SCAN`, `SCHEDULE__every`, `SCHEDULE__period`, `PROBE_DATABASE`, `REPO_URL`, `REPO_TIMEOUT`, `CONNECT_TIMEOUT`, `TASK_TIMEOUT`, `POLL_INTERVAL`, `MAX_CONCURRENT_PROBES`, `RUN_RETENTION`, `STALE_RUN_AFTER`, `ENABLED` |

| Fact | Detail |
|---|---|
| Changing the list | Only through the deployment's configuration: the `SETTINGS_OVERRIDE__ALLOWED_KEYS` variable, or a replacement `settings.yaml`. Never through the API. |
| A replacement file without the key | Removes the restriction. Every setting that can be overridden then is. |
| A stored value for a setting that is not on the list | Ignored. `DELETE /settings/<class>/<key>` removes it. |
| `DIAGNOSTICS_DELIVERY_INPUTS` | Empty until an administrator supplies it. It is written as one whole object that names every secret the delivery plan declares. A write to one part of it answers `422`. Stored secrets read back as `**********`. |

## Volumes

| Path | Holds | Mount |
|---|---|---|
| `/home/extensions/state` | The Grafana token and a generated `ENCRYPTION_KEY` | Always. Without it, a recreated container loses the key, and a deployment with encrypted values does not start. |
| The path in `SECRETS_DIR` | The secret files that PMM writes | Read-only, shared with PMM Server |

The files under `/home/extensions/app` belong to `root`, and the `extensions` user cannot change their content. The directory itself belongs to `extensions`, so that user can add, remove and replace the entries directly in it. The state directory is the only path the image creates for the sidecar to write to by design.

## Health

`healthcheck.sh` exits 0 only when all of these hold:

- every long-running program is `RUNNING`
- each of the four schema steps has written its marker, `/tmp/migrate-<step>.ok`
- the three APIs answer `/health` with 200
- the broker answers `PING`

| Fact | Detail |
|---|---|
| Health check timing | `--interval=15s --timeout=15s --start-period=150s --retries=5`. A program that stops makes the container unhealthy after about 80 seconds. |
| Schema steps | `extensions`, `inventory`, `tasks` and `beat`. Each waits for its database with no time limit. |
| The APIs' gate | Each API waits up to 300 seconds for the four markers, then fails and is restarted. |
| A container restart | Clears the four markers before any program starts. |
| Health without a Grafana token | The health check does not test the token. A sidecar that started without one is healthy, and sign-in does not work. |

To rerun one schema step in a running container, see [Rerun a schema step in a running sidecar](../docs/how-to/rerun-a-schema-step.md). That page covers `clear_sentinels.sh`.

## Ports

| Port | Service | Reached by |
|---|---|---|
| 9000 | The `extensions` service | PMM's nginx, which forwards `/extensions/` to `pmm-extensions:9000`. `PMM_EXTENSIONS_ADDRESS` on PMM Server changes that address. |
| 9001 | The `inventory` service | The other services, inside the container |
| 9002 | The `tasks` service | The other services, inside the container |
| 6379 | Valkey, on `127.0.0.1` | Celery, inside the container |

The image publishes no port. A browser reaches PMM Extensions only through PMM.
