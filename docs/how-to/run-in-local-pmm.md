# Run PMM Extensions in a local PMM

This page starts PMM and PMM Extensions on your machine, adds a database host that can run tasks, and runs one backup. It also shows how to run the code of your checkout in place of the released code.

Everything runs from `sidecar/dev/` in this repository, on released images.

If you already work in `percona/pmm`, its `docker-compose.yml` has an `extensions` profile that starts the same pair of containers. It has no database host that can run a task, and it fixes the container names and publishes PMM on every interface.

## Prerequisites

- Docker with Compose. The steps were tested with Docker 29.10 and Compose 5.8.
- Not rootless Podman. PMM and PMM Extensions start there, and a database host cannot run a task.
- An x86-64 machine, or a Mac with Apple Silicon. The PMM images exist for amd64 only, and on the Mac they ran through Rosetta. The steps were tested on Linux x86-64, and on macOS with Colima started with Rosetta: `colima start --vm-type vz --vz-rosetta`.
- A clone of this repository. You do not need the virtualenv.
- Port 8443 free on `127.0.0.1`, or another port in `PMM_PORT`.
- About 8 GB of disk space for the images.

Run every command from the root of the repository.

## Start PMM with PMM Extensions

The stack listens on `127.0.0.1` only. These variables change it:

| Variable | Default | Meaning |
|---|---|---|
| `PMM_VERSION` | `3.10.0` | The version of PMM, PMM Extensions and PMM Client. All three use the same one. |
| `PMM_PORT` | `8443` | The port on `127.0.0.1` where PMM listens. |
| `PMM_ADMIN_PASSWORD` | `admin` | The password of PMM's `admin` user. |
| `PMM_EXTENSIONS_IMAGE` | `percona/pmm-extensions:<PMM_VERSION>` | The PMM Extensions image, for an image that you built. |
| `DEV_DATABASE_PASSWORD` | `pmm-extensions-dev` | The password of every database account on the database hosts. Use letters, digits, `-` and `_` only: the hosts write it into SQL and into option files as it is. |

The default passwords are public. On a machine that other people can sign in to, set both passwords before the first start.

