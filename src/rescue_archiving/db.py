"""SQLite schema, connection management, and low-level data helpers.

The custody log is the chain-of-custody foundation (Project 8 hook). It is
made *append-only at the database level* via triggers that abort any UPDATE
or DELETE. Application code cannot silently rewrite history even by mistake.

Source protection: contributor / uploader identities live in a separate
``item_sensitive`` table. Nothing in the default export path reads it. It is
emitted only when an operator passes an explicit ``--include-sensitive``
flag, and that disclosure is itself recorded in the custody log.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from . import config

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ingest_ts        TEXT NOT NULL,
    ingested_by      TEXT NOT NULL,
    source_url       TEXT,
    source_kind      TEXT NOT NULL DEFAULT 'url',   -- 'url' | 'file'
    platform         TEXT,
    claimed_location TEXT,
    claimed_datetime TEXT,
    description      TEXT,
    status           TEXT NOT NULL DEFAULT 'ingested',
    tags             TEXT,                           -- comma-separated, normalised
    graphic_flag     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS files (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id           INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    path              TEXT NOT NULL,                 -- relative to data_dir
    media_type        TEXT,                          -- 'video' | 'image' | 'page' | 'info' | 'audio' | 'other'
    role              TEXT NOT NULL DEFAULT 'original', -- 'original' | 'keyframe' | 'sidecar' | 'snapshot'
    sha256            TEXT NOT NULL,
    phash             TEXT,
    bytes             INTEGER NOT NULL,
    original_filename TEXT,
    created_ts        TEXT NOT NULL,
    UNIQUE(item_id, path)
);

CREATE TABLE IF NOT EXISTS captures (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id      INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    method       TEXT NOT NULL,                      -- 'yt-dlp' | 'gallery-dl' | 'file-ingest' | 'wayback' | 'archivebox'
    capture_ts   TEXT NOT NULL,
    wayback_url  TEXT,
    warc_path    TEXT,
    tool         TEXT,
    tool_version TEXT,
    status       TEXT NOT NULL DEFAULT 'ok',         -- 'ok' | 'failed' | 'skipped'
    detail       TEXT
);

CREATE TABLE IF NOT EXISTS verifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id     INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    verifier    TEXT,
    verified_ts TEXT NOT NULL,
    verdict     TEXT NOT NULL,                       -- e.g. 'confirmed' | 'inconclusive' | 'disputed'
    method      TEXT,
    notes       TEXT
);

-- Append-only chain of custody. See triggers below.
CREATE TABLE IF NOT EXISTS custody_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER REFERENCES items(id) ON DELETE CASCADE,
    ts      TEXT NOT NULL,
    actor   TEXT NOT NULL,
    action  TEXT NOT NULL,
    detail  TEXT
);

CREATE TRIGGER IF NOT EXISTS custody_log_no_update
BEFORE UPDATE ON custody_log
BEGIN
    SELECT RAISE(ABORT, 'custody_log is append-only: UPDATE forbidden');
END;

CREATE TRIGGER IF NOT EXISTS custody_log_no_delete
BEFORE DELETE ON custody_log
BEGIN
    SELECT RAISE(ABORT, 'custody_log is append-only: DELETE forbidden');
END;

-- Source protection: sensitive identity data, isolated and never exported
-- by default. One row per item at most.
CREATE TABLE IF NOT EXISTS item_sensitive (
    item_id           INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    uploader_handle   TEXT,
    contributor_note  TEXT,
    recorded_ts       TEXT NOT NULL,
    recorded_by       TEXT NOT NULL
);

-- Dedup links matches rather than discarding them.
CREATE TABLE IF NOT EXISTS item_links (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id   INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    other_id  INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    relation  TEXT NOT NULL,                         -- 'exact' | 'near'
    distance  INTEGER,                               -- pHash hamming distance for 'near'
    detail    TEXT,
    created_ts TEXT NOT NULL,
    UNIQUE(item_id, other_id, relation)
);

CREATE INDEX IF NOT EXISTS idx_files_item   ON files(item_id);
CREATE INDEX IF NOT EXISTS idx_files_sha    ON files(sha256);
CREATE INDEX IF NOT EXISTS idx_files_phash  ON files(phash);
CREATE INDEX IF NOT EXISTS idx_custody_item ON custody_log(item_id);
CREATE INDEX IF NOT EXISTS idx_caps_item    ON captures(item_id);
"""


