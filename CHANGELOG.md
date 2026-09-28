# Changelog

All notable changes to PMM Extensions are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

<!--
Entries under [Unreleased] are assembled from per-PR fragments under
`changelog.d/` at release time. To land a user-facing change, add a fragment
via `make changelog-add TICKET=PMM-NNNNN SECTION=<section> MSG="..."` where
<section> is one of: added, changed, breaking, config, fixed, security.
See `changelog.d/README.md` for the full workflow.
-->

## [v0.13.1] - 2026-07-08

### Changed

- SEP-1474: Periodically purge aged task-execution logs (taskhistory_log) to bound SEP database growth
- SEP-1486: Bound per-execution task log growth: a rolling per-stream byte cap drops the oldest captured-log chunks so a long-running execution's logs no longer grow without limit; capped streams keep a bounded recent tail.

### Configuration Changes

- SEP-1474: Add tasks settings LOG_RETENTION_DAYS (runtime-overridable, default 90, max 365), LOG_PURGE_BATCH_SIZE (default 10000), and LOG_PURGE_INTERVAL (default daily)
- SEP-1486: Add LOG_STREAM_CAP_BYTES and LOG_STREAM_EVICTION_MAX_ROWS (tasks) to bound retained captured-log bytes per (task_history, source, stream) and per-flush eviction work.

### Fixed

- SEP-1297: PostgreSQL Backup stanza field: expose required pgBackRest stanza on the create form so backups run with the correct --stanza value instead of the node IP
- SEP-1490: Task logs are no longer duplicated or lost when the Nomad log fetch cursor moves between worker processes (the raw fetch frontier is now persisted per allocation).
- SEP-1490: The settings export endpoint no longer returns a 500 error when a URL-typed setting (such as the base URL) has a value; URL settings now serialize to their string form.
- SEP-1492: Backup tasks created before the v0.13.0 plugin rename no longer fail with a Python SyntaxError after upgrade; orphaned payload references are healed by a data migration and resolved via a relocation-stable relative reference.
- SEP-1544: Fixed PostgreSQL Config File Collector snippet failing to collect postgresql.auto.conf due to permission errors when the file is postgres-owned mode 0600.

## [v0.13.0] - 2026-06-09

### Added

- SEP-432: MongoDB query tuning snippet
- SEP-433: MongoDB pt-pmp snippet for collecting aggregated stack traces from an unresponsive mongod, defaulting to the eu-stack (pteu) dumper
- SEP-434: MongoDB "blocked writes" diagnostics snippet (`mongodb_blocked_writes_check.sh`) that periodically samples `pt-summary`, MongoDB internals (`serverStatus`, `currentOp`, `mongostat`) and OS metrics (`vmstat`, `iostat`, `mpstat`, `sar`, `top`) into a destination directory; stop early by creating an `exit-percona-monitor` marker file.
- SEP-448: MHA diagnostic snippets to collect MHA configuration files (`mha_config_files.sh`) and extract MHA manager/node log files (`mha_logs_extractor.sh`).
- SEP-1022: SEP now runs a Celery beat check on the configured Nomad TLS files (CA and client PEM) and opens or resolves PagerDuty alerts (via `ALERTING.PROVIDERS`) when a certificate is inside the configured warning window before `not_valid_after` (default: 7 days). The threshold is set with `TASKS.NOMAD.CERT_EXPIRY_WARN_DAYS` in `settings.yaml`. The beat schedule defaults to once per day and is controlled by `TASKS.NOMAD.CHECK_CERT_EXPIRY_INTERVAL` (set to `null` to disable the periodic task).
- SEP-1026: PostgreSQL config file collector snippet (postgresql.conf, postgresql.auto.conf, includes) with optional masking of sensitive values
- SEP-1033: Add support for `gcloud storage` uploads in Backups
- SEP-1045: Archives plugin now supports remote `pt-archiver` destinations: pick a destination MySQL service from inventory or enter a host/port/database manually, and the detail page displays the resolved destination. Existing same-server tasks are unaffected.

### Changed