If the stack already started with the default passwords, remove its data as described in [Stop and remove](#stop-and-remove). Then start it again with both variables set.

1. Start the two containers:

   ```bash
   docker compose -f sidecar/dev/compose.yaml up -d --wait
   ```

   The first run pulls two images. The command returns when both containers are healthy:

   ```text
    Container pmm-extensions-dev-pmm-server-1 Healthy
    Container pmm-extensions-dev-pmm-extensions-1 Healthy
   ```

2. Check that PMM Extensions answers through PMM:

   ```bash
   curl -sk -o /dev/null -w '%{http_code}\n' https://127.0.0.1:8443/extensions/health
   ```

   The output is `200`.

3. Open `https://127.0.0.1:8443` in a browser and sign in as `admin` with the password `admin`, or with the value of `PMM_ADMIN_PASSWORD`. The browser warns about the certificate, because PMM signs its own. PMM then asks you to change the password: select **Skip**.

## Add a database host

A task runs on a host that has PMM Client and the tool the task calls. The environment has two such hosts, each behind a Compose profile.

| Profile | Host | Holds | Service name in PMM |
|---|---|---|---|
| `mysql` | `mysql-host` | Percona Server for MySQL 8.4, XtraBackup, mydumper, Percona Toolkit and the `employees` sample database. The credentials are in `/root/.my.cnf`. | `dev-mysql` |
| `postgresql` | `postgresql-host` | Percona Distribution for PostgreSQL 17, pgBackRest with the stanza `dev` in `/etc/pgbackrest.conf`, and `pgbench` sample data. | `dev-postgresql` |

1. Start the MySQL host:

   ```bash
   docker compose -f sidecar/dev/compose.yaml --profile mysql up -d --wait
   ```

   The first run builds the host's image and imports the sample data. Here that took about seven minutes. The command returns when the host is registered with PMM:

   ```text
    Container pmm-extensions-dev-mysql-host-1 Healthy
   ```

To start the PostgreSQL host too, add `--profile postgresql`. On macOS with Colima, whose Docker runs AppArmor, both hosts start and run a backup task.

Give the same profiles to every later `docker compose` command. Compose ignores a service whose profile is not named. The same holds for a second `-f` file: a command without it recreates the containers without what that file adds.

## Call the API from a shell

PMM Extensions accepts a short-lived token that you get in exchange for a PMM session.

1. Sign in to PMM and keep the session cookie in a file that only you can read:

   ```bash
   install -m 600 /dev/null ~/.pmm-cookies
   curl -sk -c ~/.pmm-cookies -X POST https://127.0.0.1:8443/graph/login \
     -H 'Content-Type: application/json' -d '{"user":"admin","password":"admin"}'
   ```

   The output is `{"message":"Logged in","redirectUrl":"/graph/"}`.

   If you set `PMM_ADMIN_PASSWORD`, put its value in place of the second `admin`.

2. Exchange the session for a token:

   ```bash
   TOKEN=$(curl -sk -b ~/.pmm-cookies -X POST \
     https://127.0.0.1:8443/extensions/api/oauth/session/exchange | python3 -c \
     'import json,sys; print(json.load(sys.stdin)["access_token"])')
   ```

3. List the hosts that can run a task:

   ```bash
   curl -sk -H "Authorization: Bearer $TOKEN" \
     https://127.0.0.1:8443/extensions/api/extensions/hosts/
   ```

   The output lists `pmm-server` and each database host that you started:

   ```json
   [{"id":"mysql-host","name":"mysql-host","address":"172.20.0.4","can_elevate":null},{"id":"pmm-server","name":"pmm-server","address":"127.0.0.1","can_elevate":false}]
   ```

When a request answers `401`, the token has expired. Repeat step 2.

## Make a new service appear at once

PMM Extensions copies the services from PMM's inventory every 15 minutes. A new database host can run a task at once, but its service is missing from the forms until the next copy.

1. Start a copy now:

   ```bash
   curl -sk -X POST -H "Authorization: Bearer $TOKEN" -o /dev/null -w '%{http_code}\n' \
     https://127.0.0.1:8443/extensions/api/apps/inventory/sync/
   ```

   The output is `202`.

2. After a few seconds, list the services:

   ```bash
   curl -sk -H "Authorization: Bearer $TOKEN" \
     https://127.0.0.1:8443/extensions/api/extensions/services/ | python3 -c \
     'import json,sys; [print(s["id"], s["type"], s["name"]) for s in json.load(sys.stdin)["items"]]'
   ```

   The output has one line for each service:

   ```text
   2 mysql dev-mysql
   1 postgresql pmm-server-postgresql
   ```

   Note the identifier of `dev-mysql`. The next section uses it.

## Run a backup

1. Create an XtraBackup task for `dev-mysql`, with its identifier from the list in place of `2`:

   ```bash
   curl -sk -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -o /dev/null -w '%{http_code}\n' https://127.0.0.1:8443/extensions/api/apps/mysql_backups/ \
     -d '{"task_name":"first-backup","hostname":"mysql-host","service_id":2,"backup_type":"X","backup_dir":"/var/backups/dev"}'
   ```

   The output is `201`.

2. Run the task:

   ```bash
   curl -sk -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     https://127.0.0.1:8443/extensions/api/apps/mysql_backups/first-backup/execute -d '{}'
   ```

   The output includes `"status":"running"`.

3. After about 20 seconds, read the task's status:

   ```bash
   curl -sk -H "Authorization: Bearer $TOKEN" \
     https://127.0.0.1:8443/extensions/api/apps/mysql_backups/first-backup | python3 -c \
     'import json,sys; print(json.load(sys.stdin)["status"])'
   ```

   The output is `success`. While the backup runs, it is `running`.

## Run a task with one command

`sidecar/dev/run-task.py` makes the calls of the last three sections for you. It needs Python 3.11 or later, and no virtualenv.

1. Write the create request to a file, `second-backup.json`. Name the service in `service`, and the script looks up its identifier:

   ```json
   {
     "task_name": "second-backup",
     "hostname": "mysql-host",
     "service": "dev-mysql",
     "backup_type": "X",
     "backup_dir": "/var/backups/dev"
   }
   ```

2. Run the task:

   ```bash
   python3 sidecar/dev/run-task.py --app mysql_backups --body second-backup.json
   ```

   The script creates the task, runs it, and waits for its end. It prints the task's record, and then what the tool printed on the host. The record starts with the status:

   ```text
   {
     "name": "second-backup",
     "status": "success",
   ```

   The last line of the output is the result:

   ```text
   data: {"status": "success"}
   ```

The exit status tells how the run ended:

| Exit status | Meaning |
|---|---|
| 0 | The task ended with the status `success`. |
| 1 | The task ended with another status. The output still holds what the tool printed. |
| 2 | The API refused a request. The message is the API's own. An unknown service name lists the names that PMM has. |
| 3 | The task did not end within `--timeout` seconds. The default is 600. |

The script reads `PMM_PORT` and `PMM_ADMIN_PASSWORD`, or takes `--port` and `--password`. It never deletes a task, so a second run needs another `task_name`.

To see which fields an app's form has, give `--schema` in place of `--body`:

```bash
python3 sidecar/dev/run-task.py --app mysql_backups --schema
```

The output has one line for each field, with its type, whether it is required, its default and its choices. A field's description follows on a line of its own:

```text
Task
  task_name  string  required
  hostname  host  required
  service_id  service  required
```

## Run the code in your checkout

The stack runs the code inside the released image. A second Compose file runs your checkout's code in its place. It mounts three paths into the `pmm-extensions` container, read-only: `app/`, `snippets/` and `sidecar/settings.yaml`.

The mount needs a released image whose Python can run your checkout's code. [Set up a development environment](set-up-a-development-environment.md#prerequisites) names the Python version that the checkout needs. When the image's Python is older than that, the `pmm-extensions` container does not start. On `main` this is so while the released image has an older Python than `main` needs. Then build an image from your checkout and set `PMM_EXTENSIONS_IMAGE` to it: see [Build the image](get-an-app-into-a-pmm-release.md#build-the-image). The steps below are for a checkout that the released image can run.

1. Start the stack with the second file:

   ```bash
   docker compose -f sidecar/dev/compose.yaml -f sidecar/dev/compose.checkout.yaml \
     --profile mysql up -d --wait
   ```

   Compose recreates the `pmm-extensions` container. The apps listed in your `sidecar/settings.yaml` are now the apps that it serves.

2. After each change to the code or to `sidecar/settings.yaml`, restart the container and wait until it is healthy:

   ```bash
   docker compose -f sidecar/dev/compose.yaml -f sidecar/dev/compose.checkout.yaml \
     --profile mysql up -d --wait --force-recreate pmm-extensions
   ```

With your checkout's code, PMM's UI shows a banner that says PMM Server and PMM Extensions are at different versions. Your checkout is newer than the released PMM, so the banner is expected.

Your checkout's schema steps change the database, and the released code cannot use a database that newer code changed. To go back to the released code, remove the data first: see [Stop and remove](#stop-and-remove).

The second file replaces code. It does not install packages. When your checkout needs a Python package that the released image lacks, the container fails to start. Build an image from your checkout and set `PMM_EXTENSIONS_IMAGE` to it: see [Get an app into a PMM release](get-an-app-into-a-pmm-release.md).

## Use your own database host

The two hosts are a convenience. PMM Extensions runs a task on any host that meets the list in [What the sidecar expects from Nomad](../explanation/architecture.md#what-the-sidecar-expects-from-nomad). In this environment, three more things hold for such a host:

- It reaches PMM at the name `pmm-server`, on ports 8443 and 4647. A container gets that when it joins the Compose network, `pmm-extensions-dev_default`.
- Its database service is added to PMM, for example with `pmm-admin add mysql`. A form offers only the services that PMM knows.
- The credentials for the tool are in a file on the host. A form takes a path, not a password.

The files under `sidecar/dev/targets/` are working examples of such a host.

## Verify

1. In PMM's menu, expand **Apps** and select **MySQL Backups**.
2. Confirm that the page lists `first-backup` with the status **Done**.

## Troubleshooting

### The first `up` times out

The command ends with `dependency failed to start: container ... is unhealthy`. PMM took longer to start than its health check allows. Run the same command again.

### A database host is missing from the hosts that can run a task

The host's PMM Client keeps the address of the PMM it first joined. Restart the host:

```bash
docker compose -f sidecar/dev/compose.yaml --profile mysql restart mysql-host
```

### The sidecar is healthy and requests fail with HTTP 502

A request answers `502` with "Failed to get a response from Nomad". Nomad is the engine that PMM uses to run tasks. This happens with the `3.10.0` image when the `pmm-extensions` container started before PMM was ready. That image then runs without its token. Restart it:

```bash
docker compose -f sidecar/dev/compose.yaml restart pmm-extensions
```

This environment waits for PMM before it starts the container. The symptom appears with other Compose files that do not.

An image that you build from this repository waits for PMM itself when it holds no token, and it reports unhealthy until it has one. A stored token is used without a check while Grafana does not answer, so a healthy container does not prove that the token works. See [Starting before PMM is ready](../../sidecar/README.md#starting-before-pmm-is-ready).

### The port is taken

The command ends with `address already in use`. Set another port:

```bash
PMM_PORT=8444 docker compose -f sidecar/dev/compose.yaml up -d --wait
```

Use the same value in every later command, and in the URLs on this page.

## Stop and remove

1. Stop the containers and keep their data:

   ```bash
   docker compose -f sidecar/dev/compose.yaml --profile mysql --profile postgresql down
   ```

2. To remove the data too, add `-v`:

   ```bash
   docker compose -f sidecar/dev/compose.yaml --profile mysql --profile postgresql down -v
   ```

3. Remove the file that holds the session:

   ```bash
   rm -f ~/.pmm-cookies
   ```

## Next steps

- [Build your first app](../tutorials/first-app.md)
- [Port an app](port-an-app.md)
- [Add an app to the PMM UI](add-an-app-to-the-pmm-ui.md)
- [How the sidecar runs inside PMM](../explanation/architecture.md)
