# Build your first app

In this tutorial you build an app named Disk Free. It runs `df` on a database host and shows the result in PMM.

You scaffold the app, describe its form, test it, run it in a local PMM, and add it to PMM's UI. Each command shows the output to expect.

## Before you start

You need:

- A development environment with the virtualenv active: see [Set up a development environment](../how-to/set-up-a-development-environment.md).
- Docker with Compose, on an x86-64 machine or a Mac with Apple Silicon, with port 8443 free. [Run PMM Extensions in a local PMM](../how-to/run-in-local-pmm.md) lists the details.
- Node.js 22 or later and `pnpm` 11 or later, for the last part.

Run every command from the root of this repository.

## Scaffold the app

1. Generate the app:

   ```bash
   make startapp NAME=diskfree TYPE=task DISPLAY_NAME="Disk Free" COMMAND=df NO_INPUT=1
   ```

   The output ends with:

   ```text
   Registered 'diskfree' DISABLED in settings.yaml.

   Before you commit, create the app's API snapshots and regenerate the API client:
         make regen-specs
   Commit the files it writes under tests/app/extensions/snapshots/ and frontend/packages/api/ with the app.
   See docs/how-to/update-api-snapshots.md.

   To run the app inside PMM, add this line under EXTENSIONS.APPS in sidecar/settings.yaml:
         - MODULE_NAME: diskfree
   Then follow docs/how-to/run-in-local-pmm.md.
   ```

   The app is in a new directory, `diskfree`, under `app/extensions/apps/`. Its tests are in a directory of the same name under `tests/app/extensions/apps/`.

## Describe the form

The form model is the one place where you say what the app asks for. The framework builds the form, the validation and the command from it.

1. In `models.py` in the app's directory, replace the example field named `message` with these two fields:

   <!-- constructed -->
   ```python
       block_size: Annotated[
           str,
           ArgFormat(),
           Ui(section="Task", description="Unit for sizes, for example 1M"),
       ] = "1M"
       total: Annotated[
           bool,
           ArgFormat(),
           Ui(section="Task", description="Print a grand total row"),
       ] = True
   ```

   Each field becomes an option of the command: `block_size` becomes `--block-size=1M`, and `total` becomes `--total`. A field cannot be a bare positional argument, such as a path after `df`. Every field needs an option name. The file already imports the names that the two fields use.

2. In the docstring of the class, replace the `:param message:` line with these two:

   ```text
       :param block_size: The unit that sizes are printed in.
       :param total: Whether a grand total row is printed.
   ```

3. In `app.py` in the same directory, set the text of `description=` to `Report free disk space on a database host.`

## Test the app

The scaffolder generated a test that calls every route of your app.

1. Run the app's tests:

   ```bash
   pytest tests/app/extensions/apps/diskfree -q
   ```

   The output ends with a line such as:

   ```text
   31 passed, 14 skipped, 35 warnings in 1.66s
   ```

2. Run the checks that every app must pass:

   ```bash
   pytest tests/app/extensions/apps/framework/test_conformance.py tests/app/extensions/apps/framework/test_form_dsl_conformance.py -q
   ```

   The output ends with a line such as:

   ```text
   144 passed, 22 skipped, 21 warnings in 5.47s
   ```

## Turn the app on

Inside PMM, PMM Extensions serves the apps that `sidecar/settings.yaml` lists.

1. In `sidecar/settings.yaml`, add a line for your app below `- MODULE_NAME: om_bootstrap`, with the same indentation:

   ```yaml
         - MODULE_NAME: om_bootstrap
         - MODULE_NAME: diskfree
   ```

## Start the local PMM with your code

The steps mount your checkout's code, as [Run the code in your checkout](../how-to/run-in-local-pmm.md#run-the-code-in-your-checkout) explains. When your checkout needs a newer Python than the image has, that section says which image to set first.

1. Start PMM, PMM Extensions and a MySQL host, with your checkout's code in place of the released code:

   ```bash
   docker compose -f sidecar/dev/compose.yaml -f sidecar/dev/compose.checkout.yaml \
     --profile mysql up -d --wait
   ```

   The first run pulls two images and builds a third. Here that took about seven minutes. The command returns when all three containers are healthy:

   ```text
    Container pmm-extensions-dev-pmm-server-1 Healthy
    Container pmm-extensions-dev-pmm-extensions-1 Healthy
    Container pmm-extensions-dev-mysql-host-1 Healthy
   ```

   If the local PMM was already running with your checkout's code, Compose leaves the `pmm-extensions` container as it is. Step 4 then does not list your app. Restart the container:

   ```bash
   docker compose -f sidecar/dev/compose.yaml -f sidecar/dev/compose.checkout.yaml \
     --profile mysql up -d --wait --force-recreate pmm-extensions
   ```

