from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import ID_PREFIX, STATES, WITHDRAW_TARGET


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS closure_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    closure_version INTEGER NOT NULL,
                    basis TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, closure_version)
                );
                CREATE TABLE IF NOT EXISTS withdrawals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    revision INTEGER NOT NULL,
                    closure_version INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    planned_items TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','applied','reviewed')),
                    previous_version INTEGER,
                    reopened_record_ids TEXT NOT NULL DEFAULT '[]',
                    applicant TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    reviewed_by TEXT,
                    reviewed_at TEXT,
                    UNIQUE(item_id, revision)
                );
                CREATE TABLE IF NOT EXISTS basis_revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    revision INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    basis TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, revision)
                );
            """)

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def save_closure_snapshot(self, item_id: int, closure_version: int,
                              basis: dict, actor: str) -> None:
        now = utc_now()
        with self._lock, self.conn:
            self.conn.execute(
                """INSERT INTO closure_snapshots(item_id, closure_version, basis,
                   created_by, created_at) VALUES(?,?,?,?,?)""",
                (item_id, closure_version,
                 json.dumps(basis, ensure_ascii=False, sort_keys=True), actor, now),
            )

    @staticmethod
    def _snapshot(row: sqlite3.Row) -> Dict[str, Any]:
        result = dict(row)
        result["basis"] = json.loads(result["basis"])
        return result

    def get_closure_snapshot(self, item_id: int,
                             closure_version: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM closure_snapshots WHERE item_id=? AND closure_version=?",
                (item_id, closure_version),
            ).fetchone()
        return self._snapshot(row) if row else None

    def list_closure_snapshots(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM closure_snapshots WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [self._snapshot(row) for row in rows]

    def _reopen_records(self, item_id: int, record_ids: List[int],
                        status: str) -> None:
        for record_id in record_ids:
            self.conn.execute(
                "UPDATE records SET status=? WHERE id=? AND item_id=?",
                (status, record_id, item_id),
            )

    def apply_withdrawal(self, item_id: int, expected_version: int,
                         closure_version: int, reason: str, planned_items: str,
                         applicant: str, reopen_ids: List[int]) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=? AND status='closed'""",
                (WITHDRAW_TARGET, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                row = self.conn.execute(
                    "SELECT version, status FROM items WHERE id=?", (item_id,)
                ).fetchone()
                if row is None:
                    raise NotFoundError("项目不存在")
                if row["status"] != "closed":
                    raise ConflictError(
                        f"仅结案事件可撤回结案，当前版本为{row['version']}")
                raise ConflictError(f"版本冲突，当前版本为{row['version']}")
            row = self.conn.execute(
                "SELECT COALESCE(MAX(revision),0)+1 AS rev FROM withdrawals WHERE item_id=?",
                (item_id,),
            ).fetchone()
            revision = int(row["rev"])
            cur = self.conn.execute(
                """INSERT INTO withdrawals(item_id, revision, closure_version, reason,
                   planned_items, status, previous_version, reopened_record_ids,
                   applicant, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (item_id, revision, closure_version, reason, planned_items, "pending",
                 expected_version, json.dumps(list(reopen_ids)), applicant, now, now),
            )
            application_id = int(cur.lastrowid)
            self._reopen_records(item_id, reopen_ids, "open")
        return self.get_withdrawal(item_id, application_id)

    def reapply_withdrawal(self, item_id: int, application_id: int,
                           reopen_ids: List[int]) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT version, status FROM items WHERE id=?", (item_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("项目不存在")
            if row["status"] != "closed":
                raise ConflictError("剂量事件未处于结案状态，不能重试")
            self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=? AND status='closed'""",
                (WITHDRAW_TARGET, now, item_id, row["version"]),
            )
            cur = self.conn.execute(
                """UPDATE withdrawals SET previous_version=?, reopened_record_ids=?,
                   updated_at=? WHERE id=? AND item_id=? AND status='pending'""",
                (row["version"], json.dumps(list(reopen_ids)), now,
                 application_id, item_id),
            )
            if cur.rowcount == 0:
                raise ConflictError("撤回申请状态已变化，请刷新后重试")
            self._reopen_records(item_id, reopen_ids, "open")
        return self.get_withdrawal(item_id, application_id)

    def restore_closure(self, item_id: int, application_id: int) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM withdrawals WHERE id=? AND item_id=?",
                (application_id, item_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("撤回申请不存在")
            self.conn.execute(
                "UPDATE items SET status='closed', version=?, updated_at=? WHERE id=?",
                (row["previous_version"], now, item_id),
            )
            self._reopen_records(item_id, json.loads(row["reopened_record_ids"]),
                                 "closed")
        return self.get_item(item_id)

    @staticmethod
    def _withdrawal(row: sqlite3.Row) -> Dict[str, Any]:
        result = dict(row)
        result["reopened_record_ids"] = json.loads(result["reopened_record_ids"])
        return result

    def get_withdrawal(self, item_id: int, application_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM withdrawals WHERE id=? AND item_id=?",
                (application_id, item_id),
            ).fetchone()
        if row is None:
            raise NotFoundError("撤回申请不存在")
        return self._withdrawal(row)

    def list_withdrawals(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM withdrawals WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [self._withdrawal(row) for row in rows]

    def update_withdrawal_status(self, item_id: int, application_id: int,
                                 status: str, actor: Optional[str] = None
                                 ) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            if status == "reviewed":
                cur = self.conn.execute(
                    """UPDATE withdrawals SET status=?, reviewed_by=?, reviewed_at=?,
                       updated_at=? WHERE id=? AND item_id=? AND status='applied'""",
                    (status, actor, now, now, application_id, item_id),
                )
            else:
                cur = self.conn.execute(
                    """UPDATE withdrawals SET status=?, updated_at=?
                       WHERE id=? AND item_id=? AND status='pending'""",
                    (status, now, application_id, item_id),
                )
            if cur.rowcount == 0:
                raise ConflictError("撤回申请状态已变化，请刷新后重试")
        return self.get_withdrawal(item_id, application_id)

    def correct_dose(self, item_id: int, expected_version: int, severity: str,
                     quantity: float, threshold: float, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET severity=?, quantity=?, threshold=?,
                   version=version+1, updated_at=? WHERE id=? AND version=?""",
                (severity, quantity, threshold, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                row = self.conn.execute(
                    "SELECT version FROM items WHERE id=?", (item_id,)
                ).fetchone()
                if row is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError(f"版本冲突，当前版本为{row['version']}")
        return self.get_item(item_id)

    def save_basis_revision(self, item_id: int, source: str, basis: dict,
                            actor: str) -> int:
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT COALESCE(MAX(revision),0)+1 AS rev FROM basis_revisions WHERE item_id=?",
                (item_id,),
            ).fetchone()
            revision = int(row["rev"])
            self.conn.execute(
                """INSERT INTO basis_revisions(item_id, revision, source, basis,
                   created_by, created_at) VALUES(?,?,?,?,?,?)""",
                (item_id, revision, source,
                 json.dumps(basis, ensure_ascii=False, sort_keys=True), actor, now),
            )
        return revision

    def list_basis_revisions(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM basis_revisions WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        result = []
        for row in rows:
            entry = dict(row)
            entry["basis"] = json.loads(entry["basis"])
            result.append(entry)
        return result

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    def close(self) -> None:
        with self._lock:
            self.conn.close()
