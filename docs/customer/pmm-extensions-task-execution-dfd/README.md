# PMM Extensions Task Execution — Data Flow Diagram

Customer-facing documentation of how data moves through **PMM Extensions** during a typical predefined task execution, including the parallel audit and logging path.

## Contents

| File | Purpose |
|------|---------|
| [`pmm-extensions-task-execution-dfd.mmd`](pmm-extensions-task-execution-dfd.mmd) | Level-1 DFD (happy path + audit/logging) |
| [`pmm-extensions-task-execution-sequence.mmd`](pmm-extensions-task-execution-sequence.mmd) | Sequence diagram (time-ordered detail) |
| [`data-capture-redaction-retention.md`](data-capture-redaction-retention.md) | What PMM Extensions captures, redacts, stores, exposes, and retains for task output |
| [`security-review-checklist.md`](security-review-checklist.md) | Security sign-off before customer delivery (DFD §1–7, Nomad §8) |
| [`../nomad-driver-deployment.md`](../nomad-driver-deployment.md) | Nomad `raw_exec` driver, deployment topology, non-root agent, API access |
| [`exports/pmm-extensions-task-execution-dfd.pdf`](exports/pmm-extensions-task-execution-dfd.pdf) | Exported PDF — main DFD (regenerate after `.mmd` changes) |
| [`exports/pmm-extensions-task-execution-sequence.pdf`](exports/pmm-extensions-task-execution-sequence.pdf) | Exported PDF — sequence diagram |

