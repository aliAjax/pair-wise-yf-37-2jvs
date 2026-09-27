from datetime import datetime, timezone
from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import RuleEngine, observation_due_date


def _parse_date(value):
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except (TypeError, ValueError):
        return None


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
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        if kind == "contact":
            # 同一病例下同一人只保留一条；补录时若病例已确认则直接进入随访名单。
            entity, created = self.repository.create_contact_atomic(
                entity_id, payload, actor.user_id, self._decide_contact_initial
            )
            if created:
                self.audit.record(entity_id, actor, "create", None, entity["status"], {"kind": kind})
            if idempotency_key:
                self.repository.save_idempotency(actor.user_id, idempotency_key, entity["id"])
            return entity
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def _decide_contact_initial(self, case, data):
        """登记接触者时决定初始状态：病例已确认则直接转入待随访。"""
        if case and case["status"] == "confirmed":
            today = datetime.now(timezone.utc).date().isoformat()
            return "following", {
                "followup_start": today,
                "due_at": observation_due_date(data.get("exposure_start")),
            }
        return self.rules.initial_status("contact"), {}

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        if entity["kind"] == "case" and action == "lab_positive":
            return self._confirm_case(actor, entity, expected, next_status, patch)
        merged = dict(entity["data"])
        merged.update(patch)
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

    def _confirm_case(self, actor, case, expected_version, next_status, patch):
        """病例确认：同事务内把已登记接触者转成待随访并给出14天观察截止日。"""
        today = datetime.now(timezone.utc).date().isoformat()

        def plan(current_case, contacts):
            merged = dict(current_case["data"])
            merged.update(patch)
            contact_updates = []
            audits = [
                {
                    "entity_id": current_case["id"],
                    "actor_id": actor.user_id,
                    "actor_role": actor.role,
                    "action": "lab_positive",
                    "from_status": current_case["status"],
                    "to_status": next_status,
                    "detail": {"patch": patch},
                }
            ]
            seen_persons = set()
            for contact in contacts:
                if contact["data"].get("case_id") != current_case["id"]:
                    continue
                if contact["status"] != "identified":
                    continue
                person_id = contact["data"].get("person_id")
                if person_id in seen_persons:
                    continue
                seen_persons.add(person_id)
                followup_data = dict(contact["data"])
                followup_data["followup_start"] = today
                followup_data["due_at"] = observation_due_date(
                    contact["data"].get("exposure_start")
                )
                contact_updates.append(
                    {
                        "id": contact["id"],
                        "expected_version": contact["version"],
                        "status": "following",
                        "data": followup_data,
                    }
                )
                audits.append(
                    {
                        "entity_id": contact["id"],
                        "actor_id": actor.user_id,
                        "actor_role": actor.role,
                        "action": "auto_followup",
                        "from_status": "identified",
                        "to_status": "following",
                        "detail": {
                            "case_id": current_case["id"],
                            "due_at": followup_data["due_at"],
                        },
                    }
                )
            return {
                "case": {"status": next_status, "data": merged},
                "contacts": contact_updates,
                "audits": audits,
            }

        return self.repository.confirm_case_with_followups(
            case["id"], expected_version, plan
        )

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def case_summary(self, today=None):
        """每个病例的接触人数、逾期人数和最近观察截止日。"""
        today = today or datetime.now(timezone.utc).date()
        cases = self.repository.list_entities(kind="case")
        contacts = self.repository.list_entities(kind="contact")
        by_case = {}
        for contact in contacts:
            by_case.setdefault(contact["data"].get("case_id"), []).append(contact)
        items = []
        for case in cases:
            related = by_case.get(case["id"], [])
            due_dates = []
            overdue = 0
            for contact in related:
                due = _parse_date(contact["data"].get("due_at"))
                if due is None:
                    continue
                due_dates.append(due)
                if contact["status"] == "following" and due < today:
                    overdue += 1
            items.append(
                {
                    "case_id": case["id"],
                    "person_id": case["data"].get("person_id"),
                    "status": case["status"],
                    "contact_count": len(related),
                    "overdue_count": overdue,
                    "latest_due_at": max(due_dates).isoformat() if due_dates else None,
                }
            )
        return items

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
