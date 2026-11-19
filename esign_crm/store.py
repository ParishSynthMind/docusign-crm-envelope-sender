"""Local SQLite state: the send requests, webhook deliveries seen, and the audit log.

Idempotency works on three keys:
- the CRM webhook event id (CRMs retry deliveries they think failed),
- the request key `crm:deal_id:stage:template_version`: one deal reaching the
  trigger stage produces at most one contract, however many webhooks arrive,
- the DocuSign Connect delivery (envelope id + event + generated time).
An approved request is sent at most once (compare-and-set on its status).

The audit log is append-only and hash-chained: each row stores the SHA-256 of
the previous row plus its own content, so an edited or deleted row shows up in
`esign-crm audit-verify`.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS deliveries (
    delivery_key TEXT PRIMARY KEY,
    source TEXT, received_at INTEGER
);
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_key TEXT UNIQUE NOT NULL,
    crm TEXT NOT NULL,
    deal_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,         -- pending_approval, sending, sent, delivered, completed, declined, voided, failed, rejected
    envelope_id TEXT,
    deal TEXT NOT NULL,           -- JSON snapshot of the deal fields used in the contract
    pdf_path TEXT, pdf_sha256 TEXT,
    signed_path TEXT, signed_sha256 TEXT,
    certificate_path TEXT, certificate_sha256 TEXT,
    decided_by TEXT, reason TEXT,
    created_at INTEGER, updated_at INTEGER
);
CREATE INDEX IF NOT EXISTS requests_envelope ON requests(envelope_id);
CREATE TABLE IF NOT EXISTS audit (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at INTEGER NOT NULL,
    request_id INTEGER,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);
"""

GENESIS = "0" * 64
FINAL = ("completed", "declined", "voided", "rejected")


