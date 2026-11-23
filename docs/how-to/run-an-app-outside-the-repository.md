# Run an app outside the repository

This page shows how to run an app whose code is not in this repository. You try the app in the local PMM by a mount. Then you build an image that holds a released PMM Extensions and your app, and you run that image.

An app runs inside PMM Extensions and has the same reach. It uses PMM's administrator rights and the PMM Extensions database, and its tasks run on every database host that PMM monitors. Percona did not review an app that is not in this repository. Whoever built the app owns it: see [Ownership of the framework and the apps](../explanation/ownership.md). Run it on a PMM only when the person responsible for that PMM has agreed.

## Prerequisites

- What [Run PMM Extensions in a local PMM](run-in-local-pmm.md) lists. You need no virtualenv for this page.
- An app's package in a directory of your own, with the app's tests beside it:

  ```text
  my-app/
    checksums/    the app's package, as the scaffolder writes it under app/extensions/apps/
    tests/        the app's tests, as the scaffolder writes them under tests/app/extensions/apps/checksums/
  ```

- An app with no table of its own. The image of this page does not run an app's schema steps. Such an app loads, and its table is never created.

The examples use a copy of the Checksums app, which the released image does not hold. To follow the page with it, copy it out of your clone:

```bash
mkdir -p ~/my-app
cp -r app/extensions/apps/checksums ~/my-app/checksums
cp -r tests/app/extensions/apps/checksums ~/my-app/tests
```

Replace `checksums` with your app's name in every command. Run every command from the root of the repository.

To run the app's tests, see [Run every check for one app](test-an-app.md#run-every-check-for-one-app).

## Create the package of a new app

Skip this section when you already have an app's package.

The scaffolder writes a new app into your clone. You move the app out, and you put the clone back as it was.

1. Generate the app. The example is the Disk Free app of [Build your first app](../tutorials/first-app.md):

   ```bash
   python3 app/extensions/apps/framework/scaffold.py --name diskfree --type task --no-input --command df
   ```

   The output names the two directories that it wrote. [The scaffolder](../reference/scaffolder.md) lists every option.

2. Move the app's package and its tests to a directory of your own:

   ```bash
   mkdir -p ~/my-new-app
   mv app/extensions/apps/diskfree ~/my-new-app/diskfree
   mv tests/app/extensions/apps/diskfree ~/my-new-app/tests
   ```

3. Undo the line that the scaffolder added to `settings.yaml`:

   ```bash
   git restore settings.yaml
   ```

   `git status --short` now prints nothing.

## Try the app by a mount

The released image serves the apps that its settings profile lists, from `app/extensions/apps/` in the image. A second Compose file mounts your app's package there, and a profile that lists the app.

1. Write the profile. It is the released image's own profile with one line added:

   ```bash
   docker run --rm -v "$PWD/sidecar/dev/app/add_app.py:/add_app.py:ro" --entrypoint sh \
     percona/pmm-extensions:3.10.0 -c 'cat /home/extensions/app/settings.yaml > /tmp/profile.yaml &&
     python /add_app.py /tmp/profile.yaml checksums >&2 && cat /tmp/profile.yaml' \
     > ~/my-app/profile-3.10.0.yaml
   ```

   The output is the list of apps in the new profile:

   ```text
   apps in the profile: inventory, atw, mysql_backups, om_inventory, om_bootstrap, checksums
   ```

2. Name the app, its package and the profile. Both paths must be absolute:

   ```bash
   export APP_NAME=checksums
   export APP_DIR=$HOME/my-app/checksums
   export APP_PROFILE=$HOME/my-app/profile-3.10.0.yaml
   ```

3. Start the local PMM with the second file and the MySQL host:

   ```bash
   docker compose -f sidecar/dev/compose.yaml -f sidecar/dev/compose.app.yaml \
     --profile mysql up -d --wait
   ```

   The command returns when all three containers are healthy.

4. Write a create request for your app to a file, `~/my-app/first-run.json`. To see the fields of the form, run `python3 sidecar/dev/run-task.py --app checksums --schema`. For Checksums:

   ```json
   {
     "task_name": "first-run",
     "hostname": "mysql-host",
     "service": "dev-mysql",
     "recursion_method": "none",
     "databases": ["employees"]
   }
   ```

