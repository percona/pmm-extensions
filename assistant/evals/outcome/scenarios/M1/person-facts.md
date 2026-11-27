
## What Dana knows for this request

Dana reported that every backup of the PostgreSQL Backups app fails on her hosts, and gave the end of one log.

- What the user did: created a backup with the form's own values, for the stanza `dev`, and ran it. It ended with the status "failed".
- The hosts: PMM Client hosts with PostgreSQL 17 and pgBackRest. `/etc/pgbackrest.conf` holds the stanza `dev`. PMM runs its work there as root, and the backup itself runs as the `postgres` user.
- Running `pgbackrest --stanza=dev backup` by hand as `postgres` on a host works.
- Nothing may be changed on the hosts to make the app work: no new directory, no changed permission. The app has to work on a host as it is.
- What a good result looks like: the status "success", and a new backup in `pgbackrest --stanza=dev info`.
- A ticket number: she has none.
