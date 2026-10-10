# Course data management (store layer contract)

> 本文件上半部分写给使用「数据管理」页的学生；「Contract status」起的部分
> 是面向开发的冻结合同（共享契约权威），键名与闭集不得在消费包中改名或
> 扩展。

## 给学生：数据管理页怎么用

「账户菜单 → 数据管理」回答一个问题：这门课在我电脑上占了多少空间，
哪些能清、哪些删了就没了。

- **看**：每门课一行，列出各类内容（字幕、课件、文档、书签、测验、复习、
  任务记录等）的数量与最后更新时间。统计的是学习内容的文字量与文件
  占用——课程视频不在这份清单里，CourseLens 不把课程视频存到你的电脑上。
- **导出**：把这门课的学习资料导出带走（JSON 清单）。
- **重建检索**：全文检索不对劲时，可以重建这门课的检索索引；立即执行、
  不需要确认，重建的内容和原来一致。
- **清理派生数据**：AI 产物这类能重新生成的内容，确认后清理，之后可以
  重新生成（会提示这会花一次算力）；课件里的文字（PPT/OCR）依赖远端
  课件，清了要重跑一遍才有。
- **删除本地副本**：文档文件、字幕文件、课件 PDF 这些本地副本，删除前
  会显示占用的空间；确认后立即生效——没有回收站，删了就是删了。
- **删除记录**：进度、书签、测验记录、复习计划这些你自己的学习记录，
  删了不可恢复；逐条删除都要确认，批量删除还要输入课程名再确认一次。
- **解锁卡住的记录**：一条记录卡住动不了时，用它解锁。
- **学期轮换后的孤儿内容**：不再属于任何课程的残留内容也会显示在这个
  页面；「清除全部孤儿」要输入确认，动手前会把清单列给你看（还挂着
  自动化规则时也会提醒）。
- **凭据永远不在这个页面**：学号、密码、GitHub 令牌、DeepSeek Key 不被
  统计、不导出、也不会被这里的任何按钮删除——它们归「账户与连接」管。

