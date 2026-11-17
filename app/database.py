"""SQLite connection management and schema definition.

FastAPI may schedule a sync dependency and the sync endpoint it feeds on
different worker threads, so thread-local connections are NOT safe: one
connection could be used concurrently by two threads, which SQLite rejects
("cannot start a transaction within a transaction").  Instead the pool hands
out a connection per request and takes it back when the request ends, so a
connection is only ever owned by one thread at a time.  WAL permits
concurrent readers; writers serialize via ``BEGIN IMMEDIATE`` and the busy
timeout.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Every meter reading submission.  Revisions of one source are kept forever
-- for audit; only the highest revision of each source_id is authoritative.
CREATE TABLE IF NOT EXISTS readings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id   TEXT    NOT NULL,
    revision    INTEGER NOT NULL CHECK (revision >= 1),
    device_id   TEXT    NOT NULL,
    ts          TEXT    NOT NULL,           -- UTC, ISO-8601
    meter_no    TEXT    NOT NULL,
    cumulative  REAL    NOT NULL CHECK (cumulative >= 0),
    content_hash TEXT   NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(source_id, revision)
);
CREATE INDEX IF NOT EXISTS idx_readings_device ON readings(device_id);

-- Meter-change handover statements, same revision rules as readings.
CREATE TABLE IF NOT EXISTS handovers (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id      TEXT    NOT NULL,
    revision       INTEGER NOT NULL CHECK (revision >= 1),
    device_id      TEXT    NOT NULL,
    ts             TEXT    NOT NULL,
    old_meter_no   TEXT    NOT NULL,
    old_final      REAL    NOT NULL CHECK (old_final >= 0),
    new_meter_no   TEXT    NOT NULL,
    new_initial    REAL    NOT NULL CHECK (new_initial >= 0),
    content_hash   TEXT    NOT NULL,
    created_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(source_id, revision)
);
CREATE INDEX IF NOT EXISTS idx_handover_device ON handovers(device_id);

-- Computed consumption intervals.
--   status = 'current' : the interval is eligible to supply data
--   status = 'stale'   : superseded after a late submission/correction or an
--                        interrupted rebuild; kept for audit, never served as
--                        current data
-- An undeterminable interval is still a valid 'current' row: energy_kwh is
-- NULL and reason explains why.  We never write 0 to mean "cannot decide".
CREATE TABLE IF NOT EXISTS intervals (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id        TEXT    NOT NULL,
    start_ts         TEXT    NOT NULL,
    end_ts           TEXT    NOT NULL,
    start_meter_no   TEXT    NOT NULL,
    end_meter_no     TEXT    NOT NULL,
    energy_kwh       REAL,                  -- NULL when undeterminable
    status           TEXT    NOT NULL CHECK (status IN ('current','stale')),
    reason           TEXT,                  -- set exactly when energy_kwh IS NULL
    -- provenance: the source ids / revisions of the readings bounding the interval
    start_source_id  TEXT    NOT NULL,
    start_revision   INTEGER NOT NULL,
    end_source_id    TEXT    NOT NULL,
    end_revision     INTEGER NOT NULL,
    -- handover used to settle a cross-meter interval (if any)
    handover_source_id TEXT,
    handover_revision  INTEGER,
    computed_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    superseded_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_intervals_device_status
    ON intervals(device_id, status);
CREATE INDEX IF NOT EXISTS idx_intervals_status ON intervals(status);

-- Rebuild run audit trail.  'failed'/'interrupted' runs never delete stale
-- rows, and no run is ever started on service boot.
CREATE TABLE IF NOT EXISTS rebuild_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   TEXT,
    status      TEXT    NOT NULL CHECK (status IN ('running','success','failed','interrupted')),
    detail      TEXT,
    started_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    finished_at TEXT
);

-- Reconciliation conclusions between one total meter device and several
-- sub-meter devices over a closed UTC window.  A row is one immutable
-- evaluation ('published' when every segment could be stated, 'blocked'
-- otherwise).  A later accepted reading/handover correction on any
-- participating device flips it to 'superseded' in the same transaction;
-- superseded rows stay queryable for audit and can never become current again.
CREATE TABLE IF NOT EXISTS reconciliations (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    total_device_id   TEXT    NOT NULL,
    sub_device_ids    TEXT    NOT NULL,       -- canonical JSON array
    start_ts          TEXT    NOT NULL,
    end_ts            TEXT    NOT NULL,
    tolerance_kwh     REAL    NOT NULL CHECK (tolerance_kwh >= 0),
    fingerprint       TEXT    NOT NULL,
    version           INTEGER NOT NULL CHECK (version >= 1),
    status            TEXT    NOT NULL CHECK (status IN ('published','blocked','superseded')),
    segments          TEXT,                   -- JSON; NULL unless published
    blocked_devices   TEXT,                   -- JSON array; empty unless blocked
    exceeded          INTEGER,                -- 1/0 when published, NULL when blocked
    detail            TEXT,
    created_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    superseded_at     TEXT,
    superseded_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_reconciliations_fingerprint
    ON reconciliations(fingerprint, version);
CREATE INDEX IF NOT EXISTS idx_reconciliations_status ON reconciliations(status);

-- Which devices a conclusion depends on (total and each sub), indexed so an
-- accepted correction can revoke exactly the affected conclusions.
CREATE TABLE IF NOT EXISTS reconciliation_devices (
    reconciliation_id INTEGER NOT NULL REFERENCES reconciliations(id),
    device_id         TEXT    NOT NULL,
    role              TEXT    NOT NULL CHECK (role IN ('total','sub')),
    PRIMARY KEY (reconciliation_id, device_id)
);
CREATE INDEX IF NOT EXISTS idx_reconciliation_devices_device
    ON reconciliation_devices(device_id);
"""


def _open_one(db_path: str) -> sqlite3.Connection:
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # Transactions are managed explicitly with BEGIN IMMEDIATE below.  Leaving
    # sqlite3's implicit-transaction mode on makes DML open a transaction
    # behind our back, which under concurrent requests leads to
    # "cannot start a transaction within a transaction".
    conn.isolation_level = None
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


class ConnectionPool:
    """Hands out one connection per request; all back the same SQLite file."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._idle: list[sqlite3.Connection] = []
        self._all: list[sqlite3.Connection] = []
        self._lock = threading.Lock()

    def acquire(self) -> sqlite3.Connection:
        with self._lock:
            if self._idle:
                return self._idle.pop()
            conn = _open_one(self.db_path)
            self._all.append(conn)
            return conn

    def release(self, conn: sqlite3.Connection) -> None:
        # Never return a connection that still holds an open transaction or
        # an uncommitted write: roll it back so the next borrower starts clean.
        try:
            if conn.in_transaction:
                conn.rollback()
        except sqlite3.Error:
            pass
        with self._lock:
            self._idle.append(conn)

    def init_schema(self) -> None:
        conn = self.acquire()
        try:
            init_db(conn)
        finally:
            self.release(conn)

    def close(self) -> None:
        with self._lock:
            for conn in self._all:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            self._all.clear()
            self._idle.clear()


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
    # A process restart must not resurrect anything: rebuilds left in the
    # 'running' state by a crash are marked interrupted, stale rows stay stale.
    conn.execute(
        "UPDATE rebuild_runs SET status = 'interrupted', "
        "detail = COALESCE(detail,'') || ' [marked on service restart]', "
        "finished_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
        "WHERE status = 'running'"
    )
    conn.commit()
