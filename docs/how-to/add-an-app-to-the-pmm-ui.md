# Add an app to the PMM UI

This page shows how to give an app a page and a menu item in PMM's UI. The example is PostgreSQL Backups, `backup_pg`.

Two facts first:

- This page is for an app whose pages PMM's UI draws from the app's form schema. An app with pages written by hand needs those pages written in PMM's UI, which this page does not cover.
- Users see the app only after this change merges in `percona/pmm` and a PMM release carries it.

The UI is in the `percona/pmm` repository, under `ui/`. Each app needs four edits there, and two tests list the menu's items.

## Prerequisites

- The app runs in a local PMM: see [Port an app](port-an-app.md).
- A checkout of `percona/pmm`. To try the change in the local PMM, check out the tag of the version that it runs, `v3.10.0`.
- Node.js and `pnpm`, at the versions that `ui/package.json` in that checkout asks for. At `v3.10.0` those are Node.js 22 or later and `pnpm` 11 or later.

The four files are in `ui/apps/pmm/src/` of the `percona/pmm` checkout. Run the commands on this page from `ui/`, unless a step says otherwise.

## Choose the names

| Name | Example | Used for |
|---|---|---|
| The app's key | `backup_pg` | What PMM Extensions calls the app. `GET /extensions/api/apps/` lists it as `app_key`. |
| The path | `postgresql-backups` | The last part of the page's URL. |
| The constant | `BACKUP_PG` | The names of the constants in the four edits. |

## Add the path constant

1. In `lib/constants.ts`, add a line below the one that starts with `export const EXTENSIONS_MYSQL_BACKUPS_PATH`:

   ```ts
   export const EXTENSIONS_BACKUP_PG_PATH = `${PMM_NEW_NAV_PATH}/extensions/postgresql-backups`;
   ```

## Add the route

1. In `router.tsx`, add a line to the import from `'lib/constants'`, below `EXTENSIONS_MYSQL_BACKUPS_PATH,`:

   ```ts
     EXTENSIONS_BACKUP_PG_PATH,
   ```

2. In the same file, add this route above the line `// Fallback`:

   ```tsx
               {
                 path: `${relativeToNav(EXTENSIONS_BACKUP_PG_PATH)}/*`,
                 element: (
                   <ExtensionsPage>
                     <SchemaDrivenPlugin
                       pluginName="backup_pg"
                       routeBase={EXTENSIONS_BACKUP_PG_PATH}
                     />
                   </ExtensionsPage>
                 ),
               },
   ```

   `pluginName` is the app's key. `SchemaDrivenPlugin` asks PMM Extensions for that app's schema and draws the list, the form and the task's page from it. `routeBase` must be the constant that the `path` uses.

## Add the menu item

1. In `contexts/navigation/navigation.constants.ts`, add the same line to the import from `'lib/constants'`:

   ```ts
     EXTENSIONS_BACKUP_PG_PATH,
   ```

2. At the end of the file, add the item:

   ```ts
   export const NAV_EXTENSIONS_BACKUP_PG: NavItem = {
     id: 'extensions-backup-pg',
     icon: Restore,
     text: 'PostgreSQL Backups',
     url: EXTENSIONS_BACKUP_PG_PATH,
     matches: ['*'],
   };
   ```

   `text` is what the menu shows. `icon` is one of the icons that the file imports at its top. To use another icon, import it there.

## Add the item to the menu

1. In `contexts/navigation/navigation.utils.tsx`, add a line to the import from `'./navigation.constants'`:

   ```ts
     NAV_EXTENSIONS_BACKUP_PG,
   ```

2. In the same file, find the list in `addExtensionsApps`, and add the item at its end:

   ```tsx
       [NAV_EXTENSIONS_MYSQL_BACKUPS, NAV_EXTENSIONS_ATW, NAV_EXTENSIONS_BACKUP_PG]
   ```

   The menu shows the items in the order of this list. Add yours at the end: two tests read the first two items by position.

## Update the tests that list the menu

Two test files repeat the list of items. Without this step, four tests fail.

1. Open `contexts/navigation/navigation.utils.test.ts`. Find the list that holds `'extensions-mysql-backups'` and `'extensions-atw'`, and add your item's `id` at its end:

   ```ts
       expect(children.map((child) => child.id)).toEqual([
         'extensions-mysql-backups',
         'extensions-atw',
         'extensions-backup-pg',
       ]);
   ```

2. Open `contexts/navigation/navigation.provider.test.tsx`. Find the list with the same two entries, and add the same line at its end.

3. Run the navigation tests:

   ```bash
   pnpm --filter ./apps/pmm exec vitest run src/contexts/navigation
   ```

   The output ends with:

   ```text
    Test Files  3 passed (3)
         Tests  43 passed (43)
   ```

## Build the UI

1. Install the dependencies:

   ```bash
   pnpm install
   ```

2. Check the format of your edits:

   ```bash
   pnpm format:check
   ```

   The output includes `All matched files use the correct format.` If it lists a file, run `pnpm format`.

3. Build:

   ```bash
   pnpm build
   ```

   The build ends with a line that says `2 successful, 2 total`. The result is in `ui/apps/pmm/dist/`.

## Serve the build in the local PMM

The local PMM can serve a UI from a directory in place of the UI inside the PMM image.

1. From the root of your `pmm-extensions` checkout, start the local PMM with one more Compose file. Set `PMM_UI_DIST` to the full path of the build:

   ```bash
   PMM_UI_DIST=/path/to/pmm/ui/apps/pmm/dist docker compose -f sidecar/dev/compose.yaml \
     -f sidecar/dev/compose.checkout.yaml -f sidecar/dev/compose.ui.yaml \
     --profile postgresql up -d --wait
   ```

   Give the same `-f` files and profiles that you started the local PMM with, and add `-f sidecar/dev/compose.ui.yaml`. Compose recreates the PMM container, and the data is kept.

2. After each later `pnpm build`, reload the page in the browser. The container reads the directory directly.

## Verify

1. Open `https://127.0.0.1:8443` in a browser and sign in.
2. In the menu, expand **Apps** and select **PostgreSQL Backups**.
3. Confirm that the page lists the app's tasks, and that the button above the list opens the app's form.

## Next steps

- [Get an app into a PMM release](get-an-app-into-a-pmm-release.md)
- [Run PMM Extensions in a local PMM](run-in-local-pmm.md)
