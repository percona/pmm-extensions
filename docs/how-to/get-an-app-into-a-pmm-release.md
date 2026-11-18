# Get an app into a PMM release

This page shows how to build the PMM Extensions image with your app, check it, and open the two pull requests. The example is PostgreSQL Backups, `backup_pg`.

PMM Extensions has the same version as PMM, and an app reaches users with the next PMM release.

## Prerequisites

- The app runs in a local PMM from your checkout: see [Port an app](port-an-app.md).
- The change to PMM's UI is ready: see [Add an app to the PMM UI](add-an-app-to-the-pmm-ui.md).
- Podman and buildah. The image is built with buildah, into Podman's store.
- Docker with Compose, for the local PMM.

Run every command from the root of this repository.

## Commit the app first

The image takes `app/` and `snippets/` from the last commit, not from your working tree. An app that is not committed is not in the image. The files under `sidecar/` are different: the build copies them from the working tree.

1. Commit your change on your branch.

2. Confirm that nothing under the two directories is left out:

   ```bash
   git status --short app snippets
   ```

   The command prints nothing.

## Build the image

You need an image before you open a pull request, not after each change. During development, run your checkout's code: see [Run PMM Extensions in a local PMM](run-in-local-pmm.md#run-the-code-in-your-checkout).

1. Build the base image, which holds the Python dependencies:

   ```bash
   make builder
   ```

   Here that took between 8 and 20 minutes. Run it once, and again when `poetry.lock` changes.

2. Build the image:

   ```bash
   make image
   ```

   Here that took between two and five minutes. The output includes this line:

   ```text
   Successfully tagged localhost/extensions:HEAD
   ```

   Without the base image, the command stops at once and says `Run 'make builder' first.`

## Check the image

Check the image itself, not the commit that it was built from.

1. Check the list of apps:

   ```bash
   CONTAINER_RUNTIME=podman sidecar/verify_image_apps.sh localhost/extensions:HEAD restricted
   ```

   The output names your app among the packages:

   ```text
   verified restricted: 8 app packages (atw, backup_pg, framework, inventory, mysql_backups, om_bootstrap, om_inventory, shared)
   ```

2. Check that the image reads its secrets from a directory:

   ```bash
   podman run --rm --entrypoint grep localhost/extensions:HEAD \
     -c SECRETS_DIR /home/extensions/app/settings-env.sh
   ```

   The output is a number above zero.

3. Check that the image can create its own Grafana token:

   ```bash
   podman run --rm --entrypoint ls localhost/extensions:HEAD \
     -ld /home/extensions/app/grafana_service_account.py /home/extensions/state
   ```

   The output lists the file, and the directory with the mode `drwx------`.

4. Check that the image has a health check:

   ```bash
   podman image inspect localhost/extensions:HEAD --format '{{.Config.Healthcheck.Test}}'
   ```

   The output is `[CMD /home/extensions/app/healthcheck.sh]`.

## Run the image in the local PMM

1. Copy the image from Podman's store into Docker:

   ```bash
   podman save localhost/extensions:HEAD | docker load
   ```

   The output is `Loaded image: localhost/extensions:HEAD`.

2. Start the local PMM with that image and the host that your app needs. Leave out `compose.checkout.yaml`, so that the code comes from the image:

   ```bash
   PMM_EXTENSIONS_IMAGE=localhost/extensions:HEAD docker compose -f sidecar/dev/compose.yaml \
     --profile postgresql up -d --wait
   ```

3. Confirm that PMM Extensions serves the app, and run one task: follow steps 2 to 7 of [Run the app in a local PMM](port-an-app.md#run-the-app-in-a-local-pmm).

## Add the changelog fragment

A new app is a change that users see, so it needs a changelog fragment.

1. Create the fragment, with the ticket key of your change:

   ```bash
   make changelog-add TICKET=PMM-NNNNN SECTION=added MSG="PostgreSQL Backups runs pgBackRest backups from PMM."
   ```

2. Commit the file that the command created under `changelog.d/`. [Contributing](../../CONTRIBUTING.md#changelog-fragments) has the rules.

## Open the pull requests

1. Open a pull request in this repository. It holds the line in `sidecar/settings.yaml`, the test constant, the changelog fragment, and the app's move to "What ships" in [App status](../app-status.md).

2. Open a pull request in `percona/pmm` with the UI change. Link each pull request from the other.

Both must merge before a PMM release for users to see the app. [Ownership of the framework and the apps](../explanation/ownership.md) says who reviews what.

## Verify

The four image checks pass. The image that you built serves the app in the local PMM, and one task has finished with the status `success`.

## Next steps

- [The sidecar image](../../sidecar/README.md), for the image's settings
- [How the sidecar runs inside PMM](../explanation/architecture.md)
