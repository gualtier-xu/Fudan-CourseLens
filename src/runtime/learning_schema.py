"""Versioned schema for durable learning state and rebuildable search data."""

from __future__ import annotations

import sqlite3


LEARNING_SCHEMA_VERSION = 8
SEARCH_INDEX_VERSION = 2


BASE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS learning_schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS watch_progress (
    sub_id TEXT PRIMARY KEY,
    course_id TEXT NOT NULL,
    position_ms INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    completed INTEGER NOT NULL DEFAULT 0,
    playback_rate REAL NOT NULL DEFAULT 1.0,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_watch_progress_course
    ON watch_progress(course_id, updated_at DESC);
CREATE TABLE IF NOT EXISTS transcript_sources (
    sub_id TEXT PRIMARY KEY,
    source_path TEXT NOT NULL,
    source_mtime_ns INTEGER NOT NULL,
    source_size INTEGER NOT NULL,
    segment_count INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS transcript_segments (
    sub_id TEXT NOT NULL,
    segment_index INTEGER NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    text TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(sub_id, segment_index),
    FOREIGN KEY(sub_id) REFERENCES transcript_sources(sub_id)
        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_transcript_segments_time
    ON transcript_segments(sub_id, start_ms, end_ms);
CREATE TABLE IF NOT EXISTS ppt_pages (
    sub_id TEXT NOT NULL,
    page_num INTEGER NOT NULL,
    created_sec INTEGER NOT NULL,
    pptimgurl TEXT,
    text TEXT,
    ocr_status TEXT NOT NULL DEFAULT 'pending',
    ocr_at REAL,
    dhash TEXT,
    evidence_json TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(sub_id, page_num)
);
CREATE INDEX IF NOT EXISTS idx_learning_ppt_status
    ON ppt_pages(sub_id, ocr_status, created_sec);
CREATE TABLE IF NOT EXISTS ai_artifacts (
    artifact_id TEXT PRIMARY KEY,
    course_id TEXT NOT NULL,
    sub_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    model TEXT,
    content_markdown TEXT,
    content_json TEXT NOT NULL DEFAULT '{}',
    metrics_json TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(sub_id, kind, input_hash, prompt_version)
);
CREATE INDEX IF NOT EXISTS idx_ai_artifacts_latest
    ON ai_artifacts(sub_id, kind, updated_at DESC);
CREATE TABLE IF NOT EXISTS ai_artifact_parts (
    artifact_id TEXT NOT NULL,
    part_index INTEGER NOT NULL,
    source_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    model TEXT,
    content_json TEXT NOT NULL DEFAULT '{}',
    retry_count INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    error TEXT,
    updated_at REAL NOT NULL,
    PRIMARY KEY(artifact_id, part_index),
    FOREIGN KEY(artifact_id) REFERENCES ai_artifacts(artifact_id)
        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_ai_artifact_parts_status
    ON ai_artifact_parts(artifact_id, status, part_index);
CREATE TABLE IF NOT EXISTS content_references (
    reference_id TEXT PRIMARY KEY,
    course_id TEXT NOT NULL,
    sub_id TEXT NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    source TEXT NOT NULL,
    source_ref TEXT NOT NULL DEFAULT '',
    source_hash TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(course_id, sub_id, start_ms, end_ms, source, source_hash)
);
CREATE INDEX IF NOT EXISTS idx_content_references_lookup
    ON content_references(course_id, sub_id, start_ms, end_ms);
CREATE TABLE IF NOT EXISTS model_provenance (
    artifact_id TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    prompt_version TEXT NOT NULL DEFAULT '',
    input_hash TEXT NOT NULL DEFAULT '',
    pipeline_version TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    PRIMARY KEY(artifact_id, model, prompt_version),
    FOREIGN KEY(artifact_id) REFERENCES ai_artifacts(artifact_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS learning_documents (
    document_id TEXT PRIMARY KEY,
    course_id TEXT NOT NULL,
    sub_id TEXT NOT NULL,
    title TEXT NOT NULL,
    original_name TEXT NOT NULL,
    extension TEXT NOT NULL,
    media_type TEXT NOT NULL DEFAULT '',
    storage_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    page_count INTEGER NOT NULL DEFAULT 0,
    extraction_state TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    doc_type TEXT NOT NULL DEFAULT 'other',
    scope TEXT NOT NULL DEFAULT 'lecture',
    UNIQUE(course_id, sub_id, sha256)
);
CREATE INDEX IF NOT EXISTS idx_learning_documents_lecture
    ON learning_documents(course_id, sub_id, updated_at DESC);
CREATE TABLE IF NOT EXISTS learning_document_pages (
    document_id TEXT NOT NULL,
    page_num INTEGER NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    text_hash TEXT NOT NULL DEFAULT '',
    visual_hash TEXT NOT NULL DEFAULT '',
    extraction_state TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(document_id, page_num),
    FOREIGN KEY(document_id) REFERENCES learning_documents(document_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_learning_document_pages_state
    ON learning_document_pages(document_id, extraction_state, page_num);
CREATE TABLE IF NOT EXISTS document_alignments (
    alignment_id TEXT PRIMARY KEY,
    course_id TEXT NOT NULL,
    sub_id TEXT NOT NULL,
    document_hash TEXT NOT NULL,
    page_num INTEGER NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    confidence REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'suggested',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS smart_timeline_meta (
    sub_id TEXT PRIMARY KEY,
    course_id TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    classifier_version TEXT NOT NULL,
    segment_count INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS smart_timeline_segments (
    segment_id TEXT PRIMARY KEY,
    course_id TEXT NOT NULL,
    sub_id TEXT NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    label TEXT NOT NULL,
    confidence REAL NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    classifier_version TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_smart_timeline_lecture_time
    ON smart_timeline_segments(sub_id,start_ms,end_ms);
"""


SEARCH_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS search_catalog (
    sub_id TEXT PRIMARY KEY,
    course_id TEXT NOT NULL,
    course_title TEXT NOT NULL DEFAULT '',
    lecture_title TEXT NOT NULL DEFAULT '',
    teacher TEXT NOT NULL DEFAULT '',
    catalog_version TEXT NOT NULL,
    indexed_version TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_search_catalog_course
    ON search_catalog(course_id, sub_id);
CREATE TABLE IF NOT EXISTS search_documents (
    doc_key TEXT PRIMARY KEY,
    sub_id TEXT NOT NULL,
    source TEXT NOT NULL,
    source_ref TEXT NOT NULL DEFAULT '',
    document_title TEXT NOT NULL DEFAULT '',
    start_ms INTEGER,
    display_text TEXT NOT NULL,
    search_text TEXT NOT NULL,
    source_version TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_search_documents_sub_source
    ON search_documents(sub_id, source, start_ms);
"""


def _meta_int(db: sqlite3.Connection, key: str) -> int:
    row = db.execute(
        "SELECT value FROM learning_schema_meta WHERE key=?",
        (str(key),),
    ).fetchone()
    try:
        return int(row[0]) if row else 0
    except (TypeError, ValueError):
        return 0


def _write_meta(db: sqlite3.Connection, key: str, value: object) -> None:
    db.execute(
        """INSERT INTO learning_schema_meta(key,value) VALUES(?,?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
        (str(key), str(value)),
    )


# 课程知识台账（N7K K1）：一门课一条当前快照，加性建表。
#
# 为什么不让 ai_artifacts 承载：该表按 (course_id, sub_id) 汇总进课程数据
# 清单，课程级快照只能落 sub_id=''，会在清单里凭空多出一个空讲次（并进入
# materials_center 的已知对集合）。故单开一表，成本是一条 JSON 快照行。
COURSE_KNOWLEDGE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS course_knowledge_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    course_id TEXT NOT NULL,
    status TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    contract_version TEXT NOT NULL DEFAULT '',
    document_json TEXT NOT NULL,
    lecture_count INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    UNIQUE(course_id, input_hash, contract_version)
);
CREATE INDEX IF NOT EXISTS idx_course_knowledge_latest
    ON course_knowledge_snapshots(course_id, status, updated_at DESC);
"""


def ensure_course_knowledge_schema(path) -> None:
    """幂等创建课程知识快照表（传路径或连接均可）。"""
    if isinstance(path, sqlite3.Connection):
        path.executescript(COURSE_KNOWLEDGE_SCHEMA_SQL)
        return
    from contextlib import closing
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript(COURSE_KNOWLEDGE_SCHEMA_SQL)


def initialize_learning_schema(db: sqlite3.Connection) -> bool:
    """Create/migrate source tables and return whether trigram FTS is usable."""

    db.executescript(BASE_SCHEMA_SQL)
    db.executescript(SEARCH_SCHEMA_SQL)
    db.executescript(COURSE_KNOWLEDGE_SCHEMA_SQL)

    artifact_columns = {
        str(row[1]) for row in db.execute("PRAGMA table_info(ai_artifacts)").fetchall()
    }
    if "metrics_json" not in artifact_columns:
        db.execute("ALTER TABLE ai_artifacts ADD COLUMN metrics_json TEXT NOT NULL DEFAULT '{}'")

    # U8 资料分类学：类型/scope 列加性迁移，旧资料默认 lecture+other，
    # 行数据一字不动。
    document_columns = {
        str(row[1]) for row in db.execute("PRAGMA table_info(learning_documents)").fetchall()
    }
    if document_columns and "doc_type" not in document_columns:
        db.execute("ALTER TABLE learning_documents ADD COLUMN doc_type TEXT NOT NULL DEFAULT 'other'")
    if document_columns and "scope" not in document_columns:
        db.execute("ALTER TABLE learning_documents ADD COLUMN scope TEXT NOT NULL DEFAULT 'lecture'")

    # Additive evidence seam: legacy transcript caches upgrade in place and
    # keep every existing row.
    segment_columns = {
        str(row[1]) for row in db.execute("PRAGMA table_info(transcript_segments)").fetchall()
    }
    if "evidence_json" not in segment_columns:
        db.execute(
            "ALTER TABLE transcript_segments ADD COLUMN evidence_json TEXT NOT NULL DEFAULT ''"
        )

    # Additive OCR evidence seam: legacy ppt page caches upgrade in place and
    # keep every existing row.
    ppt_columns = {
        str(row[1]) for row in db.execute("PRAGMA table_info(ppt_pages)").fetchall()
    }
    if "evidence_json" not in ppt_columns:
        db.execute(
            "ALTER TABLE ppt_pages ADD COLUMN evidence_json TEXT NOT NULL DEFAULT ''"
        )

    db.execute(
        """DELETE FROM document_alignments
            WHERE rowid NOT IN (
                SELECT MAX(rowid) FROM document_alignments GROUP BY document_hash,page_num,sub_id
            )"""
    )
    db.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_document_alignments_page
           ON document_alignments(document_hash,page_num,sub_id)"""
    )

    previous_index_version = _meta_int(db, "search_index_version")
    if previous_index_version != SEARCH_INDEX_VERSION:
        db.execute("DELETE FROM search_documents")
        db.execute("DELETE FROM search_catalog")
        _write_meta(db, "search_rebuild_required", 1)

    fts_enabled = True
    try:
        db.execute(
            """CREATE VIRTUAL TABLE IF NOT EXISTS search_fts
               USING fts5(doc_key UNINDEXED, search_text, tokenize='trigram')"""
        )
        if previous_index_version != SEARCH_INDEX_VERSION:
            db.execute("DELETE FROM search_fts")
    except sqlite3.OperationalError:
        fts_enabled = False

    _write_meta(db, "learning_schema_version", LEARNING_SCHEMA_VERSION)
    _write_meta(db, "search_index_version", SEARCH_INDEX_VERSION)
    _write_meta(db, "search_fts_enabled", int(fts_enabled))
    return fts_enabled


# 考核雷达台账（N5A-P1）：幂等建表，随 ensure 学生特性族一同调用。
ASSESSMENT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS assessment_events (
    event_id TEXT PRIMARY KEY, course_id TEXT NOT NULL,
    category TEXT NOT NULL CHECK(category IN ('exam','resit','quiz','assignment','project','lab','computer_lab','attendance','rollcall','schedule_change','qa_session')),
    title TEXT NOT NULL, title_norm TEXT NOT NULL,
    due_at REAL, due_bucket TEXT NOT NULL DEFAULT '', location TEXT,
    source TEXT NOT NULL CHECK(source IN ('rule','llm','manual')),
    status TEXT NOT NULL CHECK(status IN ('unconfirmed','active','confirmed','dismissed','merged')),
    first_seen_sub_id TEXT NOT NULL, last_seen_sub_id TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]', conflict_note TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL, updated_at REAL NOT NULL,
    UNIQUE(course_id, category, title_norm, due_bucket)
);
CREATE INDEX IF NOT EXISTS idx_assessment_course_status
    ON assessment_events(course_id, status, due_at);
"""


def ensure_assessment_schema(path) -> None:
    """幂等创建考核雷达台账（传路径或连接均可）。"""
    if isinstance(path, sqlite3.Connection):
        path.executescript(ASSESSMENT_SCHEMA_SQL)
        return
    from contextlib import closing
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript(ASSESSMENT_SCHEMA_SQL)


__all__ = [
    "LEARNING_SCHEMA_VERSION",
    "SEARCH_INDEX_VERSION",
    "ASSESSMENT_SCHEMA_SQL",
    "COURSE_KNOWLEDGE_SCHEMA_SQL",
    "ensure_assessment_schema",
    "ensure_course_knowledge_schema",
    "initialize_learning_schema",
]