def utcnow() -> str:
    """ISO-8601 UTC timestamp with second precision and explicit offset."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect(cfg: config.Config | None = None) -> Iterator[sqlite3.Connection]:
    cfg = cfg or config.get_config()
    cfg.ensure_dirs()
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    try:
        # Lock down db file perms on first creation.
        config._chmod_quiet(cfg.db_path, config.DB_MODE)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(cfg: config.Config | None = None) -> None:
    cfg = cfg or config.get_config()
    with connect(cfg) as conn:
        conn.executescript(SCHEMA)
        cur = conn.execute("SELECT value FROM schema_meta WHERE key = 'version'")
        row = cur.fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES ('version', ?)",
                (str(SCHEMA_VERSION),),
            )


# ---------------------------------------------------------------------------
# Custody log: the only write helper that matters for chain-of-custody.
# ---------------------------------------------------------------------------
def log_custody(
    conn: sqlite3.Connection,
    *,
    item_id: int | None,
    actor: str,
    action: str,
    detail: str | dict[str, Any] | None = None,
) -> None:
    if isinstance(detail, dict):
        detail = json.dumps(detail, ensure_ascii=False, sort_keys=True)
    conn.execute(
        "INSERT INTO custody_log(item_id, ts, actor, action, detail) VALUES (?,?,?,?,?)",
        (item_id, utcnow(), actor, action, detail),
    )


# ---------------------------------------------------------------------------
# Item / file / capture / verification inserts
# ---------------------------------------------------------------------------
def insert_item(
    conn: sqlite3.Connection,
    *,
    ingested_by: str,
    source_url: str | None,
    source_kind: str,
    platform: str | None,
    claimed_location: str | None,
    claimed_datetime: str | None,
    description: str | None,
    tags: str | None,
    graphic_flag: bool,
) -> int:
    cur = conn.execute(
        """INSERT INTO items
           (ingest_ts, ingested_by, source_url, source_kind, platform,
            claimed_location, claimed_datetime, description, status, tags, graphic_flag)
           VALUES (?,?,?,?,?,?,?,?, 'ingested', ?, ?)""",
        (
            utcnow(), ingested_by, source_url, source_kind, platform,
            claimed_location, claimed_datetime, description,
            normalise_tags(tags), int(graphic_flag),
        ),
    )
    return int(cur.lastrowid)


def add_file_row(
    conn: sqlite3.Connection,
    *,
    item_id: int,
    rel_path: str,
    media_type: str | None,
    role: str,
    sha256: str,
    phash: str | None,
    bytes_: int,
    original_filename: str | None,
) -> int:
    cur = conn.execute(
        """INSERT INTO files
           (item_id, path, media_type, role, sha256, phash, bytes, original_filename, created_ts)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (item_id, rel_path, media_type, role, sha256, phash, bytes_,
         original_filename, utcnow()),
    )
    return int(cur.lastrowid)


def add_capture_row(
    conn: sqlite3.Connection,
    *,
    item_id: int,
    method: str,
    wayback_url: str | None = None,
    warc_path: str | None = None,
    tool: str | None = None,
    tool_version: str | None = None,
    status: str = "ok",
    detail: str | None = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO captures
           (item_id, method, capture_ts, wayback_url, warc_path, tool, tool_version, status, detail)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (item_id, method, utcnow(), wayback_url, warc_path, tool, tool_version, status, detail),
    )
    return int(cur.lastrowid)


def add_verification_row(
    conn: sqlite3.Connection,
    *,
    item_id: int,
    verifier: str | None,
    verdict: str,
    method: str | None,
    notes: str | None,
) -> int:
    cur = conn.execute(
        """INSERT INTO verifications (item_id, verifier, verified_ts, verdict, method, notes)
           VALUES (?,?,?,?,?,?)""",
        (item_id, verifier, utcnow(), verdict, method, notes),
    )
    return int(cur.lastrowid)


def set_sensitive(
    conn: sqlite3.Connection,
    *,
    item_id: int,
    uploader_handle: str | None,
    contributor_note: str | None,
    recorded_by: str,
) -> None:
    conn.execute(
        """INSERT INTO item_sensitive (item_id, uploader_handle, contributor_note, recorded_ts, recorded_by)
           VALUES (?,?,?,?,?)
           ON CONFLICT(item_id) DO UPDATE SET
             uploader_handle = excluded.uploader_handle,
             contributor_note = excluded.contributor_note,
             recorded_ts = excluded.recorded_ts,
             recorded_by = excluded.recorded_by""",
        (item_id, uploader_handle, contributor_note, utcnow(), recorded_by),
    )


def add_link(
    conn: sqlite3.Connection,
    *,
    item_id: int,
    other_id: int,
    relation: str,
    distance: int | None,
    detail: str | None,
) -> bool:
    """Insert a dedup link. Returns False if it already existed."""
    try:
        conn.execute(
            """INSERT INTO item_links (item_id, other_id, relation, distance, detail, created_ts)
               VALUES (?,?,?,?,?,?)""",
            (item_id, other_id, relation, distance, detail, utcnow()),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def set_status(conn: sqlite3.Connection, item_id: int, status: str) -> None:
    conn.execute("UPDATE items SET status = ? WHERE id = ?", (status, item_id))


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
def get_item(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()


def get_files(conn: sqlite3.Connection, item_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM files WHERE item_id = ? ORDER BY id", (item_id,)
    ).fetchall()


def normalise_tags(tags: str | None) -> str | None:
    if not tags:
        return None
    parts = [t.strip().lower() for t in tags.split(",")]
    seen: list[str] = []
    for p in parts:
        if p and p not in seen:
            seen.append(p)
    return ",".join(seen) if seen else None
