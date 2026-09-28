# PMM Extensions

## Table of Contents

* [Prerequisites](#prerequisites)
* [Setup](#setup)
* [Configuration](#configuration)
   * [Plugins](#plugins)
   * [Secrets](#secrets)
      * [Getting Casdoor's Client ID and Client Secret](#getting-casdoors-client-id-and-client-secret)
   * [Environment](#environment)
   * [Syncers](#syncers)
      * [PMMSyncer](#pmmsyncer)
         * [Getting your PMM API Key](#getting-your-pmm-api-key)
      * [MySQLSyncer](#mysqlsyncer)
* [Usage](#usage)
  * [Starting Celery with PMM Extensions for development](#starting-celery-with-pmm-extensions-for-development)
* [Contributing](#contributing)
* [Deployment](#deployment)

## Prerequisites

- **Python Version Requirements**
```text
  >=3.11.9, !=3.12.0, !=3.12.1, !=3.12.2, <3.14
```

- [Casdoor](https://casdoor.org/docs/basic/server-installation) or [Docker](https://docs.docker.com/get-started/get-docker/)

You can start Casdoor on port 9999 with Docker by running
```shell
docker run --detach --name casdoor \
--volume casdoor-data:/var/lib/mysql:z,rw \
--publish 9999:8000/tcp \
casbin/casdoor-all-in-one
```

- [PMM](https://docs.percona.com/percona-monitoring-and-management/setting-up/server/index.html) or [Docker](https://docs.docker.com/get-started/get-docker/)

You can start PMM with Docker by running
```shell
docker run --detach --restart always \
--publish 8443:443 \
--volume pmm-data:/srv \
--name pmm-server \
percona/pmm-server:2
```

- [Percona Toolkit](https://www.percona.com/percona-toolkit)
- [Nomad](https://developer.hashicorp.com/nomad/tutorials/get-started/gs-install)

For the purpose of development, you can run Nomad with
```shell
sudo nomad agent -node="pmm-server" -dev \
        -bind 0.0.0.0 \
        -network-interface='{{ GetDefaultInterfaces | attr "name" }}'
```

- [Celery](https://docs.celeryq.dev/en/stable/)

You can start the Celery Worker with:
```shell
celery -A app.tasks.celery worker -l info
```

and the Celery Beart with:
```shell
celery -A app.tasks.celery beat -S sqlalchemy --loglevel=info
```

For development purposes, you can also [start Celery with PMM Extensions](#starting-celery-with-pmm-extensions-for-development).

## Setup

1. Clone the repository and enter the cloned folder:
```shell
git clone https://github.com/percona/pmm-extensions.git
cd pmm-extensions
```

2. Create and activate a virtualenv with the required packages:
```shell
make venv
source venv/bin/activate
```

> [!TIP]
> Use `venv/bin/activate.fish` if you're on a Fish shell.

3. Create PMM Extensions' databases with `make migrate`:
```shell
make migrate
```

4. Add your Redirect URL to the Casdoor application

In Casdoor's web interface, navigate to Identity > Applications > app-built-in
(should be in [this link](http://localhost:9999/applications/built-in/app-built-in))
and scroll to **Redirect URLs**.

The Redirect URL you should add depends on how PMM Extensions is running (see [Usage](#usage)).
For example, if you're running PMM Extensions in your localhost with HTTP in port 8000, you should
add the URLs `http://localhost:8000/oauth/callback` and `http://127.0.0.1:8000/oauth/callback`.

![image](https://github.com/user-attachments/assets/8a562b77-00c7-4192-bba3-d22e3514766f)

6. Create a .env file in the project root folder to store your secrets.
See the [secrets section](#secrets) of the README for more details.

## Configuration

PMM Extensions will read settings in the following order of priority:
1. Environment variables
2. .env file
3. Secret files
4. Settings file

By default, the .env file is expected to be `.env` and the settings file `settings.yaml`.
You can change that by using the environment variables `ENV_FILE` and `SETTINGS_FILE`.
Secret files are read from the directory named by `SECRETS_DIR`, which is unset by
default; when unset, no secret files are read. See the
[secrets section](#secrets) for how to name them.

The [settings.yaml](https://github.com/percona/pmm-extensions/blob/main/settings.yaml) has base settings that you can (but don't need to) change.

Some settings are app-specific and you might not need them for running another app.
These are some, but not all, the possible settings you can have, per app:


| Name                       | App       | Required | Default                                             | settings.yaml (development)                      |
|----------------------------|-----------|----------|-----------------------------------------------------|--------------------------------------------------|
| BASE_URL                   | all       | no       | Built from the user's request                       | N/A                                              |
| ALLOWED_HOSTS              | all       | yes      | N/A                                                 | `["localhost", "127.0.0.1"]`                     |
| ALLOW_CONCURRENT_SESSIONS  | all       | no       | False                                               | False                                            |
| SSL_CAFILE                 | all       | no       | null                                                | null                                             |
| AUTH__PROVIDER__CASDOOR__ENDPOINT          | all | yes | N/A                                             | `http://localhost:9999`                          |
| AUTH__PROVIDER__CASDOOR__FRONT_ENDPOINT    | all | no  | The same as `AUTH__PROVIDER__CASDOOR__ENDPOINT` | `//:9999`                                        |
| AUTH__PROVIDER__CASDOOR__CERTIFICATE_PATH  | all | no  | null                                            | null                                             |
| AUTH__PROVIDER__CASDOOR__ORGANIZATION_NAME | all | no  | built-in                                        | N/A                                              |
| AUTH__PROVIDER__CASDOOR__APPLICATION_NAME  | all | no  | app-built-in                                    | extensions-app                                          |
| AUTH__PROVIDER__CASDOOR__ALLOWED_ISSUERS   | all | no  | `[<ENDPOINT>]`                                  | `[http://localhost:9999, http://127.0.0.1:9999]` |
| CELERY__BROKER_URL         | all       | no       | N/A                                                 | filesystem://                                    |
| CELERY__BEAT_DBURI         | all       | no       | The resolved PMM Extensions database connection                | sqlite:///schedule.db                            |
| LOGGING                    | all       | no       | WARNING                                             | N/A                                              |
| BACKEND_CORS_ORIGINS       | all       | no       | []                                                  | [http://localhost:8000, http://127.0.0.1:8000]   |
| TASKS__NOMAD__ENDPOINT     | tasks     | yes      | N/A                                                 | http://127.0.0.1:4646                            |
| TASKS__NOMAD__API_KEY      | tasks     | no       | N/A                                                 | N/A                                              |
| TASKS__NOMAD__AUTH_SCHEME  | tasks     | no       | Bearer                                              | Bearer                                           |
| TASKS__NOMAD__SECURE       | tasks     | no       | False                                               | N/A                                              |
| TASKS__NOMAD__VERIFY_SSL   | tasks     | no       | False                                               | True                                             |
| TASKS__NOMAD__TIMEOUT      | tasks     | no       | 10                                                  | 10                                               |
| TASKS__NOMAD__MINIFY_PAYLOAD | tasks   | no       | True                                                | True                                             |
| TASKS__NOMAD__LOG_SOCKET_READ_TIMEOUT | tasks | no | 10                                        | 10                                               |
| TASKS__SYNC_LOCK_TTL       | tasks     | no       | 300                                                 | 300                                              |
| TASKS__ANONYMIZER__DEFAULT_ENTITIES | tasks | no | seven high-confidence entities (see below) | `[]` (anonymization disabled) |
| TASKS__EXECUTE_MODE        | tasks     | no       | background                                          | N/A                                              |
| TASKS__DATABASE__ENGINE    | tasks     | no       | sqlite                                              | N/A                                              |
| TASKS__DATABASE__NAME      | tasks     | no       | tasks.db                                            | N/A                                              |
| TASKS__DATABASE__USER      | tasks     | no       | N/A                                                 | N/A                                              |
| TASKS__DATABASE__PASSWORD  | tasks     | no       | N/A                                                 | N/A                                              |
| TASKS__DATABASE__HOST      | tasks     | no       | ""                                                  | ""                                               |
| TASKS__DATABASE__PORT      | tasks     | no       | N/A                                                 | N/A                                              |
| EXTENSIONS__INVENTORY_ENDPOINT    | extensions | yes      | N/A                                                 | http://localhost:8000/api/inventory              |
| EXTENSIONS__TASKS_ENDPOINT        | extensions | yes      | N/A                                                 | http://localhost:8000/api/tasks                  |
| EXTENSIONS__OAUTH__REDIRECT_URI   | extensions | yes      | N/A                                                 | /oauth/callback                                  |
| EXTENSIONS__OAUTH__POST_LOGIN_URI | extensions | no       | /                                                   | N/A                                              |
| EXTENSIONS__OAUTH__AUTH_LINK      | extensions | no       | CasdoorOptions.SYNC_SDK.get_auth_link(REDIRECT_URI) | N/A                                              |
| EXTENSIONS__PROXY_HEADERS         | extensions | no       | False                                               | False                                            |
| EXTENSIONS__SYNC_REFRESH_TIME     | extensions | no       | 5                                                   | 5                                                |
| EXTENSIONS__SESSION__COOKIE_NAME  | extensions | no       | authToken                                           | casdoorToken                                     |
| EXTENSIONS__SESSION__SECURE       | extensions | no       | False                                               | False                                            |
| EXTENSIONS__SESSION__HTTP_ONLY    | extensions | no       | True                                                | True                                             |
| EXTENSIONS__SESSION__SAME_SITE    | extensions | no       | lax                                                 | lax                                              |
| EXTENSIONS__SESSION__MAX_AGE      | extensions | no       | 3600                                                | 3600                                             |
| EXTENSIONS__TEMPLATES_DIR         | extensions | no       | templates                                           | templates                                        |
| EXTENSIONS__STATIC_DIR            | extensions | no       | static                                              | N/A                                              |
| EXTENSIONS__SECURITY_HEADERS__CONTENT_SECURITY_POLICY_EXCLUDE_PATHS | extensions | no | [] | [/api/docs, /api/inventory/docs, /api/tasks/docs] |
| ALERTING__SOURCE_SUFFIX    | all       | no       | ""                                                  | ":dev"                                           |

`TASKS__NOMAD__API_KEY` is sent on every Nomad request as
`Authorization: <TASKS__NOMAD__AUTH_SCHEME> <TASKS__NOMAD__API_KEY>`, and takes
precedence over a `user:password` embedded in `TASKS__NOMAD__ENDPOINT`: while a
key is set the endpoint's userinfo is stripped, since both HTTP clients would
otherwise derive basic auth from it and override the header. Leave the key unset
to keep authenticating with the endpoint's own userinfo, if it carries any.

The active authentication provider is configured under `AUTH__PROVIDER__<NAME>__*`,
and **exactly one** provider may be configured. Casdoor is the built-in default,
shipped as the `AUTH.PROVIDER.casdoor` entry in `settings.yaml`
(`AUTH__PROVIDER__CASDOOR__*`). To use a different provider, **replace** that
`casdoor` entry in `settings.yaml` with your provider's entry — configuring a
second provider (e.g. adding `AUTH__PROVIDER__CUSTOM__*` on top of the shipped
`casdoor` block) is rejected at startup, since only one provider may be active.
An out-of-tree provider uses the `CUSTOM` name with `AUTH__PROVIDER__CUSTOM__PROVIDER_CLASS`
(a dotted import path to a `BaseAuthProvider` subclass) plus that class's own
fields. The legacy top-level `CASDOOR__*` variables are **deprecated but still
honored** (a startup warning is logged); migrate to `AUTH__PROVIDER__CASDOOR__*`.

Path settings (`AUTH__PROVIDER__CASDOOR__CERTIFICATE_PATH`, `TEMPLATES_DIR`, `STATIC_DIR`,
etc.) may have relative or absolute values. Relative paths will be resolved from the
project root folder.

### Session Management

PMM Extensions provides configurable session management through the `EXTENSIONS__SESSION` section:

```yaml
EXTENSIONS:
  SESSION:
    COOKIE_NAME: casdoorToken
    SECURE: False
    HTTP_ONLY: True
    SAME_SITE: lax
    MAX_AGE: 3600
```

- `COOKIE_NAME`: Name of the session cookie
- `SECURE`: Whether the cookie should only be sent over HTTPS
- `HTTP_ONLY`: Whether the cookie should be accessible only via HTTP(S)
- `SAME_SITE`: SameSite attribute for the cookie (lax, strict, none)
- `MAX_AGE`: Maximum age of the session in seconds

### CSRF Protection

PMM Extensions uses double-submit cookie CSRF protection. The same token can be reused for
multiple POST requests (e.g. from a React SPA), so you do not need to refetch
a token after each request. Send the token in the `X-CSRF-TOKEN` header or in
the form body as `csrf-token`. The token expires with the session (see
[Session Management](#session-management)).

CSRF token lifetime is tied to the session `MAX_AGE`. Use the same
`EXTENSIONS__SESSION` section to control how long the token stays valid:

```yaml
EXTENSIONS:
  SESSION:
    COOKIE_NAME: casdoorToken
    MAX_AGE: 604800   # 7 days (seconds); CSRF token expires after the same period
```

### Security Headers

PMM Extensions supports configurable security headers through the `EXTENSIONS__SECURITY_HEADERS` section:

```yaml
EXTENSIONS:
  SECURITY_HEADERS:
    CONTENT_SECURITY_POLICY_EXCLUDE_PATHS:
      - /api/docs
      - /api/inventory/docs
      - /api/tasks/docs
```

This allows you to exclude specific paths from Content Security Policy restrictions.

### Anonymizer Configuration

The anonymizer plugin can be configured through the `TASKS__ANONYMIZER` section:

```yaml
TASKS:
  ANONYMIZER:
    DEFAULT_ENTITIES:
      - CREDIT_CARD
      - EMAIL_ADDRESS
      - IBAN_CODE
      - IP_ADDRESS
      - PHONE_NUMBER
      - US_SSN
      - US_ITIN
```

The `DEFAULT_ENTITIES` setting specifies which Personally Identifiable Information (PII) entities should be anonymized by default when a task does not set an explicit `anonymize_mask`. The shipped `default:` profile uses the seven high-confidence entities above (checksum-validated or strong-regex recognizers). The `development:` profile sets `DEFAULT_ENTITIES` to an empty list so local task logs stay readable and no Presidio/spaCy engine is constructed.

Profile overlays merge onto `default:`: a non-empty list prepends to the inherited list, and an empty list clears it. To narrow the set relative to `default:`, edit the `default:` block or use a runtime settings override (do not rely on a shorter non-empty profile list alone). Operators can restore any of the fourteen `PIIEntity` members through `settings.yaml` or a runtime settings override without a code change. Use `"*"` to select every supported entity.

### Sync Configuration

PMM Extensions provides several sync-related configuration options:

- `EXTENSIONS__SYNC_REFRESH_TIME`: Browser refresh interval during sync operations (in seconds)
- `TASKS__SYNC_LOCK_TTL`: TaskHistory sync lock timeout (in seconds)

### Nomad Advanced Configuration

Additional Nomad configuration options are available:

- `TASKS__NOMAD__MINIFY_PAYLOAD`: Whether to minify payloads before dispatch
- `TASKS__NOMAD__LOG_SOCKET_READ_TIMEOUT`: Socket read timeout for logs (in seconds)

> [!CAUTION]
> *Do not store secrets in settings.yaml, as the file is shared in the git repository.
> See the [secrets section](#secrets) of the README for more details.

### Plugins

PMM Extensions works with modular plugins. Plugins are FastAPI routers that will be added to the application
according to defined settings. Each plugin must have their own module in `app.extensions.plugins`
with a `router` inside. The following plugins are configured by default:

```yaml
PLUGINS:
  - NAME: Schema Change
    MODULE_NAME: alters
    URI_PATH: /alters
    CSS_CLASS: alters
  - NAME: Inventory
    MODULE_NAME: inventory
    URI_PATH: /inventory
    CSS_CLASS: inventory
  - NAME: Archive
    MODULE_NAME: archives
    URI_PATH: /archives
    CSS_CLASS: archive
  - NAME: MySQL Backups
    MODULE_NAME: mysql_backups
    URI_PATH: /mysql_backups
    CSS_CLASS: mysql_backups
  - NAME: Checksums
    MODULE_NAME: checksums
    URI_PATH: /checksums
    CSS_CLASS: checksums
  - NAME: MongoDB Backups
    MODULE_NAME: backup_mongo
    URI_PATH: /backup_mongo
    CSS_CLASS: backup_mongo
```

Each plugin configuration includes:
- `NAME`: Display name for the plugin
- `MODULE_NAME`: Python module name in `app.extensions.plugins`
- `URI_PATH`: URL path where the plugin will be accessible
- `CSS_CLASS`: CSS class for styling the plugin in the UI

### Secrets

PMM Extensions needs some keys and secrets to interact with Casdoor. They are:
- `AUTH__PROVIDER__CASDOOR__CLIENT_ID`
- `AUTH__PROVIDER__CASDOOR__CLIENT_SECRET`

You can create a basic .env file template by running the following command in the project root folder:
```shell
echo -e "AUTH__PROVIDER__CASDOOR__CLIENT_ID=YOUR_CASDOOR_CLIENT_ID\nAUTH__PROVIDER__CASDOOR__CLIENT_SECRET=YOUR_CASDOOR_CLIENT_SECRET\n" > .env
```

#### `ENCRYPTION_KEY`

`ENCRYPTION_KEY` is the key PMM Extensions encrypts stored values with. Secret-typed
settings-override values are encrypted with it at rest, and **every
environment needs its own, local development included**: PMM Extensions refuses to start
without one, and so do the Celery workers, the Alembic migrations, and the
OpenAPI dump. It has no default, is never derived from
`SECRET_KEY`, and no value ships in the repository: the values it protects are
real third-party credentials, so a shared key would protect nothing from anyone
who can read the source.

Mint one and add it as `ENCRYPTION_KEY=<key>` to **the file `ENV_FILE` names**:

```shell
make -s encryption-key
```

That is `.env` by default, but not always: `ENV_FILE` is read from the process
environment — exported in your shell, set by `direnv`, or passed by your
container runtime — and it redirects the loader to a different file, whose keys
replace `.env`'s rather than adding to them. Setting `ENV_FILE` *inside* `.env`
does nothing: it is resolved before the dotenv source is configured. So on a
checkout that exports `ENV_FILE=.env.local`, appending to `.env` succeeds and
changes nothing the application sees. If you are unsure which file is in play,
start the app and read the error: it names the exact path it looked in.

`openssl rand -base64 32` works too. Note that `openssl rand -hex 32` — the
generator `SECRET_KEY` uses — does **not** produce a valid key.

A deployment supplies the same value as an environment variable or as a file
named `ENCRYPTION_KEY` under `SECRETS_DIR`.

**Keep the value stable.** Ciphertext outlives the process that wrote it, so
rotating or losing the key makes every already-encrypted row permanently
unreadable. There is no recovery path and no rotation tooling. An override PMM Extensions
cannot decrypt is logged and skipped, and the setting falls back to its
YAML/env value — the deployment keeps starting, but the stored credential is
gone.

The test suite needs no action — it mints its own key per run.

#### Supplying a setting as a mounted file

Any setting can instead be supplied as a file inside the directory `SECRETS_DIR` names,
which keeps the value out of the process environment. Name the file after the canonical
`__`-nested variable the setting already uses — `SECRET_KEY`,
`DATABASE__PASSWORD`, `EXTENSIONS__DATABASE__PASSWORD`,
`AUTH__PROVIDER__GRAFANA__SERVICE_ACCOUNT_TOKEN` — and put the value in its contents.
`/run/secrets` is the conventional mount point:

```shell
mkdir -p /run/secrets
openssl rand -hex 32 > /run/secrets/SECRET_KEY
SECRETS_DIR=/run/secrets uvicorn app.main:app
```

An unprefixed global name such as `DATABASE__PASSWORD` resolves for every prefixed
settings class that reads the same destination — one mounted file reaches Extensions,
Inventory, and Tasks when all three share one database. A per-service spelling such
as `EXTENSIONS__DATABASE__PASSWORD` overrides the global one for that service only; when
both are present in the same source, the more specific name wins regardless of
ordering. Across sources the usual priority still applies, so an environment
variable outranks a file whichever spelling each uses. A name spelled
with another class's prefix — `INVENTORY__DATABASE__PASSWORD` read by
`ExtensionsSettings`, say — stays invisible to that class.

Surrounding whitespace is stripped, so a trailing newline is fine. A file only applies
when nothing higher in the priority list supplies the same setting: an environment
variable and a .env entry both still win over a file. A configured directory that does
not exist logs a warning and is otherwise ignored, and a directory holding no matching
file changes nothing.

#### Getting Casdoor's Client ID and Client Secret

1. In a browser, open Casdoor's web interface and login (the default credentials are `admin:123`).
If you started Casdoor with the Docker command in [Prerequisites](#prerequisites), it should be in http://localhost:9999.

![image](https://github.com/user-attachments/assets/770d9957-ca7e-48b5-8e40-1dd232038fde)

2. Navigate to Identity > Applications > app-built-in. If you started Casdoor with the Docker
command in [Prerequisites](#prerequisites), it should be in http://localhost:9999/applications/built-in/app-built-in

![image](https://github.com/user-attachments/assets/2b95de9d-33ce-4f60-9457-f89e7c6ce619)

3. Copy the app's Client ID and Client Secret and replace the respective `YOUR_CASDOOR_CLIENT_ID`
and `YOUR_CASDOOR_CLIENT_SECRET` in the .env file you created.

### Environment

You can categorize settings by environments in settings.yaml:
```yaml
default:
  # defaults settings shared by all environments
development:
  # development settings
production_docker:
  # production settings for Docker deployment
```

To switch environment, use the environment variable `FASTAPI_ENV` or add `FASTAPI_ENV`
to your .env file.

### Database Configuration

PMM Extensions supports multiple database engines for different components. Each component (Extensions, Inventory, Tasks) can have its own database configuration:

#### SQLite Configuration (Development)
```yaml
EXTENSIONS:
  DATABASE:
    ENGINE: sqlite  # Database engine: sqlite, postgresql
    USER: null
    PASSWORD: null
    HOST: ""  # Database host (empty string for SQLite to avoid URL construction issues)
    PORT: null
    NAME: extensions.db

INVENTORY:
  DATABASE:
    ENGINE: sqlite
    USER: null
    PASSWORD: null
    HOST: ""
    PORT: null
    NAME: inventory.db

TASKS:
  DATABASE:
    ENGINE: sqlite
    USER: null
    PASSWORD: null
    HOST: ""
    PORT: null
    NAME: tasks.db
```

#### PostgreSQL Configuration
```yaml
EXTENSIONS:
  DATABASE:
    ENGINE: postgresql
    USER: sep_user
    PASSWORD: your_secure_password
    HOST: localhost
    PORT: 5432
    NAME: extensions_database
```

Supported database engines:
- `sqlite`: SQLite database (default for development)
- `postgresql`: PostgreSQL database

> [!NOTE]
> For SQLite databases, set `HOST` to an empty string to avoid URL construction issues.

### Syncers

PMM Extensions features Inventory syncing with external services and APIs. You can choose the syncers
you want to enable in the EXTENSIONS.SYNCERS section of the configuration:

```yaml
EXTENSIONS:
  # ...
  SYNCERS:
    - SYNCER: PMMSyncer
```

Some syncers require additional configuration. PMM connection/auth config lives in the
top-level `PMM` section (not under the syncer entry) — `PMMSyncer` reads it directly:
```yaml
PMM:
  ENDPOINT: https://127.0.0.1:8443
  VERIFY_SSL: false
```
The PMM API key is a secret and should be set via an env var rather than `settings.yaml`:
```
PMM__API_KEY=<Your PMM API key>
```

Other syncers may take extra keyword arguments, defined globally through the
`EXTENSIONS.SYNCER_EXTRA_KWARGS` config (`EXTENSIONS__SYNCER_EXTRA_KWARGS` for env settings).

#### PMMSyncer

Sync Nodes and Services with PMM. Requires the `PMM` setting with `ENDPOINT`, `API_KEY`,
and optionally `VERIFY_SSL`, `SSL_CAFILE`, `SSL_KEYFILE`, and `SSL_CERTFILE`.

#### MySQLSyncer

Sync MySQL/MariaDB inventory (schemas and tables). Optional configuration under each `MySQLSyncer` entry in `EXTENSIONS.SYNCERS`:

- **`IGNORE_SCHEMAS`**: List of schema names to skip during sync (defaults typically include `sys`, `performance_schema`, `mysql`, `information_schema`).
- **`DEFAULT_EXECUTOR_HOST`**: Nomad node name to use when the MySQL service host does not match any Nomad node. Set this when syncing **RDS**, **DBaaS**, or other remote MySQL instances: the sync payload runs on a Nomad client, so you must choose which client can reach the database. The value must match a **node name** (key) returned by **`/api/tasks/hosts/`**—not the node IP or address—otherwise task execution will fail. If unset, the first available Nomad host is used when there is no match.

Credentials are read on the Nomad client from **`~/.my.cnf`** and **`~/.mylogin.cnf`**. For each host the payload connects to, it looks up a login path **by matching the RDS host**: it tries a login path named like the host (e.g. **`host:port`** or **`host_port`** with colon replaced by underscore), then falls back to **`client`**. So for multiple RDS instances with different credentials, create a login path per host in **`~/.mylogin.cnf`** (e.g. **`mysql_config_editor set --login-path=rds-a.region.rds.amazonaws.com:3306 --user=... --password --host=rds-a.region.rds.amazonaws.com --port=3306`**); the payload will use the matching path automatically.

Example for RDS/DBaaS:

```yaml
EXTENSIONS:
  SYNCERS:
    - SYNCER: MySQLSyncer
      IGNORE_SCHEMAS:
        - sys
        - performance_schema
        - mysql
        - information_schema
      DEFAULT_EXECUTOR_HOST: "ip-10-0-1-5.region.compute.internal"  # Nomad node name from /api/tasks/hosts/ that can reach RDS
```

#### MySQL Topology

Topology is a standalone, experimental **Topology** app that is shipped disabled
by default. Enable it like any other plugin by activating its module in
`EXTENSIONS.APPS`:

```yaml
EXTENSIONS:
  APPS:
    - MODULE_NAME: topology
      ENABLED: true
```

When enabled, the app draws an interactive React Flow graph of every MySQL
service the inventory knows about: replication chains (primary → replica with
GTID/IO/SQL state), dual-primary pairs, and Percona XtraDB Cluster groups. It
sources the MySQL host list from the Inventory service at request time.
Topology data is collected **live, on demand** - there is no persisted snapshot
in the database - by dispatching
sharded `run-python` tasks (capped at 8 shards) to executor hosts via the
Tasks API. Each shard runs the
[`topology.py`](app/extensions/apps/topology/payloads/topology.py) payload, which
fans out per-host queries with a `ThreadPoolExecutor` and emits NDJSON events
to stdout. The API polls the dispatched tasks (`GET /result`), merges their
stdout into the graph, and the client polls that endpoint until every shard is
finished. Results are cached client-side with TanStack Query, so re-opening the
app is free until the user clicks **Refresh**.

Topology runtime limits live in
[`api_routes.py`](app/extensions/apps/topology/api_routes.py) as module constants:
maximum shards is 8. Changing that value currently requires a code deploy; move
it into `inventory_settings` first if it needs per-deployment tuning.

The payload reuses the same credential rules as `MySQLSyncer` -
`~/.my.cnf` and `~/.mylogin.cnf` on the executor, with per-host login paths
matched by `host:port` (or `host_port`) and a `client` fallback - so no
additional configuration is required if you already have `MySQLSyncer` running
against the same hosts.

### SSL Configuration

PMM Extensions supports SSL/TLS configuration for secure communications. SSL settings can be configured at different levels:

#### Global SSL Settings
```yaml
SSL_CAFILE: /path/to/ca-certificate.pem  # Global CA certificate file
```

#### Component-specific SSL Settings
```yaml
EXTENSIONS:
  SSL_KEYFILE: /path/to/extensions-key.pem
  SSL_CERTFILE: /path/to/extensions-cert.pem

INVENTORY:
  SSL_KEYFILE: /path/to/inventory-key.pem
  SSL_CERTFILE: /path/to/inventory-cert.pem

TASKS:
  SSL_KEYFILE: /path/to/tasks-key.pem
  SSL_CERTFILE: /path/to/tasks-cert.pem
```

#### Nomad SSL Configuration
```yaml
TASKS:
  NOMAD:
    SSL_CAFILE: /path/to/nomad-ca.pem
    SSL_CERTFILE: /path/to/nomad-cert.pem
    SSL_KEYFILE: /path/to/nomad-key.pem
```

SSL certificate files should be in PEM format. Relative paths will be resolved from the project root folder.

##### Getting your PMM API Key

1. In a browser, open PMM's web interface and login (the default credentials are `admin:admin`).
If you started PMM with the Docker command in [Prerequisites](#prerequisites), it should be in https://localhost.

![image](https://github.com/user-attachments/assets/1d521d18-388d-49ea-be32-168d7940748b)

2. Navigate to Configuration > API keys. If you started PMM with the Docker command in
[Prerequisites](#prerequisites), it should be in https://localhost/graph/org/apikeys.

![image](https://github.com/user-attachments/assets/c98792c9-a247-49d7-aafd-c4fcb3523848)

3. Click the **New API key** button to create a new API key. Make sure the **Role** is set to **Admin**.

![image](https://github.com/user-attachments/assets/59a8a2c0-2a5e-4183-a3b9-b3f7f858f9b8)

4. Copy the generated API Key and replace the respective `YOUR_PMM_API_KEY` in your .env file.

![image](https://github.com/user-attachments/assets/a6879771-6350-4505-944a-a5be8183f54c)

## Usage

1. Enter the project folder:
```shell
cd pmm-extensions
```

2. Activate your virtualenv:
```shell
source venv/bin/activate
```

3. Start PMM Extensions:
```shell
LOGGING=debug python3 -m app.main
```

PMM Extensions will be available in http://localhost:8000.

![image](https://github.com/user-attachments/assets/cec67a8e-341a-45d5-9144-e6c24f5128eb)

### API documentation

PMM Extensions exposes interactive Swagger UI pages for each of its services:

| Path | Scope |
|---|---|
| `/api/docs` | Merged core + PMM Extensions web-app schema |
| `/api/inventory/docs` | Inventory service |
| `/api/tasks/docs` | Tasks service |


### Starting Celery with PMM Extensions for development

For development environments, you can start the Celery Worker and the Celery Beat with PMM Extensions by using the `--start-celery` flag:
```shell
LOGGING=debug python3 -m app.main --start-celery
```

## Contributing

See our [CONTRIBUTING](https://github.com/percona/pmm-extensions/blob/main/CONTRIBUTING.md) guide.

## Deployment

PMM Extensions is moving to ship bundled with Percona Monitoring and Management (PMM), which will
deploy and run it — no separate PMM Extensions deployment step. That integration has not been
released yet; once it ships, PMM's own documentation will cover enabling it.

v0.13.1 was the final standalone PMM Extensions release. Its installer and standalone image remain
available from that release tag for existing deployments.
