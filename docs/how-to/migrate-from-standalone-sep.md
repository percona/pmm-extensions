# Migrate MySQL backup schedules from standalone SEP

This page shows you how to move the MySQL backup schedules of a standalone SEP v0.13.1 instance to PMM Extensions. On this page, standalone SEP means the product that ran outside PMM, up to version 0.13.1.

You export the schedules from the old instance, and a script recreates them in PMM Extensions. Every schedule arrives paused, so no backup runs from both instances at once. You then switch the schedules over one at a time.

## What moves and what does not

The script moves the schedules of the MySQL Backups app, and the backup settings of the tasks they run.

These do not move:

- **Backup history.** The old instance keeps no record of where a finished backup is. The backups stay on disk, and you restore one by typing its location. See [Restore a backup taken before the migration](#restore-a-backup-taken-before-the-migration).
- **Settings.** You enter the settings of PMM Extensions again in PMM.
- **Secrets.** The script copies no password, token or key. A file that a backup setting names, such as a MySQL defaults file, stays on the database host.
- **Users.** People sign in through PMM. The accounts of the old instance are not mapped.
- **Other apps.** The script reads only MySQL Backups schedules.

## Prerequisites

- A standalone SEP v0.13.1 instance that the `sep_installer.sh` script installed. You need a shell on its host, and its install directory.
- A PMM server with PMM Extensions, version 3.10.0 or later. To install it, see [Deploy PMM Extensions with Docker](https://docs.percona.com/percona-monitoring-and-management/3/apps/deploy-pmm-extensions.html) in the PMM documentation.
- A shell on the PMM host, with permission to run `docker`.
- Each database host that the old instance backs up is in PMM, with its MySQL service. PMM Extensions lists it under **Inventory**.
- A PMM user with the Admin role.

The commands on this page were run with Docker, with the old instance and PMM Extensions on the same PMM server. Where a new PMM server differs, the page says so.

## Set the names that the commands use

The commands on this page read these four variables. Type the four lines, with your own values, in the terminal where you run the commands. If you open a new terminal, type them again.

```sh
OLD_DIR="$HOME/sep"
PMM_URL=https://127.0.0.1
PMM_SERVER=pmm-server
PMM_EXTENSIONS=pmm-extensions
```

- `OLD_DIR` is the install directory of the old instance. It holds `compose.yaml`. Only the commands that you run on the host of the old instance use it.
- `PMM_URL` is the address of PMM as seen from the PMM host. Add the port when it is not 443, for example `https://127.0.0.1:8443`.
- `PMM_SERVER` and `PMM_EXTENSIONS` are the names of the two containers on the PMM host. The command `docker ps` lists them.

## Export the schedules from the old instance

Run these steps on the host of the old instance.

1. Create a working directory and download the script into it.

    ```sh
    mkdir migration && cd migration
    curl -fsSLO https://raw.githubusercontent.com/percona/pmm-extensions/main/scripts/migrate_standalone_schedules.py
    ```

2. Run the export inside the `tasks_api` container of the old instance.

    ```sh
    docker compose --file "$OLD_DIR/compose.yaml" --project-name sep exec -T tasks_api python - export < migrate_standalone_schedules.py > standalone-export.json
    ```

   The export reads the address and the token of the old instance from its own settings. You type neither. The command prints a warning like this one from the old instance, and then a count:

    ```text
    [11/18/26 23:05:21] WARNING  app.core.config: [-] The value '*' in config.py:508
                                 SEPSettings.ALLOWED_HOSTS matches any
                                 hostname - use it carefully <69>
    Exported 4 schedules, 4 tasks, 2 services.
    ```

3. Copy the `migration` directory to the PMM host, if that is another machine. If both are on one machine, keep using the same directory.

The file `standalone-export.json` holds every backup setting of the old instance. Treat it as you treat those settings.

## Prepare the PMM host

Run these steps on the PMM host, in the `migration` directory.

1. In PMM, go to **Users and access** > **Service accounts** and select **Add service account**. Give the account the **Admin** role, and add a token to it. For details, see [Service accounts authentication](https://docs.percona.com/percona-monitoring-and-management/3/api/authentication.html).

2. Save the token in a file that only you can read. Run the command, paste the token, press Enter, and then press Ctrl+D.

    ```sh
    (umask 077; cat > pmm.token)
    ```

   The `umask 077` part makes the file readable by you only. The script reads the token from this file. Do not put the token on a command line: it would stay in the shell history.

3. Copy the certificate of PMM out of its container.

    ```sh
    docker cp "$PMM_SERVER":/srv/nginx/certificate.crt pmm-ca.crt
    ```

   PMM creates this certificate itself when you give it none. It is valid for `127.0.0.1` and `localhost` only, which is why `PMM_URL` uses that address. If your PMM has a certificate from a certificate authority that the host trusts, skip this step and leave `--pmm-ca-file pmm-ca.crt` out of the later commands.

4. Read the name of the PMM Extensions image. The script runs in that image, because it holds a Python with the PyYAML package.

    ```sh
    EXT_IMAGE="$(docker inspect "$PMM_EXTENSIONS" --format '{{.Config.Image}}')"
    echo "$EXT_IMAGE"
    ```

    ```text
    percona/pmm-extensions:3.10.0
    ```

## Run the script without changing anything

Without the `--apply` option the script creates nothing. The command starts a short-lived container from the PMM Extensions image. The container sees only the `migration` directory, and it is removed when the script ends.

```sh
docker run --rm --network host --user "$(id -u):$(id -g)" --entrypoint python -v "$PWD":/work -w /work "$EXT_IMAGE" migrate_standalone_schedules.py migrate --export standalone-export.json --pmm-url "$PMM_URL" --pmm-token-file pmm.token --pmm-ca-file pmm-ca.crt --matches matches.json
```

The script writes its proposed matches to `matches.json` and prints its plan:

```text
Wrote the proposed matches to matches.json. Check each one before you run with --apply.
Matches, from matches.json:
  svcdocs-mysql-a: PMM service 3, svcdocs-mysql-a, node 172.31.213.11, port 3306, last synced 2026-11-18T23:08:05.061467Z
  svcdocs-mysql-b: no match

3 schedules to create, each paused:
  svcdocs-a-mydumper on svcdocs-mysql-a (PMM service 3), new task
    every 1 days
  svcdocs-a-xtrabackup on svcdocs-mysql-a (PMM service 3), new task
    cron '0 2 * * *' in UTC
  svcdocs-a-xtrabackup-rsync on svcdocs-mysql-a (PMM service 3), new task
    cron '30 3 * * 0' in America/New_York

1 schedule skipped:
  run_svcdocs-b-xtrabackup_every_12_hours_6791739818912467822: service svcdocs-mysql-b has no match in the match file

0 schedules already in PMM.

Where the old instance wrote backups. The newest one is under 'latest' in each directory:
  svcdocs-mysql-b, task svcdocs-b-xtrabackup: /var/backups/svcdocs/xtrabackup/172.31.213.12
  svcdocs-mysql-a, task svcdocs-a-xtrabackup-rsync: /var/backups/svcdocs-up/xtrabackup/172.31.213.11; copied to rsync: /var/backups/svcdocs-rsync
  svcdocs-mysql-a, task svcdocs-a-xtrabackup: /var/backups/svcdocs/xtrabackup/172.31.213.11
  svcdocs-mysql-a, task svcdocs-a-mydumper: /var/backups/svcdocs/mydumper/172.31.213.11

No write request was sent. Run again with --apply to create the schedules.
```

Keep the last list. It is where the backups of the old instance are, and you need it to restore one.

To run the script without a container, use Python 3.11 or later with PyYAML installed: `python3 migrate_standalone_schedules.py migrate`, with the same options.

## Check the matches

A schedule belongs to a MySQL service. The script must know which PMM service each old service is. It proposes a match in two ways:

1. The two services have the same `external_id`. This is the case when both inventories came from the same PMM server.
2. Otherwise, exactly one PMM service has the same node address and port.

With two candidates, or none, the script proposes no match and gives the reason.

Open `matches.json`. It has one entry for each MySQL service of the old instance:

```json
{
  "svcdocs-mysql-a": {
    "old": {
      "external_id": "2c848d33-f32f-4b20-a6ef-6b93533a074d",
      "node_address": "172.31.213.11",
      "port": 3306
    },
    "pmm_service_id": 3,
    "pmm": {
      "name": "svcdocs-mysql-a",
      "node_address": "172.31.213.11",
      "port": 3306,
      "last_synced_at": "2026-11-18T23:08:05.061467Z"
    },
    "reason": "same external_id"
  },
  "svcdocs-mysql-b": {
    "old": {
      "external_id": "2d7e672a-af56-46fc-b075-b0738b02f221",
      "node_address": "172.31.213.12",
      "port": 3306
    },
    "pmm_service_id": null,
    "pmm": null,
    "reason": "no PMM service has this external_id or this node address and port"
  }
}
```

Check each entry:

- Confirm that `pmm` describes the same database as `old`.
- Look at `last_synced_at`. An old time can mean that the service has left PMM. Check the service under **Inventory** in PMM.
- To change a match, set `pmm_service_id` to the ID of the right PMM service. To leave a service out, set it to `null`.

The script reads only `pmm_service_id`. It creates nothing for a service whose `pmm_service_id` is `null`, and it never writes to this file again. Run the previous command again to see the plan for your changes.

The script also skips a schedule that it cannot copy in full, and names the reason. For example, a task has no `BACKUP_DIR`, which PMM Extensions requires, or it uses a setting that the script does not know. Create such a schedule by hand in PMM.

## Create the schedules

Run the same command with `--apply` at the end.

```sh
docker run --rm --network host --user "$(id -u):$(id -g)" --entrypoint python -v "$PWD":/work -w /work "$EXT_IMAGE" migrate_standalone_schedules.py migrate --export standalone-export.json --pmm-url "$PMM_URL" --pmm-token-file pmm.token --pmm-ca-file pmm-ca.crt --matches matches.json --apply
```

The output ends with this line:

```text
Done: created 3 of 3 schedules. Each one is paused.
```

If PMM Extensions rejects a request, the script stops and says how many schedules it created. Fix the cause and run the same command again. A second run creates only what is missing.

## Verify

1. Run the command of the previous section again, unchanged. The plan now counts the schedules under "already in PMM" and has none to create. A schedule that was skipped is still listed as skipped. The output ends with this line:

    ```text
    Nothing to create.
    ```

2. In PMM, go to **Apps** > **MySQL Backups** and select **Schedules**. Each schedule is listed with its period and time zone, its **Enabled** checkbox is cleared, and **Last Run** shows "Not yet run".

## Switch each schedule over

A schedule in PMM Extensions writes to the same directory as the schedule it replaces. Do not leave both enabled. A mydumper backup from one instance replaces the backup that the other took on the same day.

For each schedule:

1. On the old instance, open the **MySQL Backups** page. In the **Periodic Tasks** table, turn off the switch in the **Enabled** column, and confirm.
2. In PMM, go to **Apps** > **MySQL Backups** > **Schedules** and select the **Enabled** checkbox of the same schedule.
3. After the next run, check that **Last Run** shows the run and that the new backup is on the database host.

An interval schedule first runs one interval after you enable it. A schedule that runs every day first runs a day later.

### While both instances run

PMM Extensions and the old instance both run a backup on a host as a Nomad job named `run-python-<host>`. When both instances use the Nomad server of the same PMM, they share that job, and two things can happen:

- A backup that the old instance runs takes longer. In the test, a backup of 30 seconds took about 110 seconds.
- PMM Extensions answers a run with HTTP 502 for one host: "Failed to get a response from Nomad". This happens when the old instance was the last to register its job on that host. Remove the job, and PMM Extensions registers its own on the next run:

    ```sh
    NOMAD_HOST=<host name as PMM shows it>
    curl --cacert pmm-ca.crt -u admin -X DELETE "$PMM_URL/nomad/v1/job/run-python-$NOMAD_HOST?purge=true"
    ```

  `curl` asks for the password of the PMM `admin` user.

Neither applies when the old instance uses another PMM server.

## Restore a backup taken before the migration

PMM Extensions has no record of the backups that the old instance took, so the Restore form does not list them. You type the location.

The script printed the directory of each task. The newest backup is in `latest` under that directory. An XtraBackup directory has one subdirectory for each backup, named by date and time. A mydumper directory has one for each day.

1. In PMM, go to **Apps** > **MySQL Backups** > **Restores** and select **New restore**.
2. Enter a **Task Name**, and select the **Execution Host** and the **Destination Database Service**.
3. Select the **Backup Type** that took the backup.
4. In **Backup Source**, type the full path of the backup, for example `/var/backups/svcdocs/xtrabackup/172.31.213.11/2026-11-18_22-22-00`, and select the entry that appears. The field shows "No options available" until you type.
5. For an XtraBackup restore, open the **XtraBackup** section and enter the **Data directory**.
6. Select **Create restore**. Open the task and select **Execute**.

An XtraBackup restore changes the execution host. Read this before you run one:

- It stops MySQL on the execution host, deletes everything in the data directory, restores the backup into it, and starts MySQL again. Run it on a host where that is acceptable.
- It stops and starts MySQL with `systemctl` or `service`. On a host with neither, set **Custom MySQL init command** in the **General** section to a command that accepts `stop` and `start`.
- It checks MySQL on port 3306. For another port, set the port in the **General** section. With the wrong port, the restore waits ten minutes for MySQL to stop.

When the task finishes, check the result yourself:

1. Open the run under **Execution History** and read its output. PMM Extensions 3.10.0 can show a restore as successful when the output says "Restore failed for this host".
2. Query the restored server and confirm that the data is there.

The same form restores a backup that PMM Extensions took but does not list. This happens on a database host whose disk is more than 80% full. The backup is on disk, and PMM Extensions records no location for it.

## Next steps

Keep the old instance until every schedule has run in PMM Extensions and one restore has passed. Then stop it on its host:

```sh
docker compose --file "$OLD_DIR/compose.yaml" --project-name sep stop
```

Keep `OLD_DIR` and the backups on the database hosts for as long as you may need to restore from them.

Delete `pmm.token` and `standalone-export.json` when the migration is done, and remove the service account in PMM.

## Troubleshooting

| The script prints | Do this |
|---|---|
| `certificate verify failed: self-signed certificate` | Pass `--pmm-ca-file pmm-ca.crt`, and use `https://127.0.0.1` or `https://localhost` in `PMM_URL`. |
| `the token in pmm.token is not an admin's` | Use the token of a service account with the **Admin** role. |
| `PMM Extensions answered HTTP 401` | The token is wrong or has expired. Save a new one in `pmm.token`. |
| `PyYAML is not installed for this Python` | Run the script in the PMM Extensions image, as this page does, or install PyYAML. |
| `the export file is truncated` | Run the export again, and copy the whole file. |

If you cannot reach PMM at an address that its certificate names, replace `--pmm-ca-file pmm-ca.crt` with `--insecure-skip-tls-verify`. The script then does not check which server it talks to, and it sends the token to whatever answers at `PMM_URL`. Use this option only on a network that you trust.
