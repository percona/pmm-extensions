# Rerun a schema step in a running sidecar

The sidecar applies its database schema in four steps when the container starts: `extensions`, `inventory`, `tasks` and `beat`. This page shows how to run one step again without restarting the container.

Restarting the container is the safer way: it reruns all four steps. Use this page when the container must stay up.

## Prerequisites

- A running sidecar container, and permission to run `docker exec` in it.
- The name of the step to rerun. The examples use `extensions`, and a container named `pmm-extensions`.

## Stop Celery

The Celery programs do not check the schema. Stop them first, so that no task runs against tables that are changing. Tasks that are queued stay in the broker.

1. Stop the worker and the scheduler:

   ```bash
   docker exec pmm-extensions supervisorctl -c /home/extensions/app/supervisord.conf \
       stop celery-worker celery-beat
   ```

   The output is:

   ```text
   celery-worker: stopped
   celery-beat: stopped
   ```

## Clear the marker and rerun the step

Each step writes a marker file when it finishes, and each API waits for all four markers before it starts. Clear the marker first. Otherwise a restarted API can read the marker of the previous run.

1. Clear the step's marker:

   ```bash
   docker exec pmm-extensions /home/extensions/app/clear_sentinels.sh extensions
   ```

   The script takes step names, not program names: `extensions`, not `migrate-extensions`. Continue only if it exits with status 0.

2. Restart the step and the three APIs in one call:

   ```bash
   docker exec pmm-extensions supervisorctl -c /home/extensions/app/supervisord.conf \
       restart migrate-extensions extensions inventory tasks
   ```

   The output starts with an error that is not the result:

   ```text
   migrate-extensions: ERROR (not running)
   extensions: stopped
   inventory: stopped
   tasks: stopped
   migrate-extensions: started
   extensions: started
   inventory: started
   tasks: started
   ```

   The first line appears because the step had already exited. `started` is not the result either: a step that fails is reported the same way. The marker is the result.

   Name the APIs in the same call. An API that is not restarted keeps serving against the schema it started with.

3. Wait for the marker:

   ```bash
   docker exec pmm-extensions test -f /tmp/migrate-extensions.ok
   ```

   Repeat the command until it exits with status 0. A step with nothing to apply finishes within a second.

The container's health check fails while Celery is stopped or a marker is missing. After five failed checks, the container reports unhealthy: see [Health](../../sidecar/README.md#health).

To rerun several steps, name them all in each call: `clear_sentinels.sh extensions tasks`, then `restart migrate-extensions migrate-tasks extensions inventory tasks`.

## If the clear fails

- An exit with status 2 means an unknown step name, and nothing was removed. Correct the name and run the clear again.
- An exit with any other status means that a marker could not be removed. The script stopped at that step, and the steps named before it are already cleared. Fix what blocked the removal, then clear and restart every step that was cleared. Or restart the container, which reruns all four.

## Start Celery

1. Start the worker and the scheduler again:

   ```bash
   docker exec pmm-extensions supervisorctl -c /home/extensions/app/supervisord.conf \
       start celery-worker celery-beat
   ```

   The output is:

   ```text
   celery-worker: started
   celery-beat: started
   ```

## Verify

1. Check that every program runs:

   ```bash
   docker exec pmm-extensions supervisorctl -c /home/extensions/app/supervisord.conf status
   ```

   The four `migrate-` programs show `EXITED`. Every other program shows `RUNNING`. The command exits with status 3, because some programs have exited. That is expected here.

2. Confirm that the container reports healthy:

   ```bash
   docker inspect pmm-extensions --format '{{.State.Health.Status}}'
   ```

   The output is `healthy`. If the container had turned unhealthy, the next passing check turns it back, within 15 seconds.

## Next steps

- [How the sidecar runs inside PMM](../explanation/architecture.md)
- [The sidecar reference](../../sidecar/README.md)
