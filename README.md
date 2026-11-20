# PMM Extensions

PMM Extensions adds apps to [Percona Monitoring and Management](https://docs.percona.com/percona-monitoring-and-management/3/) (PMM). An app runs a task on a database host that PMM monitors, such as a backup, and shows the result in PMM.

## How it runs

PMM Extensions runs only as a sidecar: one container beside `pmm-server`. PMM gives it a database, its secrets and sign-in. PMM also serves its API, under the path `/extensions`. PMM Extensions has no deployment of its own and no UI of its own.

Version 0.13.1 was the last release that ran without PMM. Its installer and its image stay available from that release tag.

## What ships

The image ships five apps. Two have pages in PMM, under **Apps**: MySQL Backups and Support diagnostics. The other three have no page. The apps and PMM's own pages use them: Inventory, Operations Inventory and Operations Bootstrap.

This repository holds twelve more apps that do not ship. [App status](docs/app-status.md) lists all seventeen.

## Where the UI is

The pages are part of PMM's UI, in the [`percona/pmm`](https://github.com/percona/pmm) repository under `ui/`. The package `ui/packages/extensions` draws an app's pages from the app's form schema. The app `ui/apps/pmm` holds each app's route and menu item.

## Versions and images

PMM Extensions has the same version as PMM. The image is [`percona/pmm-extensions`](https://hub.docker.com/r/percona/pmm-extensions) on Docker Hub. Its tags are the full version, such as `3.10.0`, and the moving tags `3.10`, `3` and `latest`.

[CHANGELOG.md](CHANGELOG.md) lists the changes in each version. The [releases page](https://github.com/percona/pmm-extensions/releases) holds the release notes.

## Documentation

To use PMM Extensions, read PMM's documentation. The pages below are for people who change this repository. One page is for people who run a standalone instance: the migration guide.

| Goal | Page |
|---|---|
| Build a first app, step by step | [Build your first app](docs/tutorials/first-app.md) |
| Set up a checkout for development | [Set up a development environment](docs/how-to/set-up-a-development-environment.md) |
| Run PMM Extensions on your machine | [Run PMM Extensions in a local PMM](docs/how-to/run-in-local-pmm.md) |
| Test an app | [Test an app](docs/how-to/test-an-app.md) |
| Make an app of this repository run inside PMM | [Port an app](docs/how-to/port-an-app.md) |
| Give an app a page in PMM's UI | [Add an app to the PMM UI](docs/how-to/add-an-app-to-the-pmm-ui.md) |
| Build the image and ship an app | [Get an app into a PMM release](docs/how-to/get-an-app-into-a-pmm-release.md) |
| Move MySQL backup schedules from a standalone instance to PMM | [Migrate MySQL backup schedules from standalone SEP](docs/how-to/migrate-from-standalone-sep.md) |
| Rerun a schema step without a restart | [Rerun a schema step in a running sidecar](docs/how-to/rerun-a-schema-step.md) |
| Run the type check | [Run the type check](docs/how-to/run-the-type-check.md) |
| Update the snapshots after a route or a form changes, or create them for a new app | [Update the API snapshots](docs/how-to/update-api-snapshots.md) |
| Write a script for Support diagnostics | [Write a diagnostic script](docs/how-to/write-a-diagnostic-script.md) |
| Look up a field type or a rule of a form | [Form DSL](docs/reference/form-dsl.md) |
| Look up what an app can replace in the framework | [App extension points](docs/reference/app-extension-points.md) |
| Look up the scaffolder's variables and output | [The scaffolder](docs/reference/scaffolder.md) |
| Look up what the type check enforces | [Type-check policy](docs/reference/type-check-policy.md) |
| Look up the categories of a diagnostic script | [Diagnostic categories](docs/reference/diagnostic-categories.md) |
| Look up the image's settings, volumes and ports | [The sidecar image](sidecar/README.md) |
| Understand how the sidecar fits into PMM | [How the sidecar runs inside PMM](docs/explanation/architecture.md) |
| Understand how one form model becomes an app | [The app framework](docs/explanation/app-framework.md) |
| Understand who owns the framework and each app | [Ownership of the framework and the apps](docs/explanation/ownership.md) |
| See which apps ship and what the others need | [App status](docs/app-status.md) |
| Contribute a change | [Contributing](CONTRIBUTING.md) |
| Write or edit a page | [Writing standard](docs/writing-standard.md) |