5. Run one task:

   ```bash
   python3 sidecar/dev/run-task.py --app checksums --body ~/my-app/first-run.json
   ```

   The output is the task's record, and then what the tool printed on the host. The record starts with the status:

   ```text
   {
     "name": "first-run",
     "status": "success",
   ```

6. After each change to the app's code, restart the container with the same variables set:

   ```bash
   docker compose -f sidecar/dev/compose.yaml -f sidecar/dev/compose.app.yaml \
     --profile mysql up -d --wait --force-recreate pmm-extensions
   ```

Use the mount in the local PMM only. Two things make it wrong for a PMM that someone else runs:

- A container that starts without the two mounts does not serve the app, and its log has no line that says so. The app's tasks stay in the database. Their addresses answer 404 until the app is back.
- The mounted profile replaces the image's profile completely. A profile that you wrote for one version hides the settings of the next one.

## Build an image with the app

The image is the released one plus your app. Its build fails when the app does not load.

1. Copy the two build files into the directory that holds the app's package:

   ```bash
   cp sidecar/dev/app/Containerfile sidecar/dev/app/add_app.py ~/my-app/
   ```

2. Build the image. `OWNER` names the person or the team that owns the app:

   ```bash
   docker build --build-arg APP=checksums --build-arg OWNER="Database team" \
     -f ~/my-app/Containerfile -t my-pmm-extensions:3.10.0-checksums ~/my-app
   ```

   The last step of the build loads the apps in the new image and lists them:

   ```text
   apps in this image: atw, checksums, inventory, mysql_backups, mysql_backups/restore, om_bootstrap, om_inventory
   ```

   When the app does not load, the build stops at that step with the app's error.

The build does four things. It copies the app's package to `app/extensions/apps/` in the image. It adds the app's line to the profile that the base image holds, so every other setting stays the base image's. It loads the apps. It labels the image with the app's name, its owner, and the fact that Percona did not review the app.

## Run the image

1. Stop serving the app by the mount, and start the local PMM on your image, without the second file:

   ```bash
   PMM_EXTENSIONS_IMAGE=my-pmm-extensions:3.10.0-checksums \
     docker compose -f sidecar/dev/compose.yaml --profile mysql up -d --wait
   ```

2. Change `task_name` in `~/my-app/first-run.json` to `second-run`, and run one task:

   ```bash
   python3 sidecar/dev/run-task.py --app checksums --body ~/my-app/first-run.json
   ```

   The record has `"status": "success"`.

What holds on every PMM that runs the image:

- The app is on as soon as the container starts. No administrator turns it on.
- The app has no page in PMM's UI. PMM's **Apps** menu lists the apps that PMM itself knows, and the app's address shows "Page not found". You run the app through the API, as `sidecar/dev/run-task.py` does.

To run the image on a PMM that is not yours, give its name to the person who runs that PMM. That person sets it as the image of the PMM Extensions container.

## Upgrade

1. Build the image again on the new release, with the version in `BASE` and in the tag:

   ```bash
   docker build --build-arg BASE=percona/pmm-extensions:3.10.0 --build-arg APP=checksums \
     --build-arg OWNER="Database team" \
     -f ~/my-app/Containerfile -t my-pmm-extensions:3.10.0-checksums ~/my-app
   ```

   If the framework changed under the app, the build stops at the step that loads the apps. Change the app, and build again.

Keep a copy of the PMM Extensions database from before the upgrade. A newer image changes the database when it starts. An older image does not start on a database that a newer one changed, and every request answers 503.

## Verify

1. Read the labels of the image:

   ```bash
   docker inspect my-pmm-extensions:3.10.0-checksums --format '{{json .Config.Labels}}'
   ```

   The output includes the app's name and its owner, and says that Percona did not review the app:

   ```text
   "pmm-extensions.app.name":"checksums","pmm-extensions.app.owner":"Database team","pmm-extensions.app.reviewed-by-percona":"false"
   ```

2. Confirm that the task of [Run the image](#run-the-image) ended with the status `success`.

## Next steps

- [Test an app](test-an-app.md), for the checks of an app that is not in this repository
- [Get an app into a PMM release](get-an-app-into-a-pmm-release.md), to propose the app for PMM itself
- [Ownership of the framework and the apps](../explanation/ownership.md)
- [Run PMM Extensions in a local PMM](run-in-local-pmm.md#stop-and-remove), to stop the local PMM and remove its data
