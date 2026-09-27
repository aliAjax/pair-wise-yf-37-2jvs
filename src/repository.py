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
            connection.executescript("""
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
                CREATE TABLE IF NOT EXISTS contact_registry (
                    case_id TEXT NOT NULL,
                    person_id TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(case_id, person_id)
                );
            """)

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

    def create_entity(self, entity_id, kind, status, data, actor_id):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (entity_id, kind, status, payload, actor_id, now, now),
            )
        return self.get_entity(entity_id)

    def get_entity(self, entity_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
        return self._entity_from_row(row) if row else None

    def create_contact_atomic(self, entity_id, data, actor_id, decide):
        """在单个事务里登记接触者。

        - 同一病例下同一人已登记过：直接返回 (已有实体, False)，只保留一条。
        - 否则在事务内读取关联病例，调用 decide(case, data) 得到
          (初始状态, 补充字段)，再插入实体和去重登记，返回 (新实体, True)。
        """
        now = utcnow()
        case_id = data.get("case_id")
        person_id = data.get("person_id")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT entity_id FROM contact_registry WHERE case_id = ? AND person_id = ?",
                (case_id, person_id),
            ).fetchone()
            if row:
                connection.commit()
                return self.get_entity(row["entity_id"]), False
            case_row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (case_id,)
            ).fetchone()
            case = self._entity_from_row(case_row) if case_row else None
            status, extra = decide(case, data)
            merged = dict(data)
            merged.update(extra or {})
            payload = json.dumps(merged, ensure_ascii=False, sort_keys=True)
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, 'contact', ?, 1, ?, ?, ?, ?)",
                (entity_id, status, payload, actor_id, now, now),
            )
            connection.execute(
                "INSERT INTO contact_registry(case_id, person_id, entity_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (case_id, person_id, entity_id, now),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id), True

    def confirm_case_with_followups(self, case_id, expected_version, plan):
        """在单个事务里确认病例并把接触者批量转入随访。

        事务内读取病例和全部接触者后调用 plan(case, contacts)，返回：
        {"case": {"status", "data"},
         "contacts": [{"id", "expected_version", "status", "data"}, ...],
         "audits": [{"entity_id", "actor_id", "actor_role", "action",
                     "from_status", "to_status", "detail"}, ...]}
        任何一步失败都会整体回滚，病例状态和随访名单不会只成功一半。
        """
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (case_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + case_id)
            case = self._entity_from_row(row)
            if expected_version is not None and case["version"] != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, case["version"])
                )
            rows = connection.execute(
                "SELECT * FROM entities WHERE kind = 'contact' ORDER BY created_at, id"
            ).fetchall()
            contacts = [self._entity_from_row(row) for row in rows]
            result = plan(case, contacts)
            now = utcnow()
            case_update = result["case"]
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (
                    case_update["status"],
                    json.dumps(case_update["data"], ensure_ascii=False, sort_keys=True),
                    now,
                    case_id,
                    case["version"],
                ),
            )
            for update in result["contacts"]:
                cursor = connection.execute(
                    "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                    "WHERE id = ? AND version = ?",
                    (
                        update["status"],
                        json.dumps(update["data"], ensure_ascii=False, sort_keys=True),
                        now,
                        update["id"],
                        update["expected_version"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise ConflictError("version conflict for entity: " + update["id"])
            for audit in result["audits"]:
                connection.execute(
                    "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        audit["entity_id"],
                        audit["actor_id"],
                        audit["actor_role"],
                        audit["action"],
                        audit["from_status"],
                        audit["to_status"],
                        json.dumps(audit["detail"], ensure_ascii=False, sort_keys=True),
                        utcnow(),
                    ),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(case_id)

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
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
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
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (status, payload, now, entity_id, current_version),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id)

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
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
                    "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id", (entity_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
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
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (actor_id, idem_key, entity_id, utcnow()),
            )

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
