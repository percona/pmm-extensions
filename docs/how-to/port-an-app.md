# Port an app

This page shows how to make an app that is in this repository run inside PMM. The example is PostgreSQL Backups, `backup_pg`. Replace `backup_pg` with your app's name.

Twelve apps in this repository do not ship inside PMM. [App status](../app-status.md) lists them and what each one needs.

This page covers this repository only. Users see the app after PMM's UI has it too, and after a PMM release carries both changes. The last section links to those steps.

## Prerequisites

- A development environment with the virtualenv active: see [Set up a development environment](set-up-a-development-environment.md).
- Docker with Compose, for the local PMM: see [Run PMM Extensions in a local PMM](run-in-local-pmm.md).
- An app under `app/extensions/apps/` that `sidecar/settings.yaml` does not list.

## Check what the app depends on

The apps were written when PMM Extensions ran without PMM and had a UI of its own. Check three things before you start.

- **Its pages.** PMM's UI draws an app's pages from the app's form schema. An app whose pages were written by hand needs those pages written again in PMM's UI. [App status](../app-status.md) says how each app draws its pages.
- **Its tables.** An app with a `migrations/` directory creates tables of its own. PMM Extensions applies them when the container starts. Only `alerts` among the twelve has tables, and nobody has run it inside PMM.
- **Its tool.** A task runs a tool on the database host, and PMM Extensions installs nothing there. The host needs the tool, its configuration, and credentials in a file.

`backup_pg` draws its pages from its schema and has no tables. Its tool is pgBackRest, which the local PMM's PostgreSQL host has.

## Activate the app

`sidecar/settings.yaml` lists the apps that PMM Extensions serves inside PMM. The image ships the apps in that list and removes the code of the others.

1. In `sidecar/settings.yaml`, add a line for the app at the end of the list under `EXTENSIONS.APPS`:

   ```yaml
         - MODULE_NAME: om_bootstrap
         - MODULE_NAME: backup_pg
   ```

## Update the test that pins the list

A constant in the tests repeats the list, and one test compares the two.

1. Run that test:

   ```bash
   pytest tests/sidecar/test_embedded_settings.py::test_reduced_activation_mirrors_the_baked_profile -q
   ```

   It fails, and the failure names the app that the constant lacks:

   ```text
   E         Right contains one more item: 'app.extensions.apps.backup_pg'
   ```

2. In `tests/app/extensions/conftest.py`, add the app's name at the end of the tuple in `REDUCED_ACTIVATION`. The order must match the settings file.

3. Run the test again. It passes.

## Run the tests

1. Run the app's own tests:

   ```bash
   pytest tests/app/extensions/apps/backup_pg -q
   ```

2. Run the checks that every app must pass:

   ```bash
   pytest tests/app/extensions/apps/framework/test_conformance.py tests/app/extensions/apps/framework/test_form_dsl_conformance.py -q
   ```

3. Run the tests of the sidecar's settings. Here they took about three minutes:

   ```bash
   pytest tests/sidecar -q
   ```

All three exit with status 0. [Test an app](test-an-app.md) describes what each suite covers.

## Run the app in a local PMM

Tests do not run a task on a host. Run one before you open a pull request.

The steps mount your checkout's code, as [Run the code in your checkout](run-in-local-pmm.md#run-the-code-in-your-checkout) explains. When your checkout needs a newer Python than the image has, that section says which image to set first.

1. Start the local PMM with your checkout's code and the PostgreSQL host:

   ```bash
   docker compose -f sidecar/dev/compose.yaml -f sidecar/dev/compose.checkout.yaml \
     --profile postgresql up -d --wait
   ```

   If the local PMM was already running with your checkout's code, Compose leaves the `pmm-extensions` container as it is. Step 3 then does not list the app. Restart the container:

   ```bash
   docker compose -f sidecar/dev/compose.yaml -f sidecar/dev/compose.checkout.yaml \
     --profile postgresql up -d --wait --force-recreate pmm-extensions
   ```

2. Write the create request for a backup task to a file, `first-pg-backup.json`:

   ```json
   {
     "task_name": "first-pg-backup",
     "hostname": "postgresql-host",
     "service": "dev-postgresql",
     "stanza": "dev",
     "backup_dir": "/var/lib/pgsql/backups",
     "pgbackrest_bin": "/usr/bin/pgbackrest",
     "pgbackrest_config_file": "/etc/pgbackrest.conf",
     "pgbackrest_backup_type": "incr"
   }
   ```

   To see the fields of your app's form, run `python3 sidecar/dev/run-task.py --app backup_pg --schema`.

3. Create the task, run it, and wait for its end:

   ```bash
   python3 sidecar/dev/run-task.py --app backup_pg --body first-pg-backup.json
   ```

   The output is the task's record, and then what the tool printed on the host. The record starts with the status:

   ```text
   {
     "name": "first-pg-backup",
     "status": "success",
   ```

   The command exits with status 0 when the status is `success`. [Run a task with one command](run-in-local-pmm.md#run-a-task-with-one-command) lists the other exit statuses.

   If the output is `POST /extensions/api/apps/backup_pg/ answered 404`, PMM Extensions does not serve the app. Restart the container as in step 1.

A failure on the first run is likely. [App status](../app-status.md#how-the-cost-is-judged) says why.

## Verify

1. Ask the tool on the host what it holds:

   ```bash
   docker compose -f sidecar/dev/compose.yaml --profile postgresql exec postgresql-host \
     sudo -u postgres pgbackrest --stanza=dev info
   ```

   The output has `status: ok` and one `full backup` entry. pgBackRest makes the first backup a full one.

The app is ported in this repository when the tests pass and one task has finished with the status `success`.

## Next steps

- [Add an app to the PMM UI](add-an-app-to-the-pmm-ui.md)
- [Get an app into a PMM release](get-an-app-into-a-pmm-release.md)
- [App status](../app-status.md)
