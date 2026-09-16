"""SQLite persistence: page validators, seen addresses, discovered routes,
and a durable alert outbox.

Dedup and delivery share one transaction. `record_candidate` marks an address
seen and enqueues its alert atomically, so a crash can neither drop an alert
nor produce a duplicate one on restart.

Writes are tiny and infrequent (only when a page actually changes), so plain
sqlite3 in WAL mode behind a lock is both simpler and faster here than an
async driver.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path

from .models import Alert, Candidate

log = logging.getLogger(__name__)

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS page_state (
    target_key    TEXT PRIMARY KEY,
    site_id       TEXT NOT NULL,
    url           TEXT NOT NULL,
    etag          TEXT,
    last_modified TEXT,
    body_sha256   TEXT,
    last_status   INTEGER,
    last_ok_at    REAL,
    last_change_at REAL,
    checks        INTEGER NOT NULL DEFAULT 0,
    changes       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS seen_addresses (
    site_id     TEXT NOT NULL,
    chain       TEXT NOT NULL,
    address_key TEXT NOT NULL,
    address     TEXT NOT NULL,
    first_url   TEXT NOT NULL,
    context     TEXT NOT NULL,
    confidence  REAL NOT NULL,
    label       TEXT,
    first_seen  REAL NOT NULL,
    last_seen   REAL NOT NULL,
    sightings   INTEGER NOT NULL DEFAULT 1,
    alerted     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (site_id, chain, address_key)
);
CREATE INDEX IF NOT EXISTS idx_seen_first ON seen_addresses(first_seen DESC);

CREATE TABLE IF NOT EXISTS known_routes (
    site_id    TEXT NOT NULL,
    url        TEXT NOT NULL,
    status     INTEGER,
    source     TEXT NOT NULL,
    watched    INTEGER NOT NULL DEFAULT 0,
    first_seen REAL NOT NULL,
    PRIMARY KEY (site_id, url)
);

CREATE TABLE IF NOT EXISTS outbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  REAL NOT NULL,
    payload     TEXT NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    next_try_at REAL NOT NULL DEFAULT 0,
    sent_at     REAL
);
CREATE INDEX IF NOT EXISTS idx_outbox_pending ON outbox(sent_at, next_try_at);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    # Page validators
    # ------------------------------------------------------------------
    def get_page_state(self, target_key: str) -> sqlite3.Row | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM page_state WHERE target_key = ?", (target_key,)
            )
            return cur.fetchone()

    def save_page_state(
        self,
        target_key: str,
        site_id: str,
        url: str,
        etag: str | None,
        last_modified: str | None,
        body_sha256: str,
        status: int,
        changed: bool,
    ) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO page_state (target_key, site_id, url, etag, last_modified,
                                        body_sha256, last_status, last_ok_at,
                                        last_change_at, checks, changes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(target_key) DO UPDATE SET
                    etag = excluded.etag,
                    last_modified = excluded.last_modified,
                    body_sha256 = excluded.body_sha256,
                    last_status = excluded.last_status,
                    last_ok_at = excluded.last_ok_at,
                    last_change_at = CASE WHEN ? THEN excluded.last_change_at
                                          ELSE page_state.last_change_at END,
                    checks = page_state.checks + 1,
                    changes = page_state.changes + ?
                """,
                (
                    target_key, site_id, url, etag, last_modified, body_sha256,
                    status, now, now if changed else None, 1 if changed else 0,
                    1 if changed else 0, 1 if changed else 0,
                ),
            )

    # ------------------------------------------------------------------
    # Addresses + outbox (atomic)
    # ------------------------------------------------------------------
    def record_candidate(
        self, site_id: str, cand: Candidate, *, suppress_alert: bool
    ) -> Alert | None:
        """Record a sighting. Returns the Alert if this is a first sighting
        that should be delivered, else None.

        The seen-marker and the outbox row are written in one transaction.
        """
        from .chains import normalize

        key = normalize(cand.address, cand.chain)
        now = time.time()

        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                cur = self._conn.execute(
                    "SELECT alerted FROM seen_addresses WHERE site_id=? AND chain=? AND address_key=?",
                    (site_id, cand.chain, key),
                )
                row = cur.fetchone()

                if row is not None:
                    self._conn.execute(
                        """UPDATE seen_addresses
                           SET last_seen = ?, sightings = sightings + 1,
                               confidence = MAX(confidence, ?)
                           WHERE site_id=? AND chain=? AND address_key=?""",
                        (now, cand.confidence, site_id, cand.chain, key),
                    )
                    self._conn.execute("COMMIT")
                    return None

                self._conn.execute(
                    """INSERT INTO seen_addresses
                       (site_id, chain, address_key, address, first_url, context,
                        confidence, label, first_seen, last_seen, sightings, alerted)
                       VALUES (?,?,?,?,?,?,?,?,?,?,1,?)""",
                    (
                        site_id, cand.chain, key, cand.address, cand.source_url,
                        cand.context, cand.confidence, cand.label, now, now,
                        0 if suppress_alert else 1,
                    ),
                )

                if suppress_alert:
                    self._conn.execute("COMMIT")
                    return None

                alert = Alert(
                    site_id=site_id,
                    chain=cand.chain,
                    address=cand.address,
                    source_url=cand.source_url,
                    context=cand.context,
                    confidence=cand.confidence,
                    label=cand.label,
                )
                self._enqueue_locked(alert, now)
                self._conn.execute("COMMIT")
                return alert
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def _enqueue_locked(self, alert: Alert, now: float) -> None:
        # Alert is a slots dataclass, so it has no __dict__; asdict() is the
        # supported way to serialise it.
        self._conn.execute(
            "INSERT INTO outbox (created_at, payload, next_try_at) VALUES (?, ?, ?)",
            (now, json.dumps(dataclasses.asdict(alert)), now),
        )

    def enqueue_alert(self, alert: Alert) -> None:
        now = time.time()
        with self._lock:
            self._enqueue_locked(alert, now)

    def pending_alerts(self, limit: int = 20) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(
                """SELECT id, payload, attempts FROM outbox
                   WHERE sent_at IS NULL AND next_try_at <= ?
                   ORDER BY id LIMIT ?""",
                (time.time(), limit),
            )
            return cur.fetchall()

    def mark_sent(self, outbox_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE outbox SET sent_at = ? WHERE id = ?", (time.time(), outbox_id)
            )

    def mark_failed(self, outbox_id: int, retry_in: float) -> None:
        with self._lock:
            self._conn.execute(
                """UPDATE outbox SET attempts = attempts + 1, next_try_at = ?
                   WHERE id = ?""",
                (time.time() + retry_in, outbox_id),
            )

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------
    def add_route(self, site_id: str, url: str, status: int, source: str) -> bool:
        """Insert a route. Returns True when it was previously unknown."""
        with self._lock:
            cur = self._conn.execute(
                """INSERT OR IGNORE INTO known_routes
                   (site_id, url, status, source, watched, first_seen)
                   VALUES (?,?,?,?,0,?)""",
                (site_id, url, status, source, time.time()),
            )
            return cur.rowcount > 0

    def mark_watched(self, site_id: str, url: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE known_routes SET watched = 1 WHERE site_id=? AND url=?",
                (site_id, url),
            )

    def known_routes(self, site_id: str) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM known_routes WHERE site_id = ?", (site_id,)
            )
            return cur.fetchall()

    # ------------------------------------------------------------------
    # Meta + stats
    # ------------------------------------------------------------------
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            cur = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,))
            row = cur.fetchone()
            return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO meta (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (key, value),
            )

    def stats(self) -> dict:
        with self._lock:
            def one(sql: str, *args):
                return self._conn.execute(sql, args).fetchone()[0]

            return {
                "addresses": one("SELECT COUNT(*) FROM seen_addresses"),
                "alerted": one("SELECT COUNT(*) FROM seen_addresses WHERE alerted = 1"),
                "routes": one("SELECT COUNT(*) FROM known_routes"),
                "pages": one("SELECT COUNT(*) FROM page_state"),
                "checks": one("SELECT COALESCE(SUM(checks), 0) FROM page_state"),
                "changes": one("SELECT COALESCE(SUM(changes), 0) FROM page_state"),
                "outbox_pending": one("SELECT COUNT(*) FROM outbox WHERE sent_at IS NULL"),
            }
