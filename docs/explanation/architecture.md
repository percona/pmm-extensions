# How the sidecar runs inside PMM

PMM Extensions runs as one container beside PMM Server. This page explains how the two fit together and why each piece is the way it is. For the settings themselves, see the [sidecar reference](../../sidecar/README.md). To start the pair on your machine, see [Run PMM Extensions in a local PMM](../how-to/run-in-local-pmm.md).

## One container beside PMM Server

PMM Extensions has no deployment of its own. It ships as one image, `percona/pmm-extensions`, and it runs as a sidecar: a container that is started next to `pmm-server` and that depends on it. PMM gives it a database, its secrets, sign-in and the engine that runs tasks on hosts. Version 0.13.1 was the last one that ran without PMM.

The image has no frontend. The pages that a user sees are part of PMM's UI.

## What runs in the container

One `supervisord` process starts everything, so the container has one process tree and one log stream. It runs:

- three API services: the `extensions` service on port 9000, the `inventory` service on 9001 and the `tasks` service on 9002
- a Celery worker and Celery beat, for background and scheduled work
- a Valkey server, which is the Celery broker
- four programs that apply the database schema and then exit

Each API waits until all four schema programs have finished. The container is healthy when five things hold. The sidecar has its Grafana token, every long-running program runs, the four schema steps have completed, the three APIs answer and the broker answers.

The three services share one database, `pmm_extensions`, in PMM's own PostgreSQL. Their tables do not collide, and each service keeps its own migration history.

## How PMM reaches it

A browser never talks to the sidecar directly. PMM's nginx has a `location /extensions/` block that forwards those requests to the sidecar's `extensions` service. That is why every PMM Extensions URL starts with `/extensions`, and why the image sets `EXTENSIONS.ROOT_PATH` to that prefix.

PMM finds the sidecar by name. `PMM_EXTENSIONS_ADDRESS` holds a host and a port, and it defaults to `pmm-extensions:9000`. So the Compose service is named `pmm-extensions`. nginx resolves the name for each request, which means either container can be recreated without the other.

PMM's nginx ends TLS. Inside the container the services speak plain HTTP.

## The four secret files

PMM creates the secrets that the sidecar needs. When `PMM_ENABLE_EXTENSIONS` is set, PMM Server writes four files into a volume that both containers mount:

- `SECRET_KEY`, which signs sessions and derives the token the services use among themselves
- one database password for each of the three services

PMM also creates the database and its role, and it opens its PostgreSQL to the container network. Nothing is published on the host.

The sidecar mounts that volume read-only and reads each file as the setting that the file is named after. `SECRETS_DIR` names the directory. The values never enter the container's environment, so `docker inspect` does not show them.

PMM writes the files for group 0, in a directory that passes its group on to new files. The sidecar runs as its own user, so it joins group 0 to read them. That is the `group_add` entry in the Compose file. A matching user ID would break if PMM changed the user it runs as.

