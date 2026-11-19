# App status

This page lists every app in this repository: the five that PMM ships, and the twelve that it does not. For each app that does not ship, it says what the app does and what state it is in. It also says what it costs to bring the app into PMM.

The page states facts about the code. It does not rank the apps by value.

## What ships

The PMM Extensions image ships the apps that `sidecar/settings.yaml` lists. Today there are five.

| App | Where a user finds it |
|---|---|
| MySQL Backups (`mysql_backups`) | **Apps → MySQL Backups** in PMM |
| Support diagnostics (`atw`) | **Apps → Support diagnostics** in PMM |
| Inventory (`inventory`) | Nowhere. It has no page. The other apps read their services and hosts from it. |
| Operations Inventory (`om_inventory`) | Nowhere. PMM's own pages for MongoDB operations read from it. |
| Operations Bootstrap (`om_bootstrap`) | Nowhere. PMM's own pages for MongoDB operations call it. |

PMM's documentation describes the two apps that have pages.

## How the cost is judged

An app appears in PMM after two changes. In this repository, it is added to the list that the image ships. In `percona/pmm`, the UI gets a route and a navigation entry for it. The second change decides most of the cost, and it depends on how the app draws its pages.

| Cost | Meaning |
|---|---|
| S | The app's pages are drawn from its form schema. PMM's UI needs four small edits and no new page. [Port an app](how-to/port-an-app.md) describes the work. |
| M | As S, and the app also has pages or page parts of its own. That code must be moved into PMM's UI. |
| L | One of three things holds. The app's whole UI is written by hand. The app depends on something the image does not ship. The app overlaps a PMM feature and needs a product decision first. |

Every app that runs a tool needs that tool on the database host. PMM Extensions installs nothing on a host.

Only PostgreSQL Backups has run inside PMM. The other eleven entries below rest on the apps' tests and on their code. PostgreSQL Backups passed all its tests and still failed on its first run on a real host. Expect the same kind of defect in the others.

## Apps that do not ship

The test counts are from each app's own tests on the current `main`.

### PostgreSQL Backups (`backup_pg`)

- **What it does:** Runs pgBackRest backups of a PostgreSQL service: full, incremental and differential, with retention settings. On the host it runs `pgbackrest`, as the `postgres` user.
- **State:** 898 lines of Python. 143 tests pass. Its pages are drawn from its form schema. It has run inside PMM: the app appeared under **Apps**, and one backup finished.
- **What stands between it and PMM:** A host with pgBackRest and a configured stanza. Nothing else is known.
- **Cost:** S.

### Checksums (`checksums`)

- **What it does:** Checks that MySQL replicas hold the same data as their source. It runs `pt-table-checksum` from Percona Toolkit, on the databases and tables that the user selects.
- **State:** 905 lines of Python. 72 tests pass. Its pages are drawn from its form schema.
- **What stands between it and PMM:** A host with Percona Toolkit. It has not run inside PMM.
- **Cost:** S.

### Archives (`archives`)

- **What it does:** Deletes or archives the rows of a MySQL table that match a condition. It runs `pt-archiver` from Percona Toolkit, and it can export rows to a file or copy them to another table.
- **State:** 1,586 lines of Python. 150 tests pass. Its pages are drawn from its form schema.
- **What stands between it and PMM:** A host with Percona Toolkit. Its form uses groups where the user picks one of several branches. Nobody has checked that PMM's UI draws them.
- **Cost:** S.

### Schema Change (`alters`)

- **What it does:** Changes the schema of a MySQL table without blocking reads and writes. It runs `pt-online-schema-change` from Percona Toolkit. Each change also gets a dry run and a set of checks that run first.
- **State:** 2,467 lines of Python. 146 tests pass. Its list and its form are drawn from its form schema. Its detail view is written by hand, in 149 lines of frontend code.
- **What stands between it and PMM:** A host with Percona Toolkit, and the detail view, which must be moved into PMM's UI.
- **Cost:** M.

### MongoDB Backups (`backup_mongo`)

- **What it does:** Manages backups with Percona Backup for MongoDB: logical and physical backups, storage settings, point-in-time recovery and restores. On the host it runs `pbm`.
- **State:** 4,448 lines of Python, the largest app. 550 tests pass. Its list is drawn from its form schema. Its forms and detail views are written by hand, in 1,052 lines of frontend code.
- **What stands between it and PMM:** The hand-written pages, and a product decision. PMM's own **Backups** pages already back up MongoDB with the same tool.
- **Cost:** L.

