# Try PMM Extensions on a PMM feature build

A throwaway, single-machine environment for testing the new **Management**
pages in PMM before they ship. It runs a PMM *feature build* (a pre-release
image cut from a pull request) with PMM Extensions plugged in, plus an optional
MySQL server preloaded with sample data, so you have something to back up.

> [!NOTE]
> This is for testing only. You can't install or upgrade from it. The PMM image
> can be rebuilt at any time, and everything listens on `127.0.0.1` only.

## What you get

| Container | What it is |
|---|---|
| `pmm-server` | PMM feature build, with the Management pages in the sidebar |
| `extensions-sidecar` | The PMM Extensions backend: MySQL Backups and Support diagnostics |
| `extensions-mysql` *(optional)* | Percona Server 8.4 with PMM Client, XtraBackup and mydumper, preloaded with the [`employees`](https://github.com/datacharmer/test_db) sample database (300k employees, 2.8M salary rows, about 125 MB) |

## Quick start

You need Docker (or Podman) with Compose.

```bash
git clone -b pmm https://github.com/percona/pmm-extensions.git
cd pmm-extensions/sidecar/pmm-fb
./bootstrap.sh
docker compose --profile mysql up -d --build
```

This starts PMM, PMM Extensions, and a MySQL server preloaded with sample data
to back up. The first run builds the MySQL image, which takes a few minutes.

Only checking the UI or sign-in? `docker compose up -d` (no `bootstrap.sh`)
starts just PMM and PMM Extensions. It's faster, but there's no MySQL server,
so you can't run backups.

Open <https://127.0.0.1:8443>, accept the self-signed certificate, sign in as
`admin` / `admin` and click **Management** in the sidebar. You don't need a
second login.

**First start is slow.** PMM takes a couple of minutes, and
`extensions-sidecar` stays in `Created` until PMM is healthy. That's expected.
The sample-data import then takes a few more minutes. Wait
for this line before you run a backup:

```bash
docker compose logs -f extensions-mysql   # wait for: Imported the employees seed dataset
```

## What to test

**MySQL Backups.** Create a backup with these values:

| Field | Value |
|---|---|
| Execution Host | `extensions-mysql` |
| Database Host | the `extensions-mysql` MySQL service |
| MySQL defaults file | `/root/.my.cnf` |
| XtraBackup defaults file | `/root/.my.cnf` (XtraBackup only) |

- The MySQL service can take up to 15 minutes to appear in **Database Host**
  after first start. PMM Extensions picks it up on its next inventory sync.
- Use `zstd` or `lz4` compression. `quicklz` always fails with XtraBackup 8.4.
- Only local destinations work. S3 and GCS aren't set up.

**Support diagnostics.** This page needs a Percona Support connection. Without
a subscription it stays on the setup screen.

## Stop and clean up

```bash
docker compose --profile mysql down      # stop, keep data
docker compose --profile mysql down -v   # stop and delete all data
```

## If something goes wrong

- **No MySQL server to pick under Database Host.** You probably started
  without the MySQL server. Run `./bootstrap.sh` and
  `docker compose --profile mysql up -d --build`. If you did start it, give
  the inventory sync up to 15 minutes.
- **Backup fails at "connect".** Check that **Execution Host** is
  `extensions-mysql` and not `pmm-server`. The backup runs on the execution
  host.
- **Apple Silicon or another arm64 machine.** PMM runs under x86-64 emulation,
  so it's slower. Run `./bootstrap.sh` before starting the MySQL profile so
  `extensions-mysql` builds natively. Under emulation every backup fails with
  an empty log. See [DEVELOPING.md § Caveats](DEVELOPING.md#caveats).
- **Ports 8443 or 9000-9002 are already in use.** You may have a stack from an
  older checkout still running. See the upgrade notes at the end of
  [DEVELOPING.md § Caveats](DEVELOPING.md#caveats).
- **Can I use my own PMM?** No. The PMM side of this feature isn't in any
  released PMM yet. You can use your own MySQL host, though. See
  [DEVELOPING.md § Using your own MySQL or PMM](DEVELOPING.md#using-your-own-mysql-or-pmm).

## More detail

- [mysql-target.md](mysql-target.md) explains how the MySQL test node works,
  its credentials, and how to reset it.
- [DEVELOPING.md](DEVELOPING.md) is the reference for maintainers: pinning
  images, how the containers connect, calling the API by hand, and every caveat.