The sidecar reads its settings once, when each process starts. PMM writes the files while it starts, so a sidecar that starts at the same time finds no key yet. It waits for the key file before it starts any process: see [Why the sidecar waits for PMM](#why-the-sidecar-waits-for-pmm).

## Sign-in

PMM Extensions has no users of its own. Its only identity provider is PMM's Grafana.

A user signs in to PMM. The UI then calls `POST /extensions/api/oauth/session/exchange` with the PMM session cookie. PMM Extensions asks Grafana who the session belongs to, and it returns a short-lived token. The UI keeps the token in memory and exchanges again before it expires. No long-lived credential is shared, and a change of role takes effect within one token lifetime.

## The credentials the sidecar creates itself

Three credentials do not come from PMM.

**The Grafana token.** To ask Grafana about sessions, to call PMM's API and to reach Nomad, the sidecar needs a token of its own. At startup it signs in to Grafana as the administrator. It finds or creates a service account named `pmm-extensions` and asks for a token. It stores the token in its state volume and reuses it at the next start. The administrator password is needed only when the sidecar holds no valid token.

While Grafana is still starting, the sidecar waits for it. The next section says why.

**The broker password.** The Valkey server is inside the container and serves only Celery. The entrypoint generates a new password at each start. Nothing outside the container needs to know it, and nothing in the broker survives a restart.

**The encryption key.** Secret values that an administrator stores through the settings API are encrypted with `ENCRYPTION_KEY`. The sidecar generates the key on a first start and stores it in the state volume. It generates one only when no database holds encrypted values. A new key over old data would make that data unreadable without any error, so in that case the container refuses to start.

The state volume therefore matters. A lost token costs one new token. A lost encryption key cannot be recovered.

## Why the sidecar waits for PMM

A Compose file can start the sidecar as soon as the `pmm-server` container has started, before PMM is healthy. Two things that the sidecar needs do not exist yet: the `SECRET_KEY` file, and a Grafana that can create the token. The sidecar waits for both, and neither wait has a time limit.

PMM owns the key and Grafana, so a time limit could only give up on a slow PMM. The programs read the token once, from the environment that `supervisord` starts them with. A token that is created later cannot reach them without a restart. The four schema programs wait for PMM's PostgreSQL in the same way.

PMM writes the key first and the three database passwords directly after it. So the sidecar reads the directory once more, one second after the key appears.

Some faults do not clear with time. Examples are a rejected administrator password, an address that does not serve Grafana's API, and a Grafana certificate that fails verification. The sidecar reports such a fault once and does not try again, because repeated failed sign-ins can lock the administrator account in Grafana. It then starts its programs without a token.

The health check requires the token. A sidecar without one still answers `/health`, but sign-in and the inventory copy do not work. So the health check fails while the sidecar waits, and after a fault that waiting cannot clear.

The image with the tag `3.10.0` does not wait. Without the key file, it exits. When Grafana does not answer within 60 seconds, it starts without a token and still reports healthy. That image needs a Compose file that starts it only after PMM is healthy.

PMM Server's own health check allows it 90 seconds to start. A cold start can take longer. A Compose file that makes the sidecar wait for PMM should also give PMM a longer start period, or the first `up` fails.

[Starting before PMM is ready](../../sidecar/README.md#starting-before-pmm-is-ready) lists what each wait covers and what the container log says.

## What the sidecar expects from Nomad

PMM Extensions does not run tasks itself. It hands each task to Nomad, and the Nomad that it uses is PMM's own. PMM Server runs the Nomad server when `PMM_ENABLE_NOMAD` is set, and each PMM Client runs a Nomad client.

The sidecar reaches Nomad through PMM's nginx, at `https://pmm-server:8443/nomad`, and it sends its Grafana token as a bearer token. It needs no Nomad certificate and no direct port.

A host can run a task when it has all of these:

- PMM Client 3.2 or later, registered with this PMM, which brings the Nomad client
- a route to PMM Server on port 4647, at the address in `PMM_PUBLIC_ADDRESS`
- the `raw_exec` driver reporting healthy, which is the only driver that PMM Extensions uses
- `python3` 3.9 or later with its `venv` module, for tasks that run a Python payload
- the tool that the task calls

`PMM_PUBLIC_ADDRESS` is the address that PMM gives to every Nomad client. Without it, Nomad does not start, and PMM logs a warning. With a value that clients cannot reach, such as `localhost` on a machine with several containers, each client tries to reach itself and never joins.

A task runs on the host that the user chose, and nowhere else. Each job is pinned to the node whose name equals the task's target. `raw_exec` runs the task as the user that the Nomad client runs as. On a PMM Client host, that is the user of PMM Client. An app that needs another user, as PostgreSQL Backups does, switches user inside its own payload.

## Why `BASE_URL` includes `/extensions`

Some tasks download a script from the sidecar before they run it. `BASE_URL` is the sidecar's address as a task host sees it. The download address is built by adding a path to `BASE_URL`, so the value must include the `/extensions` prefix when it goes through PMM. A value without it still looks like a valid URL and reaches PMM, not PMM Extensions.

## Why the image holds only some apps

The repository contains more apps than PMM ships. The image build removes every app package that `sidecar/settings.yaml` does not list. So that file is the single statement of what ships, and an app that it lists cannot be missing from the image.

At runtime the list can only be narrowed. A setting that names an app whose package is not in the image stops the container at startup.

[The app status page](../app-status.md) lists the apps on both sides of that line.

## Where the UI lives

The UI is in the `percona/pmm` repository, under `ui/`. The PMM application in `ui/apps/pmm` holds the route and the navigation entry of each app. An app whose pages are drawn from its form schema uses one shared component. An app with pages of its own has a package under `ui/packages/plugins/`.

So an app becomes visible to users in two steps: this repository adds it to the image, and `percona/pmm` adds it to the UI. Both reach users with a PMM release. [Add an app to the PMM UI](../how-to/add-an-app-to-the-pmm-ui.md) describes the second step.