每个按钮旁都写明删什么、能不能恢复。有任务在跑、云端结果还没导入完、
或自动化规则还挂着时，清理和删除会被整体拒绝，不会做一半。隐私口径见
[《设置与隐私指南》](settings-and-privacy-guide.md)与 README
[「安全与隐私架构」](../README.md#安全与隐私架构)。

## Contract status

Status: frozen for batch `FEATURE-DATA-CONTRACT-STORE-1` (2026-09-15). The
schema names, category closed set, action closed set, and blocker codes below
are the shared-contract authority for the course-data workspace; consumer
packages (API layer, frontend) may not rename keys or extend closed sets.

Implementation: `src/runtime/course_data_inventory.py` plus append-only
read-only aggregation methods on `LearningStore`, `CatalogRepository`, and
`TaskStore`. Zero schema migration, zero write paths, zero content-column or
file-content reads.

## Frozen schema names

- `courselens.course-data-summary.v1` — per-course summary payload
- `courselens.course-data-lecture-page.v1` — per-lecture detail page payload
- `courselens.course-data-action-result.v1` — action accept/reject receipt

Every payload carries its `schema` name. Empty or unknown values are omitted,
never fabricated (omit-not-fabricate).

## Key sets

### `course-data-summary.v1`

| Key | Type | Notes |
|---|---|---|
| `schema` | string | constant |
| `generated_at` | float | epoch seconds |
| `byte_basis` | string | constant `sqlite_file_bytes_including_indexes_and_free_pages` |
| `database_bytes` | object | `state_db`, `learning_db`, `total` — whole-file bytes (WAL/SHM sidecars included), so indexes and free pages are counted; not per-course data volume |
| `orphan_artifacts` | object | `directories`, `bytes` — namespace directories whose key cannot be resolved to a course |
| `page` | object | `page`, `page_size` (≤200), `total` after filtering |
| `rows` | array | one row per course (catalog rows first, sorted by `course_id`; orphan rows appended) |
| `unattributed` | object | `categories` map for rows that cannot be course-attributed (review plans; sub-keyed rows with no resolvable course; cross-course task records for `search_answer` / `concept_analysis`, whose comma-joined `course_id` is an operation scope, not a course) — omitted when empty |
| `scan_truncated` | bool | present only when the bounded directory walk hit `MAX_SCAN_DIRECTORIES` |

Summary row keys: `course_id`, `in_catalog`, `categories`, `file_bytes`,
`total_file_bytes`; catalog rows additionally carry `title`, `teacher`,
`lecture_count`. Orphan rows (learning rows whose `course_id` fails the
`NOT EXISTS` catalog probe) omit `title`/`teacher`/`lecture_count` and appear
only when `include_orphans` is set.

`categories` maps the frozen category closed set to
`{count, text_bytes, last_updated_at}`; categories with zero rows are omitted:

```
progress, transcript, ppt, artifacts, documents, references, timeline,
search, bookmarks, quizzes, review, tasks, automation
```

`text_bytes` is a text-volume proxy (SQL `SUM(LENGTH(...))` over the
category's text columns), not file size. `last_updated_at` is epoch seconds
(0 when unknown). `file_bytes` maps the file namespaces `documents`,
`subtitles`, `courseware` to stat byte sums for that course.

### `course-data-lecture-page.v1`

Keys: `schema`, `course_id`, `in_catalog`, `total`, `page` (`limit` ≤50,
`offset`), `lectures`, and `categories` (course-level, omitted when empty).
Lecture rows: `sub_id`, `in_catalog`, `file_bytes`, `total_file_bytes`;
catalog lectures additionally carry `title` and `date`. Catalog lectures sort
by `date`, `sub_id`; learning/task-derived lectures missing from the catalog
are appended with `in_catalog: false`.

### `course-data-action-result.v1`

Keys: `schema`, `action`, `operation_id`, `status` (`accepted` | `rejected`),
`blockers` (rejected only), `result` (accepted only). Blocker entries are
`{code, count}` from the closed set:

```
active_task, active_remote_run, active_automation_import,
automation_rule, cleanup_pending
```

Each code maps to one existing closed store probe: `find_active` (per targeted
lecture, kinds `subtitle`/`summary`), `active_remote_run_count`,
`active_automation_import_count`, `automation_course_rule_counts` (per
targeted course), and `migration_cleanup_pending_count`. Any non-zero probe
rejects the whole batch up front — bulk operations never run partially.

## Action closed set and confirmation gradient

| Action | Tier | Confirmation |
|---|---|---|
| `rebuild-search` | rebuildable | none (idempotent re-derivation) |
| `purge-derived` | derived / regenerable | single confirm; AI artifacts are included as derived items (Q3) with a compute-cost note in the copy; PPT/OCR text is always described as "依赖远端 / semi-rebuildable", never plain "可重建" (Q5) |
| `remove-copies` | local copies (document files, subtitle artifacts, courseware PDFs) | single confirm with byte counts shown; delete is immediate — there is no recycle bin in MVP (Q1) |
| `delete-records` | irreversible user records (progress, bookmarks, quiz attempts, review plans) | per-item single confirm with irreversible wording; batches additionally require typed course-name confirmation (Q1) |
| `export` | read-only | none; JSON manifest only in MVP, file bundles deferred (Q4) |

Orphan data is kept and shown by default; the only bulk orphan cleanup is the
explicit "清除全部孤儿" action with typed confirmation and the orphan list
shown first, including the automation-rule warning (Q2). Lifecycle blockers
(running tasks, remote leases/recoverable runs, pending imports, active
automation rules, cleanup-pending) reject actions up front with the
machine-readable blocker list above.

## Aggregation method and byte accounting

- SQL: one GROUP BY per logical table; only identity, count, text-volume, and
  timestamp columns are touched. Optional feature tables that do not exist in
  a given database are skipped (category omitted, not fabricated).
- Files: bounded `os.scandir` stat walk (≤4096 directories per namespace,
  symlinks never followed, resolved paths checked against the namespace root)
  over exactly three closed-namespace roots:
  - `documents/<document_id>/` — `document_id = sha256(course:sub:digest)[:32]`, resolved via `learning_documents`
  - `artifacts/subtitles/<key>/` — `key = sha256("course_id:sub_id")`
  - `courseware/lec-<key>/` — `key = "lec-" + sha256("courseware-pdf.v1\\0course\\0sub")[:16]`
  Direct child files are stat-summed; contents are never read. Directories
  whose key is malformed, unresolved, symlinked, or outside the namespace
  root count as `orphan_artifacts` with their reachable bytes (0 when the
  directory itself is unsafe to stat).
- Databases: `state.db` and `learning.db` whole-file sizes including `-wal`/
  `-shm` sidecars. Basis note: these numbers include indexes and free pages;
  per-course byte values are text-volume proxies. The two-file basis is
  reported per summary payload in `byte_basis`.

## Credentials exclusion

Credentials, Fudan sessions, GitHub tokens, DeepSeek keys, and account
management are permanently outside the course-data domain: `credentials.json`
and `remote_token_leases` belong to their own domains and are never listed,
summed, or exported by this inventory or any course-data action.
