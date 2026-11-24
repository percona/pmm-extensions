# Diagnostic categories

Every script under `snippets/` declares a `diagnostic_categories` key in its header. The key decides whether Support diagnostics shows the script in its category browser. This page lists the permitted values and what is enforced.

To write a script, see [Write a diagnostic script](../how-to/write-a-diagnostic-script.md).

## The key

| Form | Meaning |
|---|---|
| A YAML list of one or more category names | The script appears in the browser under each category named. |
| `# diagnostic_categories: []`, on one line | The script is found by search only. |
| `# diagnostic_categories:` with no value | Not valid. The YAML parser reads it as no value, the reader logs a warning, and the script is found by search only. |

Search finds every script, whatever the key says. Of the 89 scripts in `snippets/`, 38 are in the browser.

## Permitted values

The key takes the member name, from the first column. A display label, or a name that the taxonomy does not define, places the script in no category, with no error.

The taxonomy is `ATWCategory` in `app/extensions/apps/atw/categories.py`: fourteen members under four parents.

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

## Roots

The browser groups the categories under one root for each technology. The root comes from the script's `service_type` key, not from `diagnostic_categories`.

| `service_type` | Root |
|---|---|
| `mysql`, `mongodb`, `postgresql`, `proxysql`, `haproxy` | The technology's display name |
| Absent, or a value not in this table | Generic |

## What is enforced

`tests/app/extensions/snippets/test_frontmatter_authoring.py` reads every file under `snippets/` and checks it against the rules in its module docstring. Two of those rules concern this key:

| Rule | Requirement |
|---|---|
| R12 | Every script declares `diagnostic_categories` with a list value, which may be empty. |
| R13 | Every category that a script declares is a member of `ATWCategory`. |

Only the test reports a violation. A script with a missing key or an unknown name is still loaded and still runs, and the browser does not show it.

`tests/docs/test_diagnostic_authoring_taxonomy.py` checks that the table on this page matches `ATWCategory`.
