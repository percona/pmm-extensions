# Diagnostic Script Authoring

Every script under `snippets/` is reachable two ways: by **search**, from the
Snippets catalog, and by **browsing symptoms**, from the Support diagnostics
category browser (`GET /api/apps/atw/`). Search reaches all of them. The browser
reaches only the scripts whose frontmatter says so, through one key:

```yaml
# diagnostic_categories:
#  - SERVER_CRASHED_RESTART_NOT_SUCCESSFUL
#  - NOT_RESPONDING
```

This document is for the author deciding what that key should say — and, from
[Reporting a failure the operator can act on](#reporting-a-failure-the-operator-can-act-on)
onward, for the author deciding what the script should print when something goes
wrong.

## Every script declares the key

`diagnostic_categories:` is not optional, and that is the point. A script that
nobody had got round to categorising and a script deliberately kept out of the
browser used to look identical, so an omission could not be told from a
decision. Declaring the key everywhere makes the exclusion explicit, and makes
it checkable — see *What is enforced* below.

There are two valid spellings:

- **In the browser** — one or more category member names, as a YAML list.
- **Search-only** — the empty list, `# diagnostic_categories: []`, written on
  one line.

Do not write a bare `# diagnostic_categories:` with nothing after it. The
frontmatter is parsed with `yaml.safe_load`, which turns a valueless key into
`None` rather than an empty list; the reader logs it as a malformed value and
discards it. The script is then search-only by accident, with a warning in the
log as the only evidence — which is the ambiguity the key exists to remove.

## Deciding: symptom or cause

A script belongs in the browser when an operator who knows only the **symptom**
would reach for it. Every browser leaf is a symptom — "Server crashed — restart
not successful", "Writes are Blocked", "Overall Slowness" — and a script belongs
under one when running it *advances* the investigation: it extracts logs,
collects configuration, or summarises state broadly enough that the cause is
still unknown when you start.

A script is **search-only** when reaching it requires already knowing the
answer. A one-condition check named for the alert that fires it
(`mongodb_high_cache_miss_check.sh` → `MongoDBHighCacheMissRatio`) is reached
*from the alert*, which has already named the condition. Putting it in the
browser asks the operator to guess the cause before they have one.

Note what does **not** decide this: an `alerts:` binding. Most scripts in the
browser carry one too, so it neither qualifies nor disqualifies a script. What
discriminates is whether the script's own name and description state a *cause*
or a *symptom*, and whether its output narrows many causes or confirms one.

The corpus contains the boundary case that shows the rule is about the name and
not the filename suffix: `mongodb_blocked_writes_check.sh` is a `*_check.sh`
**and** is in the browser, because "writes are blocked" is itself a browser
leaf — its name states a symptom.

Most of the library is search-only, and that is the expected shape — well under
half the builtin scripts are in the browser.

## The permitted values

Write the **member name** — the left column below — not the display label. The
listing matches on the member name; a display label, or a name the taxonomy does
not define, places the script in no cell at all and is discarded silently.

The taxonomy is `ATWCategory` in `app/extensions/apps/atw/categories.py`, fourteen
members under four parents:

| Member name | Display label | Parent |
|---|---|---|
| `SERVER_CRASHED_RESTART_SUCCESSFUL` | Server crashed - Restart Successful | Crashes |
| `SERVER_CRASHED_RESTART_NOT_SUCCESSFUL` | Server crashed - Restart not Successful | Crashes |
| `OVERALL_SLOWNESS` | Overall Slowness | Performance Issues |
| `QUERY_TUNING_OPTIMIZATION` | Query Tuning / Optimization | Performance Issues |
| `NOT_RESPONDING` | Not Responding | Performance Issues |
| `WRITES_ARE_BLOCKED` | Writes are Blocked | Performance Issues |
| `PERFORMANCE_OTHER` | Other | Performance Issues |
| `TEMPORARY_STALLS` | Temporary Stalls | Performance Issues |
| `NATIVE_ASYNC_REPLICATION` | Native Asynchronous Replication | Replication High / Availability |
| `MULTI_SOURCE_REPLICATION` | Multi-Source replication | Replication High / Availability |
| `GALERA` | Galera | Replication High / Availability |
| `GROUP_REPLICATION` | Group Replication | Replication High / Availability |
| `REPLICA_SET_REPLICATION` | Replica Set Replication | Replication High / Availability |
| `BACKUP_PBM` | Backup / PBM | Backup and Recovery |

A script may name several categories; it then appears under each of them.

## Which root the categories hang from

The browser groups categories under a **root** per technology. The root comes
from the script's `service_type:`, not from `diagnostic_categories:` —
`mysql`, `mongodb`, `postgresql`, `proxysql`, `haproxy` and `external` map to
their display labels, and a script with no `service_type` (or an unrecognised
one) lands under **Generic**.

`service_type` is not display-only: Alert Troubleshooting reads it too, and its
vocabulary is narrower than the browser's. `AlertServiceType`
(`app/extensions/models.py`) defines only `generic`, `mysql`, `mongodb` and
`postgresql`, so a script declaring `proxysql`, `haproxy` or `external` is not
*moved* to some other alert group — it is dropped from Alert Troubleshooting
altogether, leaving one `Unknown service_type` warning in the log as the only
evidence.

On a script with one of those three roots a per-alert override is therefore
**required**, not optional: give every entry under `alerts:` its own
`service_type:` naming a value `AlertServiceType` defines, and that value
overrides the script-level one for that alert.
`snippets/proxysql_status.sh` is the worked example. The override is per entry —
setting it on one alert says nothing about the others on the same script — so an
alert added to such a script later needs its own line, and no test will tell you
when it is missing.

## What is enforced

`tests/app/extensions/snippets/test_frontmatter_authoring.py` parses every file under
`snippets/` and checks it against the numbered authoring rules in its module
docstring. That docstring is the authoritative rule list; two of the rules are
this document's subject:

- **R12** — every script declares `diagnostic_categories:` with a list value,
  populated or empty.
- **R13** — every declared category names an `ATWCategory` member.

Both fail loudly at test time, which is the only place they can: neither an
omitted key nor an unknown category name produces a parse error, and the script
ships and runs normally with the browser simply never showing it.

## Reporting a failure the operator can act on

A script is run from a form. When it fails because of a value someone typed into
that form, the output is the only thing telling them which field to change — so
it has to name the field, and it has to preserve what the tool actually said.

Both halves matter, and the reason the *flag* is the half that carries the
association is not obvious: a run's output is subject to PII anonymization on the
way to the operator. `--defaults-file` is not an entity Presidio masks and
survives intact; the host in `Access denied for user 'root'@'10.20.0.7'` is an
`IP_ADDRESS`, which is exactly what `TaskHistory.anonymize_mask` rewrites. Naming the flag is therefore the part
of the message that reliably arrives. Preserving the tool's own text is what
stops the prose from asserting a cause nobody observed.

### The three ways this goes wrong

In ascending cost to whoever is reading the output:

1. **Lost** — the prose names neither flag nor value, so the operator learns only
   that something did not work. `Cannot connect to MySQL.`
2. **Misattributed** — the prose asserts a cause the failure does not establish.
   Rendering refused credentials, a missing socket and a dead server alike as
   `GTID mode not available.` sends three different problems to the same dead end.
3. **Inverted** — a probe that never ran renders as an affirmative *finding*.
   Answering a failed `processlist` query with `No threads waiting for locks.`
   makes an operator rule out lock contention on evidence that does not exist.
   This is the one worth going out of your way to avoid: it is worse than
   printing nothing.

### The shape that avoids all three

Capture the stream instead of discarding it, and branch on the exit status:

```bash
if ! out=$($MYSQL -e "SELECT @@gtid_mode;" 2>&1); then
    echo "Could not read GTID mode (check --defaults-file): $out"
else
    printf '%s\n' "$out"
fi
```

The `||`-fallback form these scripts used to use cannot do this, because the
discarded stream is already gone by the time the fallback runs:

```bash
# Don't: the real error is unrecoverable here, so the fallback can only guess.
$MYSQL -e "SELECT @@gtid_mode;" 2> /dev/null || echo "GTID mode not available."
```

**A filtered command needs three branches, not two**, or class 3 comes back — a
two-branch version cannot tell "the tool failed" from "the filter matched
nothing":

```bash
err=$(mktemp)
if ! out=$($MYSQL -e "SELECT ... WHERE state LIKE '%lock%';" 2> "$err"); then
    echo "Could not query threads waiting for locks (check --defaults-file): $(cat "$err")"
elif [ -z "$out" ]; then
    echo "No threads waiting for locks."
else
    printf '%s\n' "$out"
fi
rm -f "$err"
```

This is the one shape where `2>&1` is wrong. Emptiness is the signal that
separates "the filter matched nothing" from "the tool did not run", and these
clients write to stderr on runs that otherwise succeed — a deprecation or
client-identity notice, a TLS warning, a server-version remark. Fold the two
streams together and one of those becomes the captured value, so the middle
branch never fires and the notice prints where `No threads waiting for locks.`
belongs. Keep the stream in its own file and read it only on the failure branch,
as `mysql_too_many_connections_check.sh` does.

### Name the cause, not the nearest parameter

A script usually has more than one thing that can fail, and they do not share an
owner. `postgresql_deadlocks_check.sh` asks PostgreSQL where its logs are, then
reads those files:

- the **query** fails on refused credentials or a wrong database — `--dbname` is
  the field to change;
- the **`tail`** fails on permissions or a rotated-away file, over paths the
  *server* reported. Changing `--dbname` repairs neither, so that branch names no
  parameter at all.

Attributing by proximity — reaching for whichever parameter is nearest in the
file — reproduces class 2 inside the fix for it. Two rules fall out:

- **Never name a parameter the failing invocation does not carry.**
  `mysql_proxysql_not_running_check.sh` has two admin-connection branches and only
  one of them passes `$DEFAULTS_FILE`; the other must not mention
  `--defaults-file`, because it did not use it.
- **An environmental failure gets no parameter reference.** A `systemctl status`
  check, a `pgrep`, a read of `/var/log/syslog` — no declared parameter is
  responsible, so naming one would be a guess dressed as a diagnosis.

### Where the reference comes from

Use the token the operator would actually type, which is not always `--$name`:

| Front matter | Renders as |
|---|---|
| plain parameter | `--dbname` |
| `arg_format: --defaults-file=${value}` | `--defaults-file` |
| `type: bool` (a flag) | bare `--mask` |
| `positional: true` | **no flag at all** — name its `label:` instead, as `disk_usage.sh` does with `Target` |

### Two cases that are not defects

- **A capability probe.** `command -v psql > /dev/null 2>&1` is testing whether a
  binary exists; the failure *is* the answer and there is no diagnostic to keep.
- **An auto-detection cascade.** Where a script tries several sources in turn —
  `pg_current_logfile()`, then `log_directory` + `log_filename`, then well-known
  distro paths — each individual probe failing is an expected step, not an error
  worth printing. What matters is that the *terminal* message names the parameter,
  as `postgresql_log_extractor.sh` does: `could not auto-detect a PostgreSQL log
  file. Pass --log-file.`

### This is a convention, not a gate

Nothing in the test suite enforces the rules in this section, deliberately. The
mechanically detectable signal — a stderr redirect to `/dev/null` — is a poor
proxy for the defect: across the scripts that declare parameters it matched 144
sites before this convention was applied, of which about 100 were legitimate, so
a gate would spend most of its time being waved through and would teach authors
to record an exemption rather than to fix a message. (Counting every script under
`snippets/`, parameter-declaring or not, the figure is higher still and the ratio
no better.) The
property that actually matters — whether the replacement prose claims something
the failure did not establish — is a judgement, and it belongs in review.

An **optional** checker an author can run on demand is a different and better
idea, and is tracked separately.