2. Sign in to PMM and keep the session cookie in a file that only you can read:

   ```bash
   install -m 600 /dev/null ~/.pmm-cookies
   curl -sk -c ~/.pmm-cookies -X POST https://127.0.0.1:8443/graph/login \
     -H 'Content-Type: application/json' -d '{"user":"admin","password":"admin"}'
   ```

   The output is `{"message":"Logged in","redirectUrl":"/graph/"}`.

3. Exchange the session for a token:

   ```bash
   TOKEN=$(curl -sk -b ~/.pmm-cookies -X POST \
     https://127.0.0.1:8443/extensions/api/oauth/session/exchange | python3 -c \
     'import json,sys; print(json.load(sys.stdin)["access_token"])')
   ```

4. List the apps that PMM Extensions serves:

   ```bash
   curl -sk -H "Authorization: Bearer $TOKEN" \
     https://127.0.0.1:8443/extensions/api/apps/ | python3 -c \
     'import json,sys; [print(a["app_key"]) for a in json.load(sys.stdin)]'
   ```

   The last line of the output is your app:

   ```text
   inventory
   atw
   mysql_backups
   mysql_backups/restore
   om_inventory
   om_bootstrap
   diskfree
   ```

## Run a task

1. Tell PMM Extensions to copy the services from PMM's inventory now:

   ```bash
   curl -sk -X POST -H "Authorization: Bearer $TOKEN" -o /dev/null -w '%{http_code}\n' \
     https://127.0.0.1:8443/extensions/api/apps/inventory/sync/
   ```

   The output is `202`.

2. After ten seconds, list the services:

   ```bash
   curl -sk -H "Authorization: Bearer $TOKEN" \
     https://127.0.0.1:8443/extensions/api/extensions/services/ | python3 -c \
     'import json,sys; [print(s["id"], s["type"], s["name"]) for s in json.load(sys.stdin)["items"]]'
   ```

   The output has a line for the MySQL host's service:

   ```text
   2 mysql dev-mysql
   1 postgresql pmm-server-postgresql
   ```

3. Create a task for `dev-mysql`. If its identifier in your list is not `2`, replace the `2` in the command:

   ```bash
   curl -sk -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -o /dev/null -w '%{http_code}\n' https://127.0.0.1:8443/extensions/api/apps/diskfree/ \
     -d '{"task_name":"first-report","hostname":"mysql-host","service_id":2}'
   ```

   The output is `201`.

4. Read the command that the framework built from your form:

   ```bash
   curl -sk -H "Authorization: Bearer $TOKEN" \
     https://127.0.0.1:8443/extensions/api/apps/diskfree/first-report | python3 -c \
     'import json,sys; m=json.load(sys.stdin)["data"]["meta"]; print(m["command"], m["args"])'
   ```

   The output is the command with your two fields as options:

   ```text
   df --block-size=1M --total
   ```

5. Run the task:

   ```bash
   curl -sk -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     https://127.0.0.1:8443/extensions/api/apps/diskfree/first-report/execute -d '{}'
   ```

   The output names the run:

   ```json
   {"task_name":"first-report","task_id":9,"status":"running","created_at":"2026-11-18T23:29:20Z"}
   ```

6. After ten seconds, read the task's status:

   ```bash
   curl -sk -H "Authorization: Bearer $TOKEN" \
     https://127.0.0.1:8443/extensions/api/apps/diskfree/first-report | python3 -c \
     'import json,sys; print(json.load(sys.stdin)["status"])'
   ```

   The output is `success`.

7. Read what `df` printed. If the `task_id` in step 5 was not `9`, replace the `9` in the command:

   ```bash
   curl -sk -H "Authorization: Bearer $TOKEN" https://127.0.0.1:8443/extensions/stream-logs/9
   ```

   The output starts with the report and ends with the result:

   ```text
   data: {"step":"run-script","type":"stdout","msg":"Filesystem     1M-blocks   Used Available Use% Mounted on\noverlay ...

   event: finish
   data: {"status": "success"}
   ```

## Add the app to PMM's UI

PMM's UI has a route and a menu item for each app, in its own repository. You add both for Disk Free, and then you build the UI.

1. Clone PMM next to this repository, at the version that the local PMM runs:

   ```bash
   git clone --depth 1 --branch v3.10.0 https://github.com/percona/pmm.git ../pmm
   ```

   Git warns that the tag is not a commit, and prints a note about a detached HEAD. Both are expected.

   The four files to edit are in `../pmm/ui/apps/pmm/src/`.

