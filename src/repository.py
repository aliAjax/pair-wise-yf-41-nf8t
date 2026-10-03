import json
import sqlite3
from datetime import datetime, timezone

from .domain import ConflictError, NotFoundError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entities_kind_status
                    ON entities(kind, status);
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_entity
                    ON audit_log(entity_id, id);
                CREATE TABLE IF NOT EXISTS idempotency (
                    actor_id TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, idem_key)
                );
                -- 写入失败后未完成的报告：保留待重试，重试按 ref 幂等。
                CREATE TABLE IF NOT EXISTS pending_reports (
                    ref TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL,
                    station_code TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_pending_event
                    ON pending_reports(event_id);
            """
            )

    @staticmethod
    def _entity_from_row(row):
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "version": int(row["version"]),
            "data": json.loads(row["data"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    # ---- 通用实体 -------------------------------------------------------

    def create_entity(self, entity_id, kind, status, data, actor_id):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, "
                "created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (entity_id, kind, status, payload, actor_id, now, now),
            )
        return self.get_entity(entity_id)

    def get_entity(self, entity_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
        return self._entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
            ).fetchall()
        return [self._entity_from_row(row) for row in rows]

    def find_entities(self, kind, field, value):
        return [
            entity
            for entity in self.list_entities(kind=kind)
            if (
                entity["id"] == value
                if field == "id"
                else entity["data"].get(field) == value
            )
        ]

    def update_entity(self, entity_id, expected_version, status, data):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + entity_id)
            current_version = int(row["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )
            cursor = connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, "
                "updated_at = ? WHERE id = ? AND version = ?",
                (status, payload, now, entity_id, current_version),
            )
            if cursor.rowcount != 1:
                raise ConflictError("entity was modified concurrently: " + entity_id)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id)

    # ---- 事件 + 台站报告联动（单事务）-----------------------------------

    def create_event_with_reports(self, event_id, event_status, event_data, reports,
                                  actor_id):
        """创建事件，并在同一事务内为初始报告集合建立台站报告实体。

        reports: [(report_id, report_data), ...]
        """
        now = utcnow()
        event_payload = json.dumps(event_data, ensure_ascii=False, sort_keys=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, "
                "created_at, updated_at) VALUES (?, 'event', ?, 1, ?, ?, ?, ?)",
                (event_id, event_status, event_payload, actor_id, now, now),
            )
            for report_id, report_data in reports:
                report_payload = json.dumps(
                    report_data, ensure_ascii=False, sort_keys=True
                )
                report_status = "superseded" if report_data.get("supersedes") else \
                    "received"
                connection.execute(
                    "INSERT INTO entities(id, kind, status, version, data, created_by, "
                    "created_at, updated_at) VALUES (?, 'report', ?, 1, ?, "
                    "?, ?, ?)",
                    (report_id, report_status, report_payload, actor_id, now, now),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(event_id)

    def apply_pending_report(self, pending, expected_event_version, prepare):
        """把一条补报合并进事件（与台站报告实体同事务）。

        事件按乐观锁更新；同一份依据（最新报告集合）之外的并发补报直接冲突失败，
        待重试项保留在 pending_reports。prepare 是规则计算回调，参数为
        (connection, event)，返回字典：
          status, data, report_id, report_data, superseded_ids
        """
        now = utcnow()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?",
                (pending["event_id"],),
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + pending["event_id"])
            current_version = int(row["version"])
            if expected_event_version is not None and current_version != int(
                expected_event_version
            ):
                raise ConflictError(
                    "version conflict: event %s expected %s, found %s; only the "
                    "latest report set is accepted"
                    % (pending["event_id"], expected_event_version, current_version)
                )
            event = self._entity_from_row(row)
            result = prepare(connection, event)

            event_payload = json.dumps(
                result["data"], ensure_ascii=False, sort_keys=True
            )
            cursor = connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, "
                "updated_at = ? WHERE id = ? AND version = ?",
                (
                    result["status"],
                    event_payload,
                    now,
                    event["id"],
                    current_version,
                ),
            )
            if cursor.rowcount != 1:
                raise ConflictError("event was modified concurrently: " + event["id"])

            report_id = result["report_id"]
            report_payload = json.dumps(
                result["report_data"], ensure_ascii=False, sort_keys=True
            )
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, "
                "created_at, updated_at) VALUES (?, 'report', 'received', 1, ?, "
                "?, ?, ?)",
                (
                    report_id,
                    report_payload,
                    pending["actor_id"],
                    now,
                    now,
                ),
            )
            # 同台站旧版报告退出"采用集合"，实体留痕可查。
            for old_id in result.get("superseded_ids", []):
                cursor = connection.execute(
                    "UPDATE entities SET status = 'superseded', "
                    "updated_at = ? WHERE id = ? AND kind = 'report'",
                    (now, old_id),
                )
                if cursor.rowcount != 1:
                    raise NotFoundError("report entity missing: " + old_id)

            # 补报正式采用后，待重试项才清除；失败则整个事务回滚、保留待重试。
            connection.execute("DELETE FROM pending_reports WHERE ref = ?",
                               (pending["ref"],))
            connection.commit()
        except Exception as exc:
            connection.rollback()
            # 未完成的报告保留待重试（在独立连接里记账，避免污染已回滚的事务）。
            self.touch_pending(pending["ref"], attempt=True, error=str(exc))
            raise
        finally:
            connection.close()
        return self.get_entity(pending["event_id"])

    # ---- 待重试报告 -----------------------------------------------------

    def save_pending_report(self, pending):
        now = utcnow()
        payload = json.dumps(pending["payload"], ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO pending_reports(ref, event_id, station_code, "
                "actor_id, payload, attempts, last_error, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 0, NULL, ?, ?)",
                (
                    pending["ref"],
                    pending["event_id"],
                    pending["station_code"],
                    pending["actor_id"],
                    payload,
                    now,
                    now,
                ),
            )
        return self.get_pending_report(pending["ref"])

    def get_pending_report(self, ref):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM pending_reports WHERE ref = ?", (ref,)
            ).fetchone()
        return self._pending_from_row(row) if row else None

    def list_pending_reports(self, event_id=None, actor_id=None):
        clauses = []
        params = []
        if event_id:
            clauses.append("event_id = ?")
            params.append(event_id)
        if actor_id:
            clauses.append("actor_id = ?")
            params.append(actor_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM pending_reports" + where +
                " ORDER BY created_at, ref",
                params,
            ).fetchall()
        return [self._pending_from_row(row) for row in rows]

    def touch_pending(self, ref, attempt=False, error=None):
        with self._connect() as connection:
            if attempt:
                connection.execute(
                    "UPDATE pending_reports SET attempts = attempts + 1, "
                    "last_error = ?, updated_at = ? WHERE ref = ?",
                    (error, utcnow(), ref),
                )
            else:
                connection.execute(
                    "UPDATE pending_reports SET last_error = ?, updated_at = ? "
                    "WHERE ref = ?",
                    (error, utcnow(), ref),
                )

    @staticmethod
    def _pending_from_row(row):
        return {
            "ref": row["ref"],
            "event_id": row["event_id"],
            "station_code": row["station_code"],
            "actor_id": row["actor_id"],
            "payload": json.loads(row["payload"]),
            "attempts": int(row["attempts"]),
            "last_error": row["last_error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    # ---- 审计 / 幂等 ----------------------------------------------------

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status,
                     to_status, detail):
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, "
                "from_status, to_status, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entity_id,
                    actor_id,
                    actor_role,
                    action,
                    from_status,
                    to_status,
                    json.dumps(detail, ensure_ascii=False, sort_keys=True),
                    utcnow(),
                ),
            )

    def list_audit(self, entity_id=None):
        with self._connect() as connection:
            if entity_id:
                rows = connection.execute(
                    "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id",
                    (entity_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM audit_log ORDER BY id"
                ).fetchall()
        return [
            {
                "id": row["id"],
                "entity_id": row["entity_id"],
                "actor_id": row["actor_id"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_idempotency(self, actor_id, idem_key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT entity_id FROM idempotency WHERE actor_id = ? AND idem_key = ?",
                (actor_id, idem_key),
            ).fetchone()
        return row["entity_id"] if row else None

    def save_idempotency(self, actor_id, idem_key, entity_id):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, "
                "created_at) VALUES (?, ?, ?, ?)",
                (actor_id, idem_key, entity_id, utcnow()),
            )

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
