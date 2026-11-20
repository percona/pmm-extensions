# Security Review Checklist — PMM Extensions Task Execution DFD

This page describes the standalone deployment (v0.13.1 and earlier) except where it says otherwise, and a version for PMM Extensions inside PMM is planned.

Complete this checklist before sending customer-facing task-execution or Nomad materials.

**DFD deliverables:** [`exports/pmm-extensions-task-execution-dfd.pdf`](exports/pmm-extensions-task-execution-dfd.pdf) (and optional sequence PDF)
**Diagram sources:** `pmm-extensions-task-execution-dfd.mmd`, `pmm-extensions-task-execution-sequence.mmd`
**Companion docs:** [`README.md`](README.md), [`../nomad-driver-deployment.md`](../nomad-driver-deployment.md)

---

## Reviewer information

| Field | Value |
|-------|-------|
| Reviewer name | |
| Review date | |
| Git commit / tag reviewed | |
| PDF generated from commit | |

---

## 1. Authentication model

| # | Question | Pass | Fail | Notes |
|---|----------|:----:|:----:|-------|
| 1.1 | Diagram states engineers authenticate via **Grafana session exchange** (PMM session → short-lived PMM Extensions bearer), not client-certificate user login | ☐ | ☐ | |
| 1.2 | mTLS is shown only on **service-to-service** links (Extensions↔Tasks, Tasks↔Nomad), not as user identity | ☐ | ☐ | |
| 1.3 | Token lifecycle: exchange returns a short-lived PMM Extensions bearer in the response body; no PMM Extensions cookie is set; no refresh token is issued. When the bearer expires the SPA repeats the exchange. | ☐ | ☐ | |
| 1.4 | `EXTENSIONS_INTERNAL_TOKEN` service principal is documented if relevant to customer deployment | ☐ | ☐ | |

---

## 2. Command execution controls

| # | Question | Pass | Fail | Notes |
|---|----------|:----:|:----:|-------|
| 2.1 | UI path shows **predefined** work only (snippet args / app-fixed commands) | ☐ | ☐ | |
| 2.2 | Snippet **approval** gate is represented (Path A) | ☐ | ☐ | |
| 2.3 | **Path B** (checksums / proxy apps): create vs execute phases shown; command fixed at create | ☐ | ☐ | |
| 2.4 | Server-side construction of `meta` / Nomad job type is clear for both paths | ☐ | ☐ | |
| 2.5 | No diagram implication that users can run arbitrary shell via PMM Extensions UI | ☐ | ☐ | |

---

## 3. Data stores and retention

| # | Question | Pass | Fail | Notes |
|---|----------|:----:|:----:|-------|
| 3.1 | Tasks PostgreSQL identified as system of record for run history and output chunks | ☐ | ☐ | |
| 3.2 | PMM annotations described as secondary timeline (not primary audit store) | ☐ | ☐ | |
| 3.3 | HTTP/app logs distinguished from task execution DB | ☐ | ☐ | |
| 3.4 | Customer retention/backup expectations called out or referred to ops runbook | ☐ | ☐ | |
| 3.5 | Storage encryption-at-rest described accurately — `taskhistory_log` content is compressed-but-not-encrypted at the application layer; protection depends on PostgreSQL volume or host-level encryption | ☐ | ☐ | |

---

## 4. Access control

| # | Question | Pass | Fail | Notes |
|---|----------|:----:|:----:|-------|
| 4.1 | Authenticated users can read task history/logs **without per-user row filter** (documented accurately) | ☐ | ☐ | |
| 4.2 | Snippet approval restricted to **admin** | ☐ | ☐ | |
| 4.3 | No overstatement of “only the executor can see logs” unless deployment adds controls | ☐ | ☐ | |

---

## 5. Transport security

| # | Question | Pass | Fail | Notes |
|---|----------|:----:|:----:|-------|
| 5.1 | Browser ↔ PMM Nginx: HTTPS (PMM's Nginx terminates TLS and fronts PMM Extensions on a sub-path) | ☐ | ☐ | |
| 5.1a | Session exchange endpoint (`/api/oauth/session/exchange`) is same-origin behind PMM's Nginx — no separate IdP port | ☐ | ☐ | |
| 5.2 | Extensions ↔ Tasks/Inventory: mTLS with client certs | ☐ | ☐ | |
| 5.3 | Tasks ↔ Nomad API: TLS/mTLS per deployment config | ☐ | ☐ | |

---

## 6. Privacy and output handling

| # | Question | Pass | Fail | Notes |
|---|----------|:----:|:----:|-------|
| 6.1 | Optional log **anonymization** (`anonymize_mask`) mentioned where customer cares about PII | ☐ | ☐ | |
| 6.2 | Command parameters and stdout/stderr may contain sensitive data — customer warned appropriately | ☐ | ☐ | |

---

## 7. Deliverable integrity

| # | Question | Pass | Fail | Notes |
|---|----------|:----:|:----:|-------|
| 7.1 | PDF was regenerated from the reviewed `.mmd` commit | ☐ | ☐ | |
| 7.2 | README code-mapping table spot-checked against current codebase | ☐ | ☐ | |
| 7.3 | Sequence diagram (if provided) consistent with main DFD | ☐ | ☐ | |
| 7.4 | [`nomad-driver-deployment.md`](../nomad-driver-deployment.md) spot-checked against GAS `automation/nomad.yaml` and PMM Extensions job templates (if sending Nomad doc) | ☐ | ☐ | N/A if DFD only |

---

## 8. Nomad driver and deployment

Complete **§8** when delivering [`nomad-driver-deployment.md`](../nomad-driver-deployment.md) to the customer (with or without the DFD PDF).

| # | Question | Pass | Fail | Notes |
|---|----------|:----:|:----:|-------|
| 8.1 | Doc states **PMM Extensions dispatches `raw_exec` jobs only** and **filters Nomad nodes by `raw_exec` availability**; whether the `exec` driver is also enabled on customer Nomad agents is a customer-cluster configuration choice (PMM Extensions does not dispatch to it either way) | ☐ | ☐ | |
| 8.2 | Links to official HashiCorp **exec** and **raw_exec** driver documentation are present and correct | ☐ | ☐ | |
| 8.3 | **Nomad agent does not run as root** — default user systemd + automation OS user accurately described | ☐ | ☐ | Spot-check: `ps` / `systemctl` on a Nomad host |
| 8.4 | **Server vs client** topology matches customer inventory (`nomad_server_enabled` / `nomad_enabled` hosts) | ☐ | ☐ | |
| 8.5 | Task execution targets **DB (client) nodes** via `${node.unique.name}` = `NOMAD_META_target` constraint | ☐ | ☐ | |
| 8.6 | **Nomad ACL is not enabled**; API access scoped by **mTLS client certificates** is stated (no overstatement of ACL “users”) | ☐ | ☐ | |
| 8.7 | PMM Extensions Tasks → Nomad uses **`global-client-nomad`** cert paths consistent with customer `prod-settings` / cert mount layout | ☐ | ☐ | |
| 8.8 | Optional `sep_nomad_readable_by_all` widening of key permissions called out if enabled in customer env | ☐ | ☐ | N/A if false |
| 8.9 | Nomad doc **engineering lead sign-off** (§8 of nomad doc) completed | ☐ | ☐ | |

---

## Sign-off

| Role | Name | Date | Signature / approval |
|------|------|------|----------------------|
| Security reviewer | | | |
| Engineering author | | | |
| Product / PM (optional) | | | |

**Approved for customer delivery:** ☐ Yes  ☐ No — follow-up required

**Follow-up actions (if any):**

1.
2.
