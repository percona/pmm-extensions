# The MySQL target node (`extensions-mysql`)

Behind the `mysql` compose profile. One container carrying Percona Server
8.4.10, a PMM Client, `percona-xtrabackup-84` (8.4.0), `mydumper` (1.0.3) and
the `datacharmer/test_db` employees dataset — the target a MySQL Backups run
executes *on*, not just against. The seed is baked into the image, so first
boot needs no download; it lands as ~125 MB of real data (300,024 employees,
2.8 M salary rows). For how long the import takes and how to tell it has
finished, see [README.md](README.md#bring-up) — this file does not restate
that timing.

**Why one container.** PMM Extensions does no scheduling: it pins a Nomad job to the node
name the operator picked and `raw_exec` runs it as a plain process in that
node's namespace. The XtraBackup payload reads the datadir directly and PMM Extensions
pins its server config to `localhost`, so the datadir, a MySQL server on
loopback and the backup binaries all have to be reachable from that one
namespace. Sharing a datadir volume between a MySQL container and a separate
PMM Client container is the attractive wrong answer: everything upstream of
exec looks correctly wired, and the run then fails on *connect* rather than on
the datadir. Mydumper is the counterpart — it connects over the network to the
node's service address — so the single combined node covers both paths, since
it can reach itself by its own address.

The PMM Client is what makes the node selectable at both ends: `pmm-admin add
mysql` registers the service, which the syncer pulls into PMM Extensions as a backup
source, while the Nomad client `pmm-agent` supervises makes the same host an
executor. Joining Nomad is not enough on its own — PMM Extensions' host list filters on
`Status == ready and raw_exec in Drivers and Drivers.raw_exec.Healthy == true`,
so a node can be joined and still never appear. If it does not, check
`nomad node status -verbose` inside `pmm-server` before suspecting PMM Extensions.

**Prerequisites.** The MySQL service reaches PMM Extensions' backup-source picker only
through the PMM syncer, so trigger a sync (`POST
https://127.0.0.1:8443/extensions/api/apps/inventory/sync/`) once the node has
registered. PMM publishes the Grafana token the syncer needs, so there is no
minting step. Until a sync runs, `extensions-mysql` can appear in PMM Extensions' executor-host
list but its MySQL service is absent from the backup-source picker.

**Credentials.** `./bootstrap.sh` generates three values into `.env`, like
every other secret here — nothing is committed:

| `.env` slot | MySQL account | Used by |
|---|---|---|
| `EXTENSIONS_MYSQL_ROOT_PASSWORD` | `root@localhost` | local administration |
| `EXTENSIONS_MYSQL_BACKUP_PASSWORD` | `extensions_backup@%` | both backup payloads, via `/root/.my.cnf` |
| `EXTENSIONS_MYSQL_PMM_PASSWORD` | `pmm@127.0.0.1` | the PMM exporters |

`extensions_backup` is granted from `%` rather than `127.0.0.1` on purpose: XtraBackup
connects to loopback and Mydumper connects from the service address, and one
credential has to satisfy both. The entrypoint writes it into `/root/.my.cnf`
at mode `0600` on every boot.

Rotating any of them in `.env` after first boot does **not** take effect — the
passwords live in the datadir. Re-bootstrapping means dropping **both**
`extensions-mysql-data` and `extensions-mysql-pmm-config`:

```bash
docker compose --profile mysql down
docker volume rm pmm-extensions-fb_extensions-mysql-data pmm-extensions-fb_extensions-mysql-pmm-config
```

Drop the datadir alone and the exporters keep authenticating with the old `pmm`
password, so monitoring fails quietly. Removing the config volume as well
re-registers the node, which mints new PMM ids and orphans any catalogued
backup rows — the right trade when the datadir those backups came from is being
discarded anyway. The service's third volume, `extensions-mysql-nomad`, carries the
Nomad client's own state and holds no credential, so it is left alone.

**Running a backup.** In the MySQL Backups create form:

- **Execution Host** — `extensions-mysql`
- **Database Host** — the `extensions-mysql` MySQL service
- **MySQL defaults file** — `/root/.my.cnf`
- **XtraBackup defaults file** — `/root/.my.cnf` (XtraBackup runs only)

Set both defaults-file fields explicitly: the general one configures the
payload's own connection to MySQL, while only the XtraBackup one becomes
`--defaults-file` on the `xtrabackup` command line. Left empty they fall back to
`f"{os.environ.get('HOME')}/.my.cnf"`, which becomes the literal `None/.my.cnf`
when `HOME` does not reach the dispatched task — and that surfaces as an
*authentication* failure rather than a missing-file one. A successful run logs
`Connecting to MySQL server host: localhost, user: extensions_backup`.

`--defaults-file` is exclusive — it suppresses `/etc/my.cnf` rather than adding
to it — so the entrypoint writes `[mysqld]` and `[xtrabackup]` groups into
`/root/.my.cnf` beside `[client]`, carrying the datadir and socket the binary
would otherwise have taken from the distro config.

Both host fields matter independently. Leaving **Execution Host** on
`pmm-server` while pointing **Database Host** at the `extensions-mysql` service is the
mistake the form invites, and it does not fall back: XtraBackup runs in
`pmm-server`'s namespace, where the pinned `localhost` has no MySQL and there is
no datadir, so it fails at connect.

Compression and encryption need nothing extra — `zstd`, `lz4`, `gzip`, `gpg` and
XtraBackup's own `xbcrypt` all arrive with the packages the image installs. Two
caveats:

- **`quicklz` will fail.** The form offers it for XtraBackup, but Percona
  XtraBackup 8.4 supports only `lz4` and `zstd` (`xtrabackup --help`), and the
  `qpress` binary that produced `.qp` files is not packaged for this base.
  Nothing in the harness can make that combination work — pick `zstd` or `lz4`.
- **Only local destinations are reachable.** `rsync` is present; S3 and GCS
  uploads need `aws` and `gsutil`, which are not installed and would need
  credentials and a bucket anyway.

None of these paths is exercised by the runs this harness was added for; they
are documented so the next person knows which failures are the image and which
are the code.

**Repinning the feature build.** `${PMM_FB_TAG}` drives both `pmm-server`'s
image and the PMM Client copied into `extensions-mysql`, and the two must stay on the
same build: the client ships its own `nomad` binary that has to speak RPC to
the server's, and a released client beside a feature-build server pairs two
Nomad builds nobody has tested. The one sanctioned exception is an arm64
engine, where `bootstrap.sh` points the build at the released multi-arch
`percona/pmm-client:3.9.1`: its aarch64 Nomad is the version `NOMAD_VERSION`
pins — while its `pmm-agent` is the released one, so client-side changes in the
feature build are not exercised there (README § Caveats).

A repin moves **three** values, not two. `compose.yaml` spells `PMM_FB_TAG`'s
pinned default out on two lines; `NOMAD_VERSION` takes whatever `tools/nomad
version` the new build ships; and `NOMAD_VERSION_FB_TAG` records the build that
version was read from. The third is what makes the pairing enforced rather than
merely intended on the arm64 path, where the build's own Nomad comparison is
vacuous — a released client's Nomad cannot move with the tag. Leave the witness
behind and the arm64 build is refused, naming both tags. CI then re-reads the real
feature-build client on an amd64 runner
(`.github/workflows/pmm-fb-nomad-pin.yaml`) and holds `NOMAD_VERSION` to it, so
a repin that bumped the witness without re-verifying the version is caught
before it merges. A repin is never the case that job waves through: it warns
and skips only when the PR left both `PMM_FB_TAG` and `NOMAD_VERSION` exactly
as the base branch carried them and that inherited build has since been
collected from `perconalab`. Move either value and an image CI cannot pull
fails the job instead.

Rebuild with `docker compose --profile mysql up -d --build`. Without `--build`
you keep the old client against the new server, and the mismatch is silent:
registration succeeds and only `raw_exec` placement misbehaves.