2. In `lib/constants.ts`, add a line below the one that starts with `export const EXTENSIONS_MYSQL_BACKUPS_PATH`:

   ```ts
   export const EXTENSIONS_DISKFREE_PATH = `${PMM_NEW_NAV_PATH}/extensions/disk-free`;
   ```

3. In `router.tsx`, add a line to the import from `'lib/constants'`, below `EXTENSIONS_MYSQL_BACKUPS_PATH,`:

   ```ts
     EXTENSIONS_DISKFREE_PATH,
   ```

   In the same file, add this route above the line `// Fallback`:

   ```tsx
               {
                 path: `${relativeToNav(EXTENSIONS_DISKFREE_PATH)}/*`,
                 element: (
                   <ExtensionsPage>
                     <SchemaDrivenPlugin
                       pluginName="diskfree"
                       routeBase={EXTENSIONS_DISKFREE_PATH}
                     />
                   </ExtensionsPage>
                 ),
               },
   ```

4. In `contexts/navigation/navigation.constants.ts`, add the same line to the import from `'lib/constants'`:

   ```ts
     EXTENSIONS_DISKFREE_PATH,
   ```

   At the end of the file, add the menu item. The file already imports its icon:

   ```ts
   export const NAV_EXTENSIONS_DISKFREE: NavItem = {
     id: 'extensions-diskfree',
     icon: MemoryOutlined,
     text: 'Disk Free',
     url: EXTENSIONS_DISKFREE_PATH,
     matches: ['*'],
   };
   ```

5. In `contexts/navigation/navigation.utils.tsx`, add a line to the import from `'./navigation.constants'`, below `NAV_EXTENSIONS_MYSQL_BACKUPS,`:

   ```ts
     NAV_EXTENSIONS_DISKFREE,
   ```

   In the same file, replace the line `[NAV_EXTENSIONS_MYSQL_BACKUPS, NAV_EXTENSIONS_ATW]` with:

   ```tsx
       [NAV_EXTENSIONS_MYSQL_BACKUPS, NAV_EXTENSIONS_ATW, NAV_EXTENSIONS_DISKFREE]
   ```

6. Build the UI, and return to this repository:

   ```bash
   cd ../pmm/ui
   pnpm install
   pnpm build
   cd -
   ```

   The output includes:

   ```text
    Tasks:    2 successful, 2 total
   ```

   Here the two commands took under two minutes together.

7. Start the local PMM again, with the UI that you built:

   ```bash
   PMM_UI_DIST=$PWD/../pmm/ui/apps/pmm/dist docker compose -f sidecar/dev/compose.yaml \
     -f sidecar/dev/compose.checkout.yaml -f sidecar/dev/compose.ui.yaml \
     --profile mysql up -d --wait
   ```

   Compose recreates the PMM container. The task that you ran is kept.

8. Open `https://127.0.0.1:8443` in a browser and sign in as `admin` with the password `admin`. The browser warns about the certificate, because PMM signs its own. PMM then asks you to change the password: select **Skip**.

9. In the menu, expand **Apps** and select **Disk Free**. The page lists one task, `first-report`, with the status **Done**.

   PMM shows a banner that says PMM Server and PMM Extensions are at different versions. Your checkout's code is newer than the released PMM, so the banner is expected here.

## Stop the local PMM

1. Stop the containers and remove their data:

   ```bash
   docker compose -f sidecar/dev/compose.yaml --profile mysql down -v
   ```

2. Remove the file that holds the session:

   ```bash
   rm -f ~/.pmm-cookies
   ```

   The app, the line in `sidecar/settings.yaml` and the UI edits stay in your two checkouts.

## What you built

You built an app from one form model. The framework derived its routes, its form, its validation and its command. The app runs in your checkouts only.

## Next steps

- [The app framework](../explanation/app-framework.md), for how one form model becomes an app
- [Form DSL](../reference/form-dsl.md), for the other field types and for rules between fields
- [Test an app](../how-to/test-an-app.md)
- [Update the API snapshots](../how-to/update-api-snapshots.md), because the whole test suite fails until you regenerate the snapshots for a new app
- [Port an app](../how-to/port-an-app.md), for the test that compares its list of apps with `sidecar/settings.yaml`
- [Add an app to the PMM UI](../how-to/add-an-app-to-the-pmm-ui.md), which explains the four edits
- [Get an app into a PMM release](../how-to/get-an-app-into-a-pmm-release.md)
