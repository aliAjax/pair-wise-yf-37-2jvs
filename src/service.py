from datetime import datetime, timezone
from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import RuleEngine, observation_due_date


def _today():
    return datetime.now(timezone.utc).date().isoformat()


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        if kind == "contact":
            # 同一病例下同一人重复登记只保留一条
            duplicate = self._find_contact(payload.get("case_id"), payload.get("person_id"))
            if duplicate:
                return duplicate
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        detail = {"kind": kind}
        if kind == "contact":
            # 病例已确认后补录的接触者直接进入随访名单
            case = self.repository.get_entity(str(payload.get("case_id") or ""))
            if case and case["status"] == "confirmed":
                status = "following"
                payload["followup_start"] = _today()
                payload["due_at"] = observation_due_date(payload.get("exposure_start"))
                detail["auto_followup"] = True
                detail["due_at"] = payload["due_at"]
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, detail)
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        if entity["kind"] == "case" and next_status == "confirmed":
            return self._confirm_case(
                actor, entity, action, expected, next_status, merged, patch
            )
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def _find_contact(self, case_id, person_id):
        if not case_id or not person_id:
            return None
        for contact in self.repository.find_entities("contact", "case_id", case_id):
            if contact["data"].get("person_id") == person_id:
                return contact
        return None

    def _confirm_case(self, actor, case, action, expected, next_status, merged, patch):
        """病例确认：把已登记接触者批量转为待随访，与病例状态同事务提交。"""
        updates = [
            {
                "id": case["id"],
                "expected_version": expected,
                "status": next_status,
                "data": merged,
            }
        ]
        audits = [
            {
                "entity_id": case["id"],
                "actor_id": actor.user_id,
                "actor_role": actor.role,
                "action": action,
                "from_status": case["status"],
                "to_status": next_status,
                "detail": {"patch": patch},
            }
        ]
        seen_persons = set()
        for contact in self.repository.list_entities(kind="contact", status="identified"):
            if contact["data"].get("case_id") != case["id"]:
                continue
            person_id = contact["data"].get("person_id")
            if person_id in seen_persons:
                continue
            seen_persons.add(person_id)
            due_at = observation_due_date(contact["data"].get("exposure_start"))
            data = dict(contact["data"])
            data["followup_start"] = _today()
            data["due_at"] = due_at
            updates.append(
                {
                    "id": contact["id"],
                    "expected_version": contact["version"],
                    "status": "following",
                    "data": data,
                }
            )
            audits.append(
                {
                    "entity_id": contact["id"],
                    "actor_id": actor.user_id,
                    "actor_role": actor.role,
                    "action": "begin_followup",
                    "from_status": "identified",
                    "to_status": "following",
                    "detail": {
                        "auto": True,
                        "trigger": action,
                        "case_id": case["id"],
                        "due_at": due_at,
                    },
                }
            )
        self.repository.apply_bundle(updates, audits)
        return self.repository.get_entity(case["id"])

    def case_followup_summary(self, today=None):
        """每个病例的接触人数、逾期人数和最近观察截止日。"""
        today = today or _today()
        contacts_by_case = {}
        for contact in self.repository.list_entities(kind="contact"):
            contacts_by_case.setdefault(contact["data"].get("case_id"), []).append(contact)
        items = []
        for case in self.repository.list_entities(kind="case"):
            related = contacts_by_case.get(case["id"], [])
            due_dates = [
                contact["data"]["due_at"] for contact in related if contact["data"].get("due_at")
            ]
            overdue = sum(
                1
                for contact in related
                if contact["status"] == "following"
                and contact["data"].get("due_at")
                and contact["data"]["due_at"] < today
            )
            items.append(
                {
                    "case_id": case["id"],
                    "person_id": case["data"].get("person_id"),
                    "status": case["status"],
                    "contact_count": len(related),
                    "overdue_count": overdue,
                    "latest_due_at": max(due_dates) if due_dates else None,
                }
            )
        return {"today": today, "items": items}

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