@dataclass
class Request:
    id: int
    request_key: str
    crm: str
    deal_id: str
    stage: str
    status: str
    envelope_id: str | None
    deal: dict
    pdf_path: str | None
    pdf_sha256: str | None
    signed_path: str | None
    signed_sha256: str | None
    certificate_path: str | None
    certificate_sha256: str | None
    decided_by: str | None
    reason: str | None
    created_at: int
    updated_at: int

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def _row_hash(prev_hash: str, at: int, request_id, actor: str, action: str, detail: str) -> str:
    material = json.dumps([prev_hash, at, request_id, actor, action, detail], separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class Store:
    def __init__(self, path: str | Path = ":memory:", clock=time.time):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        if str(path) != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.clock = clock

    def _now(self) -> int:
        return int(self.clock())

    # --- deliveries -----------------------------------------------------------
    def first_delivery(self, key: str, source: str) -> bool:
        """True the first time a delivery key is seen, False for a retry or duplicate."""
        cur = self.db.execute("INSERT OR IGNORE INTO deliveries VALUES (?, ?, ?)", (key, source, self._now()))
        return cur.rowcount == 1

    def forget_delivery(self, key: str) -> None:
        """Let a delivery be retried (used when processing failed before anything was written)."""
        self.db.execute("DELETE FROM deliveries WHERE delivery_key = ?", (key,))

    # --- requests -------------------------------------------------------------
    def _row(self, row) -> Request | None:
        if row is None:
            return None
        cols = [c[0] for c in self.db.execute("SELECT * FROM requests LIMIT 0").description]
        data = dict(zip(cols, row, strict=True))
        data["deal"] = json.loads(data["deal"])
        return Request(**data)

    def create_request(self, request_key: str, crm: str, deal_id: str, stage: str, deal: dict,
                       status: str = "pending_approval") -> Request | None:
        """Insert a new request. None if this request key already exists (idempotent)."""
        now = self._now()
        cur = self.db.execute(
            "INSERT OR IGNORE INTO requests (request_key, crm, deal_id, stage, status, deal, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (request_key, crm, deal_id, stage, status, json.dumps(deal, sort_keys=True), now, now))
        if cur.rowcount != 1:
            return None
        return self.get(cur.lastrowid)

    def get(self, request_id: int) -> Request | None:
        return self._row(self.db.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone())

    def by_key(self, request_key: str) -> Request | None:
        return self._row(self.db.execute("SELECT * FROM requests WHERE request_key = ?", (request_key,)).fetchone())

    def by_envelope(self, envelope_id: str) -> Request | None:
        return self._row(self.db.execute("SELECT * FROM requests WHERE envelope_id = ?", (envelope_id,)).fetchone())

    def list(self, status: str | None = None) -> list[Request]:
        if status:
            rows = self.db.execute("SELECT * FROM requests WHERE status = ? ORDER BY id", (status,)).fetchall()
        else:
            rows = self.db.execute("SELECT * FROM requests ORDER BY id").fetchall()
        return [self._row(r) for r in rows]

    def open_envelopes(self) -> list[Request]:
        """Requests with an envelope that has not reached a final status (for reconciliation)."""
        rows = self.db.execute(
            "SELECT * FROM requests WHERE envelope_id IS NOT NULL AND status IN ('sent', 'delivered') ORDER BY id"
        ).fetchall()
        return [self._row(r) for r in rows]

    def transition(self, request_id: int, from_statuses: tuple[str, ...], to_status: str, **fields) -> bool:
        """Compare-and-set: move to `to_status` only if the current status is one of `from_statuses`."""
        allowed = {"envelope_id", "pdf_path", "pdf_sha256", "signed_path", "signed_sha256",
                   "certificate_path", "certificate_sha256", "decided_by", "reason"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"cannot set {sorted(bad)}")
        sets = ["status = ?", "updated_at = ?"] + [f"{k} = ?" for k in fields]
        marks = ",".join("?" * len(from_statuses))
        cur = self.db.execute(
            f"UPDATE requests SET {', '.join(sets)} WHERE id = ? AND status IN ({marks})",
            (to_status, self._now(), *fields.values(), request_id, *from_statuses))
        return cur.rowcount == 1

    def update_fields(self, request_id: int, **fields) -> None:
        allowed = {"pdf_path", "pdf_sha256", "signed_path", "signed_sha256", "certificate_path",
                   "certificate_sha256", "reason", "envelope_id"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"cannot set {sorted(bad)}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE requests SET {sets}, updated_at = ? WHERE id = ?",
                        (*fields.values(), self._now(), request_id))

    def sent_today(self) -> int:
        since = self._now() - 86400
        return self.db.execute(
            "SELECT COUNT(*) FROM audit WHERE action = 'envelope_sent' AND at >= ?", (since,)).fetchone()[0]

    # --- audit ----------------------------------------------------------------
    def audit(self, action: str, actor: str = "system", request_id: int | None = None, **detail) -> str:
        at = self._now()
        text = json.dumps(detail, sort_keys=True, default=str)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
            prev = row[0] if row else GENESIS
            h = _row_hash(prev, at, request_id, actor, action, text)
            self.db.execute(
                "INSERT INTO audit (at, request_id, actor, action, detail, prev_hash, hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (at, request_id, actor, action, text, prev, h))
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return h

    def audit_rows(self, request_id: int | None = None) -> list[dict]:
        q = "SELECT seq, at, request_id, actor, action, detail, prev_hash, hash FROM audit"
        args: tuple = ()
        if request_id is not None:
            q += " WHERE request_id = ?"
            args = (request_id,)
        rows = self.db.execute(q + " ORDER BY seq", args).fetchall()
        keys = ["seq", "at", "request_id", "actor", "action", "detail", "prev_hash", "hash"]
        out = []
        for r in rows:
            d = dict(zip(keys, r, strict=True))
            d["detail"] = json.loads(d["detail"])
            out.append(d)
        return out

    def verify_audit(self) -> tuple[bool, int | None]:
        """(True, None) if the chain is intact, else (False, first bad seq)."""
        prev = GENESIS
        for seq, at, request_id, actor, action, detail, prev_hash, h in self.db.execute(
                "SELECT seq, at, request_id, actor, action, detail, prev_hash, hash FROM audit ORDER BY seq"):
            if prev_hash != prev or _row_hash(prev, at, request_id, actor, action, detail) != h:
                return False, seq
            prev = h
        return True, None