**Last diagram export:** regenerate both PDFs after editing `.mmd` files (see [Export PDF](#export-pdf)).

## Legend

| Symbol | Meaning |
|--------|---------|
| Rounded rectangle `([...])` | External entity (engineer, Grafana/PMM, Nomad, log stack) |
| Rectangle `[...]` | Process (PMM Extensions/Tasks/Celery components) |
| Cylinder `[(...)]` | Data store (PostgreSQL, snippet files, Nomad runtime) |
| Solid arrow | Primary data flow |
| Dotted arrow | Secondary / read / alternate path |

### Authentication (engineers)

In the shipped PMM-embedded deployment, identity comes from **PMM's Grafana**. The browser's existing PMM session cookie rides a same-origin request to `POST /api/oauth/session/exchange`; PMM Extensions validates that session against Grafana, maps the org role, and returns a **short-lived PMM Extensions-signed bearer token** in the response body. No PMM Extensions cookie is set and no refresh token is issued — when the bearer expires the SPA repeats the exchange. Subsequent API calls carry the PMM Extensions bearer as `Authorization: Bearer <token>`.

Casdoor OAuth remains a configurable alternative (`AuthProviderEnum.CASDOOR`) but is not selected by the embedded profile.

**X.509 certificates are not used for engineer identity.** Certificates secure **transport** (HTTPS and inter-service mTLS).

### Predefined commands only

The UI does not submit arbitrary shell commands. PMM Extensions uses two main execution patterns:

#### Path A — One-shot execute (snippets, dipper)

| Step | What the engineer does | What the server enforces |
|------|------------------------|-------------------------|
| Run | Pick an **approved** snippet (or dipper collector) and submit a **schema-defined** form | No saved task definition; `meta` is built on each run |
| Command | Never typed by the user | Fixed Nomad job type (`exec-artifact`, etc.) plus server-built `meta` (target, interpreter, signed artifact URL) |

**Snippets:** `POST /api/apps/snippets/snippet/execute?snippet_filename=...` → PMM Extensions → Tasks API `POST /execute/{execution_task_name}` where `execution_task_name` resolves to `exec-artifact` for bash snippets and `exec-python-artifact` for Python snippets with requirements.
**Dipper:** `POST /api/apps/dipper/` → same Tasks execute pattern with dipper script metadata.

#### Path B — Create then execute (proxy apps: checksums, alters)

| Step | What the engineer does | What the server enforces |
|------|------------------------|-------------------------|
| **Create** | Fill an app form (e.g. MySQL service, databases, checksum options) | App resolves inventory entities and builds a **PROXY** task: fixed `meta.command` (e.g. `pt-table-checksum`), `meta.args`, `meta.target` stored in Tasks DB |
| **Execute** | Click **Execute** on a saved task (optional ETA / task chain) | Request body does **not** include a new shell command; Tasks API **merges** stored `meta` from the task definition (`prepare_task_history` for `TaskBackendEnum.PROXY`) |
| **Nomad** | — | Dispatches underlying `run-command` job with merged meta |

**Checksums example (canonical Path B):**

1. **Create:** `POST /api/apps/checksums/` — `build_checksums_spec` assembles `pt-table-checksum` with args from inventory (`app/extensions/apps/checksums/spec.py`), with the legacy Jinja form path using (`app/extensions/apps/checksums/deps.py`, `_assemble_checksum_payload`).
2. **Persist:** `POST /api/tasks/` — task row with `owner=CHECKSUMS`, `backend=PROXY`, `data.task=run-command`.
3. **Execute:** `POST /api/apps/checksums/{task_name}/execute` — body may only contain `eta`, `chain_task_names`, `chain_on_failure`; command comes from stored task (`app/extensions/apps/framework/api.py`, `derive_execute_route`, which posts to `POST /api/tasks/execute/{task_name}`).

The same two-phase pattern applies to **alters** (`command: pt-online-schema-change`).

The Nomad `run-command` template runs `${NOMAD_META_command}` with args from meta; the UI never supplies a free-form command string at execute time.

### What is logged

| Data | Storage | When |
|------|---------|------|
| User identity | `taskhistory.executed_by` (authenticated user id; `"SYSTEM"` for periodic runs) | Task dispatch |
| Timestamps | `created_at`, `started_at`, `finished_at` | Create + executor sync |
| Command context | `execution_request` JSON (`task`, `target`, `meta`, `payload`) | Create |
| Output | `taskhistory_log` (stdout/stderr chunks) | During/after run |
| Lifecycle | Nomad `task_states` → execution events API | Sync |
| External timeline | PMM annotations | Start / terminal status |
| HTTP context | `request_id`, `correlation_id`, `user` in app logs | Each request (infra stack, not Tasks DB) |

Optional **Presidio anonymization** may mask log content when `anonymize_mask` is enabled for a task owner.

### Where logs live and who can access them

| Store | Access |
|-------|--------|
| **Tasks PostgreSQL** (`taskhistory`, `taskhistory_log`) | Any **authenticated** OAuth user can read task history and logs by ID. A separate `EXTENSIONS_INTERNAL_TOKEN` service principal (synthetic non-admin user `extensions-service`, id `00000000-0000-4000-8000-000000000000`) can authenticate for service-to-service calls and has the same read scope as a regular authenticated user. General list/retrieve APIs do **not** filter by `executed_by`. |
| **PMM** | Users with PMM annotation access for the environment. |
| **Infrastructure logs** | Platform operators with access to the deployment log pipeline. |
| **Snippet approval** | **Admin** users only. Single approve: `PUT /api/apps/snippets/snippet/approval?snippet_filename=...`. Bulk approve: `PATCH /api/apps/snippets/approvals`. Both gated via the `ApiAdminUser` dependency. |

No per-row owner filter is applied to general task history / log reads. Future versions may add filtering for specific task / meta combinations.

## Edit the diagrams

1. Install [Mermaid](https://mermaid.js.org/) support in your editor, or use [mermaid.live](https://mermaid.live).
2. Edit `.mmd` files in this directory.
3. Re-export PDF (see below) before customer delivery.

Alternative: import `.mmd` into [diagrams.net](https://app.diagrams.net/) for GUI editing, then export PDF manually.

**Sequence diagram editing:** `mermaid-cli` fails on semicolons, curly braces, and some punctuation inside `Note over` lines and message text. Keep labels simple or split Path B into separate `rect` blocks (as in `pmm-extensions-task-execution-sequence.mmd`).

## Export PDF

`mermaid-cli` drives a headless Chrome to render the diagram, so it needs a browser
on the machine. Point it at a system Chromium with
`PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium`, or install one for Puppeteer once with
`npx puppeteer browsers install chrome-headless-shell`. Without either, the commands
below fail with a misleading *"Could not find Chrome"*.

From the repository root:

```bash
npx -y @mermaid-js/mermaid-cli \
  -i docs/customer/pmm-extensions-task-execution-dfd/pmm-extensions-task-execution-dfd.mmd \
  -o docs/customer/pmm-extensions-task-execution-dfd/exports/pmm-extensions-task-execution-dfd.pdf \
  -b white

# Optional: sequence diagram PDF
npx -y @mermaid-js/mermaid-cli \
  -i docs/customer/pmm-extensions-task-execution-dfd/pmm-extensions-task-execution-sequence.mmd \
  -o docs/customer/pmm-extensions-task-execution-dfd/exports/pmm-extensions-task-execution-sequence.pdf \
  -b white
```

Commit the updated `.mmd` sources with any README/checklist changes. The PDF under `exports/` may be committed or produced at release time; if committed, regenerate whenever sources change.

## Code mapping (DFD nodes → implementation)

| DFD node | Source (representative) |
|----------|-------------------------|
| P1 Session exchange | `app/api/routes/oauth.py` (`spa_session_exchange`) |
| P2 PMM Extensions bearer validation | `app/extensions/deps.py` (`get_current_user`), `app/core/auth/providers/grafana/models.py` (`GrafanaUser.from_bearer`) |
| P3 PMM Extensions UI | `frontend/packages/shell/` (React 18 SPA — entry `src/main.tsx`, auth context `src/contexts/auth.tsx`). App UIs live under `frontend/packages/apps/{name}/` and `frontend/packages/framework/` (shared schema-driven UI). |
| P4a Snippets app API | `app/extensions/apps/snippets/app.py` (declarative `TaskExecutionApp`), `script_source.py` (`snippet_source` — derives list/schema/history/execute); auxiliary verbs (approval, refresh, preview) in `extra_routes.py` |
| P4b Proxy app create (checksums) | `app/extensions/apps/checksums/app.py` (declarative `TaskExecutionApp`), `spec.py` (`build_checksums_spec`), `models.py` (`ChecksumsForm`); create route derived by `app/extensions/apps/framework/api.py` (`derive_crud_routes`) |
| P4c Proxy app execute (checksums) | `app/extensions/apps/framework/api.py` (`derive_execute_route`), enabled via `app/extensions/apps/checksums/app.py` (`AppCapabilities(execute=True)`) |
| PROXY meta merge | `app/tasks/deps.py` (`prepare_task_history`, lines ~191–193) |
| P5 Tasks API | `app/tasks/routes.py` (`execute_task_name`), `app/tasks/deps.py` |
| D4 Task definitions | `task` table via `POST /api/tasks/`; seed template `run-command` in `app/tasks/db/seed.py` |
| Inventory (Path B) | `app/extensions/apps/checksums/deps.py` (`get_created_entity` for services/schemas/tables) |
| P6 Celery worker | `app/tasks/celery.py` (`dispatch_queue_item`) |
| P7 Nomad executor | `app/tasks/execution/executors/nomad/models.py` |
| P8 Record attribution | `app/tasks/deps.py` (`prepare_task_history`) |
| P9 Persist logs | `app/tasks/logs/log_writer.py`, `app/tasks/crud.py` |
| P10 PMM annotations | `app/core/pmm.py` |
| P11 HTTP request context | `app/core/middleware/log_context.py`, `app/api/deps.py` |
| D1 Snippet store | `app/extensions/snippets/`, snippet approval in DB |
| D2 Tasks PostgreSQL | `app/tasks/models/`, `app/tasks/db/` |
| D3 Nomad runtime | Nomad allocation logs/files (fetched via Nomad API) |
| mTLS / TLS transport | PMM's Nginx fronts PMM Extensions (HTTPS termination is PMM-owned). PMM Extensions-internal mTLS: `app/core/requests/remote_api.py` (SSL context builder); inter-service certs at `/data/certs/sep/*`. Nomad mTLS: client cert and CA at `/data/certs/nomad/`. For external-Nomad deployments these are customer-supplied. |

## Security review

Complete [`security-review-checklist.md`](security-review-checklist.md) and obtain sign-off **before** sending the PDF to a customer.

## Out of scope

- Certificate-based **user** login (not implemented in default PMM Extensions)
- Full Nomad/PMM deployment topology (see [Nomad driver & deployment](../nomad-driver-deployment.md))
- Celery-only (non-Nomad) executors except where noted
- Inventory sync flows unrelated to task execution