- SEP-1102: Rename MySQL backups plugin identifiers from backup/backups to mysql_backups (module, URI path, templates, CSS, route names)
- SEP-1226: Accept legacy backup / backups values for plugin MODULE_NAME and remap them to mysql_backups with a deprecation warning. The legacy aliases will be removed in the next release; update settings.yaml overrides accordingly.

### Breaking Changes

- SEP-1102: The `/backups/*` URL path is removed — bookmarks, dashboards, and scripts referencing it will 404; `settings.yaml` deployment overrides `MODULE_NAME: backup` / `URI_PATH: /backups` must be changed to `mysql_backups` / `/mysql_backups`.

### Fixed

- SEP-879: pcs-collect-pmm-mysql.py now verifies SSL certificates by default; pass --insecure to disable for self-signed cert environments
- SEP-999: make checkmigrations no longer fails when alerts plugin is enabled; plugin-owned DB tables are now managed through Alembic via plugin-scoped migration directories and branch labels
- SEP-1108: Inventory connectivity check now resolves the Nomad target by address instead of passing the inventory display name through, fixing failures on hosts where the inventory and Nomad node names differ
- SEP-1134: Dipper legacy UI now pre-fills the executor host when the inventory display name differs from the Nomad client node name (matching the inventory dropdown's address-based resolution)
- SEP-1204: PMM STARTED annotation now posts for periodic and chained task dispatches
- SEP-1207: PostgreSQL Backups plugin no longer crashes with NoMatchFound when mysql_backups is not also enabled
- SEP-1224: Archives task creation/edit form no longer returns HTTP 422 when optional integer fields (DEST_PORT, LIMIT, SLEEP, etc.) are left empty; form-binding 422s now surface as flash messages on the originating page instead of rendering as a raw JSON blob in the browser
- SEP-1225: Binlog backup tasks now complete successfully without AttributeError when child process exits
- SEP-1232: Schema Change (alters) Recursion method dropdown now offers `hosts` instead of the invalid `host`, so `pt-online-schema-change` accepts the value and discovers replicas correctly. A one-shot data migration rewrites any legacy `--recursion-method=host` stored in existing tasks to `hosts`.
- SEP-1239: Persist and restore the binlog backup task's Alternative Host Address so the edit form pre-populates it correctly.
- SEP-1254: Executor host lookups against a host-only Nomad endpoint no longer fail with "Executor backend unreachable: Expecting value" (the endpoint's trailing slash is now stripped before the Nomad API call).
- SEP-1254: Installer compose now runs `alembic upgrade heads` (plural) for every service, so deployments no longer fail to migrate the multi-head `sep` schema.
- SEP-1260: Tasks API `/hosts/` now returns 502 JSON when the executor backend is unreachable or returns a non-JSON body, so the SEP dashboard banner surfaces the real cause instead of a generic 500.
- SEP-1286: Round-trip S3, GSUTIL, and RSYNC storage-target fields through the backup task YAML so the edit form pre-populates them consistently.
- SEP-1302: Archiver: allow entering a destination schema manually when the destination host is the same as source
- SEP-1304: Legacy Jinja archiver form: the Destination File field is now reachable (visible and submittable) when no destination table is selected, so archiving to a file works again
- SEP-1305: Archiver task creation now accepts destinations that share only the table name with the source; the same-table check compares full host + schema + table identity instead of the bare table name.
- SEP-1307: Archiver PMM annotations now attach to the source database node instead of the executor host
- SEP-1309: PostgreSQL Snippet Manager scripts now accept a configurable target database (dbname parameter, default postgres) and thread it into every psql call, so they connect successfully instead of failing when the connecting OS user has no same-named database.
- SEP-1312: Archiver: renamed the misleading 'Delete Data' toggle to 'Delete Without Archiving' and clarified its helper text (it purges source rows instead of archiving them).

### Security

- SEP-1130: Upgrade `mako` to remediate a directory-traversal weakness via backslash paths on Windows.
- SEP-1283: Upgrade Starlette to >=1.0.1 and FastAPI to >=0.136 to remediate CVE-2026-48710 (malformed Host header bypass via request.url).
- SEP-1285: Upgrade `yarl` to >=1.24.2 to remediate AIKIDO-2026-10912 (URL-parser host confusion via malformed authority/host strings).

## [v0.12.1] - 2026-05-05

### Fixed

- SEP-1093: Restore chained task dispatch when the task page is open during the parent task's completion
- SEP-1101: Dipper PMM collector now references the top-level PMM config path in error messages and form-field descriptions
- SEP-1103: Continue chain on failure setting on chained periodic tasks now hydrates correctly when editing the task in the legacy Scheduled tasks panel

## [v0.12.0] - 2026-04-30

### Added

- SEP-446: Snippets for ProxySQL logs
- SEP-449: Snippets for ProxySQL status
- SEP-491: Automatic PMM annotations for task lifecycle events (STARTED, COMPLETED, FAILED, STOPPED, LOST)
- SEP-503: PagerDuty alert triggered on inventory sync item failure
- SEP-562: Backup tasks can be chained — when scheduling a backup execution, select a follow-up task to dispatch automatically once the primary task reaches a terminal state, regardless of outcome
- SEP-687: Mydumper payload writes a lockfile on the source host to prevent concurrent backups against the same instance
- SEP-709: Snippet parameters can be organized into labeled groups in execution forms via a `groups` frontmatter field
- SEP-711: Nomad job events are collected and surfaced alongside task history for richer dispatch diagnostics
- SEP-748: NodeAgentDown alert snippet
- SEP-796: Inventory sync registered as a periodic task; schedules can be created and edited from the inventory plugin and execution history is tracked alongside other task types
- SEP-808: Alert snippets for StaleBackup, StaleUpload, StaleBackupLog, StaleUploadLog, and BackupFailed
- SEP-812: Alert snippets for PostgreSQLLockConflicts, PostgreSQLCommitRateLow, and PostgreSQLTooManyConnections
- SEP-842: Alert snippets for PostgreSQLUptime, PostgreSQLIsDown, PostgreSQLIdleInTransaction, PostgreSQLTransactionDuration, and PostgreSQLTooManyLocksAcquired
- SEP-859: Alert snippets for PostgreSQLDeadlocks and PostgreSQLArchiveFailed
- SEP-860: Alert snippets for PostgreSQLReplicationLag, PostgreSQLExporterError, and PostgreSQLWraparound
- SEP-862: MongoDB alert diagnostic snippets
- SEP-864: MySQL alert diagnostic snippets
- SEP-882: Auto-resolve PagerDuty alerts when a failed backup task is re-executed and succeeds
- SEP-904: Alert Troubleshooting plugin with index page showing alerts grouped by service type
- SEP-905: Alert Troubleshooting detail page with AJAX snippet execution and inline terminal output
- SEP-908: Backup tasks can upload to Google Cloud Storage buckets via the `gcloud storage` CLI, alongside the existing S3 and rsync upload options
- SEP-913: GAS Reporting plugin: pull advisor data and monitoring metrics from PMM for a chosen timeframe, generate HTML/PDF reports, and optionally upload them to a ServiceNow knowledge base space; report generation can be scheduled directly in SEP
- SEP-921: JSON REST API for the Checksums plugin under `/api/plugins/checksums/` (list, detail, create, delete tasks, and schema endpoint for SPA clients)
- SEP-928: Inventory Sync split button — the chevron next to the existing sync-all control opens a dropdown that lets DBAs run a single configured syncer instead of waiting for the full chain
- SEP-932: Database connectivity check endpoint via Nomad task dispatch (MySQL, PostgreSQL, MongoDB)
- SEP-933: Manual connectivity check button on inventory service detail page
- SEP-934: Automatic connectivity check on task creation with non-blocking warnings and visual indicators
- SEP-935: Pre-execution connectivity check before Nomad task dispatch with configurable mode (disabled/warn/block) and result caching
- SEP-995: Per-task "Check connectivity" checkbox on task creation forms backed by a new `SEP.CONNECTIVITY_CHECK_DEFAULT` setting to opt out of the automatic Nomad connectivity check
- SEP-997: Alert Troubleshooting: cross-tag diagnostic snippets across related alerts, and let each `alerts:` entry in a snippet's frontmatter declare its own `service_type` so a generic snippet can surface on service-specific troubleshooting pages without being duplicated
- SEP-998: Batch approval of snippets from the snippets manager
- SEP-1019: Cancel stuck pending Nomad allocations once the configurable `TASKS__STALENESS_THRESHOLD_SECONDS` threshold (default 3600 s) is exceeded. Stale allocations self-abort via a prestart staleness check, surface as the new `STALE` task history status, and emit a deduped `task_stale` alert.
- SEP-1040: Inline inventory-sync schedule management on the inventory node list page
- SEP-1041: Inventory-sync schedules can target a specific syncer or all syncers; the inventory page now renders one row per schedule.
- SEP-1046: MySQL Archiver tasks can now disable `--bulk-insert` via a checkbox in the create/edit form, allowing `pt-archiver` to run on hosts where `local_infile=OFF`

### Changed

- SEP-379: Task log streaming endpoint releases the connection promptly when the task reaches a terminal state, rather than holding it open until completion; clients can re-open the stream to follow live output
- SEP-737: Snippet form parameter tooltips replaced with info icons to prevent accidental tooltip activation on hover
- SEP-816: Reduce write amplification for `TaskHistory.execution_request` by using `JSONB` on Postgres, deferring the column by default, and clearing the sync lock via targeted `UPDATE` instead of a full ORM save
- SEP-817: Move task logs out of `execution_request.tracking.task_logs` into a dedicated append-only `taskhistory_log` table; legacy records continue to render via a dual-read fallback until their eventual cleanup. The `tracking.task_logs` field is no longer populated for new task histories and is no longer collapsed to a boolean in `TaskHistoryResponse` for pre-migration records — API consumers that relied on the field's presence should switch to streaming the `/history/{id}/logs/` endpoint.
- SEP-818: Add PostgreSQL and SQLite expression indexes on `taskhistory.execution_request->>'task'`/`->>'target'` so dispatch dedup and task-history filter queries use index scans instead of scanning a narrowed candidate set
- SEP-856: YAML frontmatter in snippet previews is collapsed by default and excluded from the line-count limit
- SEP-937: PMM connection settings moved to top-level `PMM` config section (old `SEP.PMM` path still works with deprecation warning)
- SEP-988: Convert `taskhistory.execution_request` to `JSONB` on PostgreSQL, add a GIN index `ix_taskhistory_execution_request_meta` on `execution_request->'meta'` using `jsonb_path_ops`, and refactor `_raise_if_identical_task_conflict` to use jsonb-native operators on PostgreSQL (`@>` containment for scalar meta items, jsonb equality for list/dict meta items). MySQL and SQLite continue to use the per-key text-equality loop unchanged.

### Breaking Changes

- SEP-924: Inventory list endpoints now return paginated responses with `offset`/`limit` query parameters
- SEP-925: Tasks list routes (`GET /`, `GET /history/`, `GET /{task}/history/`) now return paginated responses with `offset`/`limit` query parameters
- SEP-937: The `SEP__PMM_FRONTEND` environment variable has been removed. Use `PMM__FRONTEND` (top-level) or `SEP__PMM__FRONTEND` (nested under SEP.PMM) instead.
- SEP-988: `_raise_if_identical_task_conflict` dedup comparison on PostgreSQL is now type-strict for scalar meta values, replacing a type-loose `str(raw_value)` text comparison. A dispatch with `meta.key = 1` (int) no longer deduplicates against a pending task with `meta.key = "1"` (string), and vice versa. Bool and `None` scalar meta values were previously not deduplicated correctly on PostgreSQL (latent bugs in `str(True) == "True"` vs jsonb text output `"true"`, and `str(None) == "None"` vs jsonb `NULL`); they are now correctly deduplicated via jsonb containment.
- SEP-988: `_raise_if_identical_task_conflict` dedup comparison on PostgreSQL is now order-insensitive for dict-valued meta items. Two dispatches whose dict-valued meta items contain the same keys/values in different insertion orders were previously considered distinct and are now considered duplicates. MySQL and SQLite dedup semantics are unchanged.
- SEP-1069: `TASKS.INVENTORY_SYNC_API_KEY` removed; replaced by `SEP_INTERNAL_TOKEN` env var. Affects only operators tracking v0.12.0 RCs (never shipped stable).

### Configuration Changes

- SEP-491: Added `PMM.ANNOTATIONS_ENABLED` setting (default: `false`) to control PMM annotation creation
- SEP-491: Added `PMM.ANNOTATIONS_TIMEOUT` setting (default: `5`) to configure PMM annotation request timeout
- SEP-929: Added `UVICORN_EXTRA_RELOAD_DIRS`, `UVICORN_EXTRA_RELOAD_INCLUDES`, and `UVICORN_EXTRA_RELOAD_EXCLUDES` settings to extend uvicorn reload paths via `settings.yaml`
- SEP-935: Added `TASKS.PRE_EXECUTION_CONNECTIVITY_CHECK` setting (disabled/warn/block) to control pre-execution connectivity checks before task dispatch
- SEP-988: Upgrades that cross this revision run `ALTER TABLE taskhistory ALTER COLUMN execution_request TYPE jsonb USING execution_request::jsonb`, which rewrites every row in the `taskhistory` table under an `ACCESS EXCLUSIVE` lock on PostgreSQL. Expected downtime impact is proportional to row count (approximately one minute per two million rows on typical production hardware); size the upgrade window accordingly for deployments with large `taskhistory` tables. The Tasks API also fail-fasts at startup if the column is still plain `json` after deploying SEP-988, with a clear remediation message pointing to the migration.
- SEP-1019: New `TASKS__STALENESS_THRESHOLD_SECONDS` setting (default 3600 s) controls how long a dispatched task may wait in Nomad before self-aborting as stale.

### Fixed

- SEP-479: Archiver task marked a failed
- SEP-746: Backup log stream continues updating throughout the full duration of the run instead of stopping after a few seconds
- SEP-764: pt-osc pre-checks now execute against the database host selected in the task form instead of the default host
- SEP-810: pt-osc `recursion-method` DSN parameter now has validation and a sensible default value
- SEP-909: Task history rows no longer remain stuck in RUNNING status after the underlying Nomad job completes
- SEP-917: Hardcoded `#fff` background on form inline styles no longer breaks dark mode rendering
- SEP-989: Xtrabackup Nomad payload further optimized to stay within Nomad's 256 KB dispatch limit
- SEP-1005: Coerce `syntax_highlight` filter input to `str` so backup and archiver detail pages render correctly when task metadata contains non-string scalar values (integers, floats, booleans, None).
- SEP-1018: Periodic tasks whose target host is not ready on Nomad are now recorded as FAILED TaskHistory rows with a deduped alert instead of queuing a stuck Nomad allocation.
- SEP-1049: `backup_pg`-only installs no longer crash the homepage and plugin pages with `NoMatchFound` when none of the router-gating allowlist plugins are enabled
- SEP-1050: Cron schedule validation in SEP periodic-task and inventory-sync schedule forms now surfaces a clear validation message instead of a generic server error.
- SEP-1069: Scheduled inventory sync now uses a stable internal service token (`SEP_INTERNAL_TOKEN`) instead of a Casdoor user access token, eliminating the periodic auth failures caused by token expiry.
- SEP-1084: Archives SWAP-ARCHIVE-DROP tasks no longer crash when SWP_TABLE_SUFFIX is a date value

### Security

- SEP-883: Fixed command injection via unsanitized `backup_source` path in restore payload `_symlink_to_real()`

## [v0.11.0] - 2026-04-02

### Added

- SEP-777: Alert template YAML schema and loader (PMM Alerting plugin)
- SEP-778: PMM API client alerting endpoints
- SEP-779: Alert templates list page with service filter tabs
- SEP-780: Push to PMM functionality for alert templates
- SEP-781: PagerDuty contact point management widget
- SEP-782: Periodic alert rules backup
- SEP-783: Alert rules restore from backup
- SEP-602: Include RDS instances in inventory sync with `DEFAULT_EXECUTOR_HOST` setting
- SEP-800: IO/CPU/load/memory utilization alert snippets
- SEP-801: TimeDriftPMMAgents alert snippet
- SEP-867: `strict_executor_matching` setting to fail sync when no matching executor host
- SEP-804: `OPTIONAL_DEFAULT_TRUE` SnippetSudoOption for pre-checked sudo checkbox
- SEP-807: Signed URLs for artifact downloads instead of forwarding tokens to Nomad
- SEP-424: Next execution column in periodic tasks table
- SEP-802: Internal container registry support in installer and deployment

### Changed

- SEP-891: Core CSS dark mode fixes
- SEP-892: Theme-aware syntax highlighting for dark mode
- SEP-893: Dark mode for Simple Datatables
- SEP-894: Dark mode for flash messages, confirm modal, and saved-task containers
- SEP-895: Fix page-specific hardcoded colors for dark mode
- SEP-813: Execution target dropdowns show inventory node names alongside service names
- SEP-533: Improved cron mode UI for task scheduling
- SEP-794: Dipper form defaults dynamically populated from selected service and PMM context
- SEP-798: Updated Snippets Manager main page title
- SEP-749: PBM credentials path is now configurable
- SEP-400: All MongoDB restore options (batch size, workers, download buffers) now available in the UI
- SEP-698: Binlog PITR restore supports extra arguments via `BINLOG_RESTORE_EXTRA_ARGS`
- SEP-693: Binary log file filtering for all backup source types
- SEP-696: XB restore cleans up `.zst` files after decompression
- SEP-699: Tightened permissions on backup logs and output files
- SEP-870: Installer script respects `NO_COLOR` convention
- SEP-792: Secrets masked in logs and dev shell using Pydantic `SecretStr`

### Fixed

- SEP-868: Plugin pages fail to load for services with 130K+ schemas/tables — inventory data now loaded with pagination
- SEP-811: Frequent "CSRF signatures do not match" errors — tokens now persist across requests with correct cookie attributes
- SEP-822: Mydumper + Upload fails for file permissions when XB backup was executed first
- SEP-821: Backup S3 information not available on Edit page
- SEP-889: Navbar actions dropdown items partially unclickable
- SEP-888: Login page does not display error messages for invalid credentials
- SEP-885: Sub-app lifespan never runs in standalone mode due to `__name__` guard
- SEP-806: Global JSON deserializer eagerly converts Task.data dicts to TaskExecutionRequest
- SEP-803: Snippet/Dipper preview content leaks outside highlight container
- SEP-795: Download button stuck on 'Preparing download' after download starts
- SEP-563: Mydumper backup log not shown in full in SEP
- SEP-855: pt-stalk execution fails due to unsupported system-only option
- SEP-854: Parameter help parsing error in `mysql_version.sh`
- SEP-853: Parameter help parsing error in `mysql_query_tuning.sh`
- SEP-841: Permission issue in `mysql_log_extractor.sh` when reading MySQL error log

### Security

- SEP-931: Upgrade `Pygments` to 2.20.0 — CVE-2026-4539
- SEP-930: Bump `aiohttp` to 3.13.4
- SEP-906: Bump `Pygments` package for security
- SEP-731: Upgrade `Celery` to 5.6.0+ — credential leakage fix (AIKIDO-2025-10881)

### Configuration Changes

- SEP-602: New `DEFAULT_EXECUTOR_HOST` setting in MySQLSyncer configuration for RDS inventory sync
- SEP-867: New `strict_executor_matching` setting to fail sync when no matching executor host is found
- SEP-749: PBM credentials file path is now configurable via settings
- SEP-802: New `CONTAINER_REGISTRY` environment variable for using internal container registries in the installer

## [v0.10.3] - 2026-02-25

### Added

- SEP-662: CSRF token persistence for SPA — tokens now persist across POST requests and expire with the session
- SEP-636: Backend security hardening for the SPA proof of concept

### Fixed

- SEP-790: Xtrabackup S3 upload fails for incremental backups — `BACKUP_DIR_REGEX` was missing the "I" key for incremental backup directories
- SEP-789: MyDumper and Binlog backup metrics use wrong names — `Enum.__format__` produced malformed metric names, breaking PMM alerting

### Security

- SEP-752: Update `presidio-analyzer` and `presidio-anonymizer` to 2.2.361, resolving pinned `cryptography` vulnerability

## [v0.10.2] - 2026-02-17

### Fixed

- SEP-762: MyDumper backup fails on first execution — textfile collector attempted to stat the backup directory before it existed
- SEP-763: Xtrabackup post-run encryption fails — backup directory passed as string instead of `Path` object to GPG encryption routine

## [v0.10.1] - 2026-02-13

### Fixed

- SEP-758: Backup task cannot be executed because Nomad payload size exceeded
- SEP-759: Backup MyDumper fails execution
- SEP-760: Backup MyDumper form shows the less-locking checkbox but Edit does not

## [v0.10.0] - 2026-02-11

### Added

- SEP-651: Data collection core engine (Dipper plugin)
- SEP-652: Dipper plugin UI with host/service selection
- SEP-648: MySQL collector scripts
- SEP-649: MongoDB collector scripts
- SEP-650: PostgreSQL collector scripts
- SEP-653: PMM collector for MySQL
- SEP-692: AWS CLI support in binary log payloads
- SEP-694: Customizable S3 upload extra arguments via `AWSCLI_S3_UPLOAD_EXTRA_ARGS`
- SEP-397: Enable all PBM backup configuration options in the UI
- SEP-633: Use PMM custom labels (`sep_sync: disabled`) to ignore inventory entities during sync
- SEP-695: Add `msp_backup_running` metric for backup monitoring
- SEP-656: Type-ahead for schema and table selection in Archive form

### Changed

- SEP-718: Persistent table sort preferences saved across sessions
- SEP-550: Execution history tables default to most recent first (sorted by "Started At")
- SEP-736: Replace backup form label tooltips with info icons
- SEP-739: Disable `allow_extra_args` in all snippets to prevent unexpected behavior
- SEP-740: pt-stalk uses `--run-time` for bounded execution compatible with Nomad scheduling
- SEP-735: Remove output destination option from snippets

### Fixed

- SEP-747: MyDumper `--less-locking` regression — deprecated flag added even when disabled, breaking newer MyDumper versions
- SEP-744: Cannot save task definition changes — Edit button stayed grayed out after creation
- SEP-685: Valkey services break inventory sync — unsupported service types are now skipped
- SEP-690: Backup version comparison broken on Python 3.12+
- SEP-719: Incorrect `--defaults-file` handling in several MySQL snippets
- SEP-742: Snippet help messages causing execution errors
- SEP-741: Non-string snippet parameter defaults not working
- SEP-713: Various pt-summary snippet issues
- SEP-691: AWS CLI compatibility with `--include-from`
- SEP-715: `mysql_config_files.sh` parameter handling

### Security

- SEP-703: Update `urllib3` to 2.6.3
- SEP-701: Update `aiohttp` to 3.13.3
- SEP-728: Update `python-multipart` to 0.0.22

[Unreleased]: https://github.com/percona/pmm-extensions/compare/v0.13.1...HEAD
[v0.13.1]: https://github.com/percona/SEP/compare/v0.13.0...v0.13.1
[v0.13.0]: https://github.com/percona/SEP/compare/v0.12.1...v0.13.0
[v0.12.1]: https://github.com/percona/SEP/compare/v0.12.0...v0.12.1
[v0.12.0]: https://github.com/percona/SEP/compare/v0.11.0...v0.12.0
[v0.11.0]: https://github.com/percona/SEP/compare/v0.10.3...v0.11.0
[v0.10.3]: https://github.com/percona/SEP/compare/v0.10.2...v0.10.3
[v0.10.2]: https://github.com/percona/SEP/compare/v0.10.1...v0.10.2
[v0.10.1]: https://github.com/percona/SEP/compare/v0.10.0...v0.10.1
[v0.10.0]: https://github.com/percona/SEP/compare/v0.9.6...v0.10.0