### Topology (`topology`)

- **What it does:** Draws the replication topology of a MySQL service as a graph. A task on the host reads the replication state from MySQL.
- **State:** 1,662 lines of Python. 40 tests pass. Its whole UI is written by hand, in 830 lines of frontend code.
- **What stands between it and PMM:** The graph view, which must be moved into PMM's UI.
- **Cost:** L.

### Task Manager (`tasks`)

- **What it does:** Lists the tasks of every app in one place. It runs nothing on a host.
- **State:** 407 lines of Python. 30 tests pass. Its UI is written by hand, in 665 lines of frontend code.
- **What stands between it and PMM:** The list page, which must be moved into PMM's UI.
- **Cost:** M.

### Health & Security Report (`report`)

- **What it does:** Builds a report on a PMM installation for a period that the user chooses. It reads advisors, alerts, backups, disk use, uptime and inventory from PMM's API, and it produces an HTML or PDF document. It runs nothing on a host.
- **State:** 2,535 lines of Python. 177 tests pass. Its UI is written by hand, in 916 lines of frontend code.
- **What stands between it and PMM:** The hand-written pages, and the libraries that produce the PDF. The image was built without them.
- **Cost:** L.

### Dipper Data Collection (`dipper`)

- **What it does:** Collects data about a database host and its PMM graphs for a support engagement. It runs collection scripts on the host for MySQL, MongoDB, PostgreSQL and Valkey, and it archives the results.
- **State:** 2,655 lines of Python. 137 tests pass. Its UI is written by hand, in 397 lines of frontend code.
- **What stands between it and PMM:** The hand-written pages, and a product decision. Support diagnostics, which ships, also collects data from hosts with the same script library.
- **Cost:** L.

### Alert Templates (`alerts`)

- **What it does:** Creates alert rules in PMM from a set of templates, and configures where the alerts are sent. It also keeps backups of the alert configuration. It uses PMM's API and runs nothing on a host.
- **State:** 1,834 lines of Python. 231 tests pass. Its UI is a wizard written by hand, in 1,187 lines of frontend code. It is the only one of the twelve that has a database table of its own.
- **What stands between it and PMM:** The hand-written wizard, and a product decision. PMM has its own alert rule templates.
- **Cost:** L.

### Alert Troubleshooting (`alert_troubleshooting`)

- **What it does:** Links each alert to the diagnostic scripts that help investigate it. It runs the linked scripts through Snippet Manager.
- **State:** 599 lines of Python. 60 tests pass. Its UI is written by hand, in 356 lines of frontend code.
- **What stands between it and PMM:** Snippet Manager, which the image leaves out, and the hand-written pages.
- **Cost:** L.

### Snippet Manager (`snippets`)

- **What it does:** Holds a catalog of approved diagnostic scripts. A user browses them, an administrator approves them, and a user runs one on a chosen host.
- **State:** 384 lines of Python in the app, and 5,123 in the script engine it shares with Support diagnostics. 117 tests pass. Its UI is written by hand, in 1,545 lines of frontend code.
- **What stands between it and PMM:** A decision. The image leaves the app out on purpose, and a test asserts that. The script library itself ships, and Support diagnostics uses it.
- **Cost:** Not judged. It is not a candidate as it stands.

## Summary

| App | Tool on the host | Pages | Cost |
|---|---|---|---|
| PostgreSQL Backups | pgBackRest | From the schema | S |
| Checksums | Percona Toolkit | From the schema | S |
| Archives | Percona Toolkit | From the schema | S |
| Schema Change | Percona Toolkit | From the schema, with one view of its own | M |
| Task Manager | none | Written by hand | M |
| Topology | none beyond MySQL | Written by hand | L |
| Health & Security Report | none | Written by hand | L |
| MongoDB Backups | Percona Backup for MongoDB | Mostly written by hand | L, after a decision |
| Dipper Data Collection | Collection scripts | Written by hand | L, after a decision |
| Alert Templates | none | Written by hand | L, after a decision |
| Alert Troubleshooting | The linked scripts | Written by hand | L, after Snippet Manager is decided |
| Snippet Manager | The chosen script | Written by hand | Not a candidate as it stands |
