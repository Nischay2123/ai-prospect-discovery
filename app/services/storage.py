"""SQLite store for every discovery run: the query, its intent, each platform's raw results (written the moment
that platform finishes) and the final response.

Powers three things: Idempotency-Key replays, resuming a run that crashed mid-way, and an audit trail linked to
the trace ID. Personal data is not kept forever: rows older than RETENTION_DAYS are purged.

ponytail: sync sqlite3 on the event loop is fine for a few sub-millisecond writes per request; Postgres (same
methods) for several instances.
"""

import hashlib
import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.errors import IdempotencyConflict, IdempotencyInProgress
from app.schemas import PlatformName, PlatformStatus, SearchHit
from app.utils.normalizers import normalize_query

SCHEMA = """
CREATE TABLE IF NOT EXISTS discovery_runs (
    id                TEXT PRIMARY KEY,
    idempotency_key   TEXT UNIQUE,            -- NULL when the caller sent no key
    query             TEXT NOT NULL,
    query_fingerprint TEXT NOT NULL,          -- sha256(normalised query): rejects key reuse with another query
    intent_json       TEXT,
    response_json     TEXT,                   -- full debug response; NULL while running
    success           INTEGER,
    total_prospects   INTEGER,
    trace_id          TEXT,
    llm_cost_usd      REAL,
    created_at        TEXT NOT NULL,
    completed_at      TEXT
);
CREATE TABLE IF NOT EXISTS source_results (
    run_id      TEXT NOT NULL REFERENCES discovery_runs(id) ON DELETE CASCADE,
    platform    TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('ok', 'failed', 'not_configured')),
    provider    TEXT,
    error_type  TEXT,
    error       TEXT,
    found       INTEGER NOT NULL,
    hits_json   TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (run_id, platform)
);
CREATE INDEX IF NOT EXISTS idx_runs_created ON discovery_runs(created_at);
"""


def _now() -> datetime:
    return datetime.now(UTC)


def fingerprint(query: str) -> str:
    return hashlib.sha256(normalize_query(query).encode()).hexdigest()


@dataclass
class Run:
    id: str
    replay: dict | None = None                    # stored response for a completed idempotent request
    done_sources: dict[PlatformName, list[SearchHit]] = field(default_factory=dict)  # resume after a crash


PURGE_EVERY = timedelta(hours=24)


class Storage:
    def __init__(self, path: str, retention_days: float | None = None) -> None:
        self._retention_days = retention_days
        self._last_purge: datetime | None = None
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._db:
            self._db.execute("PRAGMA foreign_keys = ON")
            self._db.executescript(SCHEMA)

    def start(self, query: str, idempotency_key: str | None, trace_id: str | None) -> Run:
        """Record the run before any API is called, or reopen it for a repeated Idempotency-Key."""
        self._purge_if_due()
        if idempotency_key:
            row = self._db.execute("SELECT id, query_fingerprint, response_json FROM discovery_runs "
                                   "WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
            if row is not None:
                if row["query_fingerprint"] != fingerprint(query):
                    raise IdempotencyConflict("Idempotency-Key was already used with a different query")
                if row["response_json"]:
                    return Run(id=row["id"], replay=json.loads(row["response_json"]))
                return Run(id=row["id"], done_sources=self._done_sources(row["id"]))
        run = Run(id=uuid.uuid4().hex)
        try:
            with self._db:
                self._db.execute(
                    "INSERT INTO discovery_runs (id, idempotency_key, query, query_fingerprint, trace_id, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (run.id, idempotency_key, query, fingerprint(query), trace_id, _now().isoformat()),
                )
        except sqlite3.IntegrityError:  # same new key inserted concurrently by another worker
            raise IdempotencyInProgress("A request with this Idempotency-Key is still in progress") from None
        return run

    def record_source(self, run_id: str, platform: PlatformName, hits: list[SearchHit], status: PlatformStatus) -> None:
        """One platform's outcome, stored as soon as it is known (latest attempt wins)."""
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO source_results VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, platform, status.status, status.provider, status.error_type, status.error, status.found,
                 json.dumps([asdict(h) for h in hits]), _now().isoformat()),
            )

    def finish(self, run_id: str, intent_json: str | None, response: dict, llm_cost_usd: float | None) -> None:
        with self._db:
            self._db.execute(
                "UPDATE discovery_runs SET intent_json = ?, response_json = ?, success = ?, total_prospects = ?,"
                " llm_cost_usd = ?, completed_at = ? WHERE id = ?",
                (intent_json, json.dumps(response), int(response.get("success", False)),
                 response.get("total_prospects"), llm_cost_usd, _now().isoformat(), run_id),
            )

    def _purge_if_due(self) -> None:
        """Retention is enforced while running, not only at startup (a long-lived container would never purge)."""
        if self._retention_days is None:
            return
        now = _now()
        if self._last_purge is None or now - self._last_purge >= PURGE_EVERY:
            self._last_purge = now
            self.purge_older_than(self._retention_days)

    def purge_older_than(self, days: float) -> int:
        cutoff = (_now() - timedelta(days=days)).isoformat()
        with self._db:
            ids = [r["id"] for r in self._db.execute("SELECT id FROM discovery_runs WHERE created_at < ?", (cutoff,))]
            self._db.executemany("DELETE FROM source_results WHERE run_id = ?", [(i,) for i in ids])
            self._db.executemany("DELETE FROM discovery_runs WHERE id = ?", [(i,) for i in ids])
        return len(ids)

    def get(self, run_id: str) -> dict | None:
        row = self._db.execute("SELECT * FROM discovery_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        sources = self._db.execute("SELECT platform, status, provider, error_type, found FROM source_results "
                                   "WHERE run_id = ? ORDER BY platform", (run_id,)).fetchall()
        return {**dict(row), "sources": [dict(s) for s in sources]}

    def _done_sources(self, run_id: str) -> dict[PlatformName, list[SearchHit]]:
        rows = self._db.execute("SELECT platform, hits_json FROM source_results WHERE run_id = ? AND status = 'ok'",
                                (run_id,))
        return {r["platform"]: [SearchHit(**h) for h in json.loads(r["hits_json"])] for r in rows}

    def close(self) -> None:
        self._db.close()
