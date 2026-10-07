# PMM Extensions — Nomad Driver and Deployment Configuration

Customer-facing reference for security and cloud architects reviewing how **HashiCorp Nomad** is deployed and used by **PMM Extensions**. This document describes the **Percona GAS automation** deployment (`nomad.yaml` / `sep.yaml` in the GAS automation repository) and how PMM Extensions consumes Nomad at runtime.

**Related:** [PMM Extensions Task Execution DFD](pmm-extensions-task-execution-dfd/README.md) (application data flows and command controls).
**Security sign-off:** [security-review-checklist.md](pmm-extensions-task-execution-dfd/security-review-checklist.md) §8 before customer delivery.

---

## 1. Official HashiCorp documentation

| Topic | Link |
|-------|------|
| Nomad overview | [What is Nomad?](https://developer.hashicorp.com/nomad/intro) |
| **`exec` driver** (isolated, cgroups/chroot) | [Exec task driver](https://developer.hashicorp.com/nomad/plugins/drivers/exec) |
| **`raw_exec` driver** (direct host process) | [Raw exec task driver](https://developer.hashicorp.com/nomad/plugins/drivers/raw_exec) |
| Agent configuration | [Nomad agent configuration](https://developer.hashicorp.com/nomad/docs/configuration) |
| TLS on agents | [Nomad agent TLS](https://developer.hashicorp.com/nomad/docs/operations/security/tls) |
| Nomad ACL (not used in default Percona deployment; see §6) | [Nomad ACL](https://developer.hashicorp.com/nomad/docs/secure/acl) |

---

## 2. Which driver PMM Extensions uses

| Driver | Enabled on Nomad agents (Percona default) | Used by PMM Extensions job templates |
|--------|-------------------------------------------|---------------------------|
| **`raw_exec`** | **Yes** — explicitly enabled in agent config | **Yes** — all parameterized batch jobs |
| **`exec`** | **No** — not enabled in automation templates | **No** |
| **`nomad-driver-podman`** | Optional (`nomad_plugin_container_enabled`, default **false**) | **No** for PMM Extensions task execution |

PMM Extensions registers parameterized Nomad jobs (`run-command`, `run-python`, `exec-artifact`, etc.) whose tasks declare `"Driver": "raw_exec"`. The Tasks service only schedules work onto nodes that advertise a healthy `raw_exec` driver. From `app/tasks/execution/executors/nomad/models.py`:

```python
filter_expression = "Status == ready and raw_exec in Drivers and Drivers.raw_exec.Healthy == true"
return {
    node["Name"]: node["Address"]
    for node in self.backend.nodes.get_nodes(filter_=filter_expression)
}
```

**Why `raw_exec` and not `exec`:** PMM Extensions runs predefined tooling (Percona Toolkit, approved snippets, Python payloads) on **database hosts** already managed by Percona automation. `raw_exec` runs the process as the **same unprivileged OS user as the Nomad client agent** (see §5), with job placement constrained to a named node (see §4). The `exec` driver adds an isolation boundary (chroot, cgroups) that is not required for the current PMM Extensions job design and is **not** turned on in the standard playbook.

---

## 3. Percona deployment (GAS automation)

### 3.1 Playbooks and roles

| Item | Location / behavior |
|------|---------------------|
| Nomad playbook | `automation/nomad.yaml` — dynamic inventory groups, server/client install, TLS |
| PMM Extensions stack playbook | `automation/sep.yaml` — imports `nomad.yaml` (tag `nomad`), then deploys PMM Extensions on `monitors` |
| Nomad server role | `automation/roles/nomad` — binary, `config.hcl`, systemd unit, TLS, optional PMM scrape |
| Nomad client role | `automation/roles/nomad_client` — includes the `nomad` role on client hosts |
| PMM Extensions Nomad API settings | `automation/roles/sep/templates/prod-settings.yaml.j2` → `TASKS.NOMAD` |

Deployment is driven by inventory variables (typically set in ServiceNow / `gascan` CI):

| Variable | Default | Meaning |
|----------|---------|---------|
| `nomad_server_enabled` | `false` | Host joins dynamic group `nomad_servers` and runs a Nomad **server** agent |
| `nomad_enabled` | `false` | Host joins `nomad_clients` and runs a Nomad **client** agent (also required on servers) |

Playbook flow (`nomad.yaml`):

1. Preflight (`assertions.yaml`).
2. On all hosts except `vips`, assign each host to `nomad_servers` or `nomad_clients` from the flags above.
3. Build `nomad_client_servers` — list of server addresses clients use to join the cluster.
4. Apply role `nomad` on `nomad_servers` and `nomad_client` on `nomad_clients` when `nomad_enabled` is true.

Reference variable documentation: GAS `docs/reference/nomad.md` and `docs/usage/nomad.md`.

### 3.2 Agent configuration (drivers and TLS)

Rendered from `GAS/automation/roles/nomad/templates/config.hcl.j2`:

```hcl
plugin "raw_exec" {
  config {
    enabled = true
  }
}
```

TLS is **required** for HTTP and RPC; server hostname verification and **HTTPS client certificate verification** are enabled:

```hcl
tls {
  http = true
  rpc  = true
  # ca_file, cert_file, key_file per server vs client role
  verify_server_hostname = true
  verify_https_client    = true
}
```

Other notable settings:

- **Nomad version:** `1.10.5` (role default `nomad_version`).
- **Bind/advertise:** `nomad_bind_addr` (defaults to `ansible_host`).
- **Node name:** `nomad_node_name` → `pmm_payload_base.node_name` or `inventory_hostname` (this is the value PMM Extensions uses as execution **target**).
- **Data/config paths:** under the automation OS user, e.g. `~/.config/nomad`, `~/.local/share/nomad/data`.

Certificates are generated with `nomad tls ca create` and `nomad tls cert create` (`roles/nomad/tasks/certs.yaml`):

| Certificate | Purpose |
|-------------|---------|
| `nomad-agent-ca.pem` | Cluster CA |
| `global-server-nomad.pem` | Server/agent identity on Nomad server nodes |
| `global-client-nomad.pem` | Client identity — used by **PMM Extensions Tasks** API to call Nomad |
| `global-cli-nomad.pem` | Operator CLI / troubleshooting |

### 3.3 PMM Extensions connection to Nomad

Production Tasks settings (from automation template) use **HTTPS + mTLS** to the Nomad HTTP API on the monitor host:

```yaml
# GAS: roles/sep/templates/prod-settings.yaml.j2 (TASKS.NOMAD)
NOMAD:
  ENDPOINT: https://<sep_nomad_ip>:4646
  SECURE: true
  SSL_CAFILE: /data/certs/nomad/nomad-agent-ca.pem
  SSL_CERTFILE: /data/certs/nomad/global-client-nomad.pem
  SSL_KEYFILE: /data/certs/nomad/global-client-nomad-key.pem
```

Nomad certificates are mounted into the PMM Extensions pod/stack (`roles/sep/templates/sep-local.yaml.j2`). Local development may use plain HTTP (`settings.yaml`).

---

## 4. Where Nomad runs and where tasks execute

### 4.1 Typical topology

| Role | Inventory flag | Typical host | Executes PMM Extensions workloads? |
|------|----------------|--------------|------------------------|
| **Nomad server** | `nomad_server_enabled: true` | **Monitor** / PMM server host | Server-only scheduling metadata; may also run client on same host |
| **Nomad client** | `nomad_enabled: true` | **Database nodes** (and optionally monitor) | **Yes** — `raw_exec` allocations run here |

Hosts in the `vips` group are **excluded** from dynamic Nomad grouping (`nomad.yaml` uses `hosts: '!vips'`).

Exact host lists are **customer-specific** (Ansible inventory / ServiceNow). Percona enables Nomad per host via `nomad_enabled` / `nomad_server_enabled`; there is no hard-coded host list in PMM Extensions.

### 4.2 How PMM Extensions picks a node

1. Engineers choose a **target** (Nomad node name) in the UI/API, from `GET /api/tasks/hosts/` (healthy clients with `raw_exec`).
2. Dispatched jobs include meta `target` matching `${node.unique.name}`.
3. Job templates constrain placement. From `app/tasks/db/seed.py`:

```python
"Constraints": [
    {
        "LTarget": "${node.unique.name}",
        "RTarget": "${NOMAD_META_target}",
        "Operand": "=",
    },
],
```

So a task runs **only** on the client whose registered name equals the selected target (usually the DB host’s inventory / PMM node name).

### 4.3 Network ports (reference)

| Source | Destination | Port | Use |
|--------|-------------|------|-----|
| Nomad client/server | Nomad server/client | **4646/TCP** | HTTP API (PMM Extensions → Nomad) |
| Nomad server | Nomad client | **4647/TCP** | RPC |

See GAS `docs/usage/nomad.md` for production networking guidance.

### 4.4 Executor-host Python

PMM Extensions' Python payloads (backups, restores, pre-checks, connectivity checks, inventory and host-facts collection, and diagnostics collectors) run under the executor host's own interpreter. The `run-python` and `exec-python-artifact` jobs build a virtual environment with `python3 -m venv` and install the payload's drivers into it with `pip`. So every Nomad client that runs PMM Extensions workloads needs:

- **`python3` 3.9 or newer** on the Nomad agent's `PATH`;
- **its `venv` module**, including the `pip` bootstrap `venv` relies on.

| Platform | Default `python3` | Action |
|----------|-------------------|--------|
| Red Hat / Oracle Linux 8 | 3.6 | Install a newer Python, for example the `python39` package, and make `python3` resolve to it |
| Red Hat / Oracle Linux 9, Amazon Linux 2023 | 3.9 | None |
| Ubuntu 22.04 | 3.10 | Install `python3-venv` |
| Debian 12 | 3.11 | Install `python3-venv` |
| Red Hat 10 | 3.12 | None |

On Python 3.9, `pip` installs the newest driver releases that still support it, which can be older than on a newer host.

**Customer action:** On each Nomad client, run this as the agent's user. It builds a virtual environment the way PMM Extensions does and prints `pip`'s version line, which must end in `(python 3.9)` or newer; an error instead means the host lacks `venv` or its `pip` bootstrap:

```bash
d=$(mktemp -d) && python3 -m venv --copies "$d/venv" && "$d/venv/bin/python" -m pip --version; rm -rf "$d"
```

Importing the `venv` module is not enough of a check: on Debian and Ubuntu it imports without `python3-venv`, and only building an environment fails.

---

## 5. Nomad does not run as root (customer requirement)

**Confirmation:** In the default Percona automation layout, the Nomad agent is **not** run as `root`.

| Mode | How it is deployed | Process user |
|------|-------------------|--------------|
| **Default** | **User systemd** unit (`~/.config/systemd/user/nomad.service`), with `loginctl enable-linger` for the automation user | **Automation OS user** (`ansible_user_id` / `ansible_user_gid`) — not root |
| **Optional** | `nomad_use_global_systemd: true` — unit installed system-wide via `become` | Still **`User=` / `Group=`** set to the automation user in the unit file |

Systemd unit excerpt (`GAS/automation/roles/nomad/templates/systemd/nomad.service.j2`):

- When `nomad_use_global_systemd` is set, the unit includes `User={{ ansible_user_id }}` and `Group={{ ansible_user_gid }}` (the automation account, not root).
- `ExecStart` runs `nomad agent` with `-config` pointing at the automation user’s `config.hcl`.
- **Default install** uses a **user** systemd unit (`systemctl --user`) without global `User=` lines; the process still runs as the logged-in automation user that started the unit.

PMM Extensions job tasks set `"User": ""` in Nomad job JSON, which means tasks inherit the **client agent’s user** — the same unprivileged automation account, not root.

**Customer action:** Verify on each Nomad host that the running agent matches policy, e.g. `ps -o user= -C nomad` or `systemctl --user status nomad` (default) / `systemctl status nomad` (global systemd variant).

---

## 6. Access control (API and operators)

### 6.1 Nomad ACL policies

The rendered agent configuration **does not enable** Nomad’s built-in ACL system (`acl { enabled = true }` is absent). HashiCorp ACL tokens and policies are **not** the primary access-control mechanism in this deployment.

### 6.2 What scopes API access instead

Access to the Nomad HTTP API is restricted by:

1. **TLS** — API and RPC require TLS (`tls.http`, `tls.rpc`).
2. **Mutual TLS** — `verify_https_client = true`; callers must present a certificate issued by the cluster CA.
3. **Certificate distribution** — Only hosts/principals that receive key material can call the API:
   - **PMM Extensions Tasks service:** `global-client-nomad.pem` / key (mounted in PMM Extensions container).
   - **Operators:** `global-cli-nomad.pem` (automation-generated; stored under `~/.config/nomad/certs` on managed hosts).
   - **Agents:** server or client agent certs on each Nomad node.

This is **certificate-based access control**, not per-Unix-user Nomad ACL identities. Operational “who may run Nomad CLI” is governed by **who can log in as the automation user** and read those files (and by platform SSH/access policies).

Optional: `sep_nomad_readable_by_all: true` widens read access on `global-client-nomad-key.pem` for the PMM Extensions stack user inside a container (`roles/sep/tasks/stack.yaml`); default is `false`.

### 6.3 PMM Extensions application-layer control

Nomad access does **not** replace PMM Extensions’ own controls:

- Engineers authenticate to PMM Extensions via **Casdoor OAuth/JWT** (see task execution DFD).
- PMM Extensions only dispatches **predefined** job types and merged `meta` (no arbitrary shell from the UI).
- Snippet approval and app-defined commands are enforced in PMM Extensions before Nomad dispatch.

---

## 7. PMM Extensions Nomad job templates (summary)

Registered in the Tasks database (`app/tasks/db/seed.py`):

| Job ID | Purpose | Driver |
|--------|---------|--------|
| `run-command` | Proxy apps / `pt-*` style commands via `meta.command` + `meta.args` | `raw_exec` |
| `run-python` | Python payload with venv prestart, built from the host's `python3` (3.9 or newer, §4.4) | `raw_exec` |
| `exec-artifact` | Approved snippets / signed artifact download | `raw_exec` |

Common properties:

- **Type:** `batch` (parameterized dispatch).
- **Placement:** `${node.unique.name}` = `${NOMAD_META_target}`.
- **Staleness guard:** optional `check-staleness` prestart task (`raw_exec` + shell preamble).
- **Retries:** `RestartPolicy.Attempts: 0` on execution tasks (fail fast).

---

## 8. Engineering review (acceptance criteria)

| Criterion | Status |
|-----------|--------|
| References official HashiCorp documentation | §1 |
| Includes Percona-specific deployment notes | §3–§7 (GAS automation + PMM Extensions) |
| Reviewed by engineering lead for accuracy | **Pending** — complete sign-off below |

### Sign-off

| Role | Name | Date | Notes |
|------|------|------|-------|
| Document author | | | |
| Engineering lead | | | Accuracy of automation + PMM Extensions behavior |
| Security / customer architect | | | Customer delivery |

---

## 9. Document maintenance

| Source | When to update this doc |
|--------|-------------------------|
| `GAS/automation/nomad.yaml`, `roles/nomad/**` | Agent version, TLS, driver plugins, systemd/user mode |
| `GAS/automation/roles/sep/templates/prod-settings.yaml.j2` | PMM Extensions → Nomad endpoint or cert paths |
| `pmm-extensions/app/tasks/db/seed.py` | Job templates or drivers |
| `pmm-extensions/app/tasks/execution/executors/nomad/models.py` | Node selection / health filters |
| `pmm-extensions/tests/app/host_payloads.py` (`MINIMUM_HOST_PYTHON`) and the `python-version` of the Python 3.9 step in `pmm-extensions/.github/workflows/python.yaml` and `coverage-main.yml` | The executor-host Python floor in §4.4, which `tests/app/test_host_payloads.py` checks the shipped payloads against: each file loads under that Python, uses no newer standard-library API, and evaluates no `X \| Y` type union at runtime |

**Automation repository:** Percona **GAS** repository, `automation/` directory (sibling to PMM Extensions in Percona’s source layout). Paths in §3 are relative to that tree.
