# Local data migration recovery protocol

CourseLens treats `state.db` and `learning.db` as one migration set. The
protocol is deliberately separate from application startup until the lifecycle
can stop every database user before migration and start them only after a
successful result.

## Supported versions and steps

Protocol schema versions `0` and `1` are supported. Version `0` means the
protocol metadata table is absent. The only current step is:

1. `0 -> 1`: run the application-provided schema callback inside
   `BEGIN IMMEDIATE`, validate it, and record protocol version `1`.

Any version greater than `1` fails closed with
`LOCAL_DATA_E_VERSION_TOO_NEW`. Version metadata is written only after the
application callback succeeds.

## Backup and recovery

Backups live under `runtime/data/migration-backups/<UTC timestamp>-<random>/`
when the application data root is `runtime/data`. Each set contains only
`state.db`, `learning.db`, and `manifest.json`; media and credentials are never
copied. SQLite's backup API creates a consistent database image, including
committed WAL content without copying `-wal` or `-shm` files.

Database and manifest files are written to a same-directory temporary file,
flushed, atomically replaced, and restricted to the current user where the
platform permits. A database larger than 512 MiB is rejected. The newest three
completed or restored sets are retained; incomplete or unreadable sets are
never pruned automatically.

The manifest includes only file size and hash, protocol version, table names,
and table row counts. It never includes rows, course titles, document text,
URLs, cookies, account data, SQL parameters, or credentials.

Before backup and after migration/restore, both `quick_check` and
`integrity_check` must return `ok`. Required-table and non-decreasing row-count
invariants protect durable records. A success manifest is written only after
both databases pass their final validation. On any failure, both original
databases are restored and checked again. Restore is idempotent.

An incomplete `backup_complete`, `migrating`, or `restoring` set is restored
before a later migration may write. A lock file rejects concurrent launches;
locks older than 15 minutes are treated as crash residue.

Schema callbacks use `migration_step` around table creation, index creation,
deduplication, deletion, and table/column renames. Each operation exposes
`step.before.<operation>` and `step.after.<operation>`. The coordinator also
exposes backup before/after, callback, schema-version write, pre/post commit,
post-validation, manifest success, and per-database restore-replace boundaries.

## Stable error codes

| Code | Meaning |
| --- | --- |
| `LOCAL_DATA_E_BUSY` | Another process holds the migration lock. |
| `LOCAL_DATA_E_SIZE_LIMIT` | A database exceeds the backup size policy. |
| `LOCAL_DATA_E_INTEGRITY` | A source, backup, migrated, or restored DB is corrupt. |
| `LOCAL_DATA_E_INVARIANT` | A required table or durable row-count invariant failed. |
| `LOCAL_DATA_E_VERSION_TOO_NEW` | Protocol schema is newer than this client. |
| `LOCAL_DATA_E_BACKUP_UNAVAILABLE` | Backup or manifest is missing, changed, or unreadable. |
| `LOCAL_DATA_E_MIGRATION_FAILED` | A migration boundary failed and restore succeeded. |
| `LOCAL_DATA_E_RESTORE_FAILED` | An explicit restore could not complete. |
| `LOCAL_DATA_E_RECOVERY_REQUIRED` | Automatic restore failed; writes must remain disabled. |

Errors contain no SQLite statement, row value, path from stored content, or
credential material. Local recovery instructions direct the user to close the
application, preserve the data directory, and use the newest validated backup.

## Lifecycle integration

The next lifecycle change should:

1. acquire the single-instance application lock and stop/close every DB user;
2. call `migrate_local_data` with state and learning schema callbacks;
3. require the critical tables for tasks, progress, timetable, search, and
   settings;
4. construct application services only after the call returns successfully;
5. keep all writes disabled when a recovery error is returned.

Do not call Store constructors before this gate: several current constructors
perform schema changes immediately.
