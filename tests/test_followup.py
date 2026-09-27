import http.client
import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.domain import Actor, ConflictError
from src.http_api import create_server
from src.repository import SQLiteRepository
from src.rules import RuleEngine, observation_due_date
from src.service import DomainService


def _today():
    return datetime.now(timezone.utc).date().isoformat()


class FollowupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.lab = Actor("lab-1", "lab")

    def tearDown(self):
        self.tmp.cleanup()

    def _case(self, person_id="P-1", onset="2026-03-01"):
        case = self.service.create(
            self.admin,
            "case",
            {
                "person_id": person_id,
                "onset_date": onset,
                "location": "District-A",
                "symptoms": ["fever"],
            },
        )
        self.service.transition(self.admin, case["id"], "triage", {"clinician": "C-1"})
        return case

    def _contact(self, case_id, person_id, exposure_start):
        return self.service.create(
            self.admin,
            "contact",
            {
                "case_id": case_id,
                "person_id": person_id,
                "exposure_start": exposure_start,
            },
        )

    def _confirm(self, case_id, **kwargs):
        return self.service.transition(
            self.lab, case_id, "lab_positive", {"lab_id": "L-1", "result": "positive"}, **kwargs
        )

    def test_observation_due_date_is_fourteen_days_after_exposure(self):
        self.assertEqual(observation_due_date("2026-02-25"), "2026-03-11")
        self.assertEqual(observation_due_date("2026-12-30"), "2027-01-13")

    def test_confirm_converts_registered_contacts_to_following(self):
        case = self._case()
        first = self._contact(case["id"], "P-2", "2026-02-25")
        second = self._contact(case["id"], "P-3", "2026-03-02")
        confirmed = self._confirm(case["id"])
        self.assertEqual(confirmed["status"], "confirmed")
        for contact_id, due in ((first["id"], "2026-03-11"), (second["id"], "2026-03-16")):
            updated = self.service.get(contact_id)
            self.assertEqual(updated["status"], "following")
            self.assertEqual(updated["data"]["due_at"], due)
            self.assertEqual(updated["data"]["followup_start"], _today())
        audit = self.service.audit_log(first["id"])
        self.assertEqual(audit[-1]["action"], "begin_followup")
        self.assertTrue(audit[-1]["detail"]["auto"])

    def test_duplicate_registration_keeps_single_record(self):
        case = self._case()
        first = self._contact(case["id"], "P-2", "2026-02-25")
        duplicate = self._contact(case["id"], "P-2", "2026-02-26")
        self.assertEqual(first["id"], duplicate["id"])
        self.assertEqual(len(self.service.list("contact")), 1)

    def test_late_registration_after_confirmation_enters_list(self):
        case = self._case()
        self._confirm(case["id"])
        late = self._contact(case["id"], "P-4", "2026-03-03")
        self.assertEqual(late["status"], "following")
        self.assertEqual(late["data"]["due_at"], "2026-03-17")
        self.assertEqual(late["data"]["followup_start"], _today())

    def test_cascade_dedupes_preexisting_duplicate_contacts(self):
        case = self._case()
        contact = self._contact(case["id"], "P-2", "2026-02-25")
        # 模拟历史遗留的重复登记（直接写库，绕过服务层去重）
        self.repo.create_entity(
            "legacy-dup",
            "contact",
            "identified",
            {"case_id": case["id"], "person_id": "P-2", "exposure_start": "2026-02-26"},
            "admin",
        )
        self._confirm(case["id"])
        self.assertEqual(self.service.get(contact["id"])["status"], "following")
        self.assertEqual(self.service.get("legacy-dup")["status"], "identified")

    def test_bundle_rolls_back_when_a_step_fails(self):
        case = self._case()
        contact = self._contact(case["id"], "P-2", "2026-02-25")
        updates = [
            {
                "id": case["id"],
                "expected_version": case["version"],
                "status": "confirmed",
                "data": dict(case["data"]),
            },
            {
                "id": contact["id"],
                "expected_version": 999,
                "status": "following",
                "data": dict(contact["data"]),
            },
        ]
        audits = [
            {
                "entity_id": case["id"],
                "actor_id": "admin",
                "actor_role": "admin",
                "action": "lab_positive",
                "from_status": "investigating",
                "to_status": "confirmed",
                "detail": {},
            }
        ]
        with self.assertRaises(ConflictError):
            self.repo.apply_bundle(updates, audits)
        self.assertEqual(self.service.get(case["id"])["status"], "investigating")
        self.assertEqual(self.service.get(contact["id"])["status"], "identified")
        actions = [entry["action"] for entry in self.service.audit_log()]
        self.assertNotIn("lab_positive", actions)

    def test_confirm_with_stale_version_changes_nothing(self):
        case = self._case()
        contact = self._contact(case["id"], "P-2", "2026-02-25")
        with self.assertRaises(ConflictError):
            self._confirm(case["id"], expected_version=999)
        self.assertEqual(self.service.get(case["id"])["status"], "investigating")
        self.assertEqual(self.service.get(contact["id"])["status"], "identified")

    def test_summary_counts_overdue_and_latest_due(self):
        today = _today()
        case = self._case()
        self._contact(case["id"], "P-2", "2020-01-01")  # 确认后仍待随访 -> 逾期
        done = self._contact(case["id"], "P-3", "2020-01-02")  # 已完成 -> 不算逾期
        self._contact(case["id"], "P-4", today)  # 观察期内 -> 不逾期
        self._confirm(case["id"])
        self.service.transition(
            self.admin, done["id"], "complete_followup", {"outcome": "no symptoms"}
        )
        empty = self._case(person_id="P-9", onset="2026-04-01")
        summary = self.service.case_followup_summary(today=today)
        items = {item["case_id"]: item for item in summary["items"]}
        row = items[case["id"]]
        self.assertEqual(row["contact_count"], 3)
        self.assertEqual(row["overdue_count"], 1)
        self.assertEqual(row["latest_due_at"], observation_due_date(today))
        empty_row = items[empty["id"]]
        self.assertEqual(empty_row["contact_count"], 0)
        self.assertEqual(empty_row["overdue_count"], 0)
        self.assertIsNone(empty_row["latest_due_at"])


class SummaryApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.server = create_server("127.0.0.1", 0, self.service, RuleEngine(), ".")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def test_summary_endpoint(self):
        admin = Actor("admin", "admin")
        case = self.service.create(
            admin,
            "case",
            {
                "person_id": "P-1",
                "onset_date": "2026-03-01",
                "location": "District-A",
                "symptoms": ["fever"],
            },
        )
        self.service.transition(admin, case["id"], "triage", {"clinician": "C-1"})
        self.service.create(
            admin,
            "contact",
            {"case_id": case["id"], "person_id": "P-2", "exposure_start": "2026-02-25"},
        )
        self.service.transition(
            Actor("lab-1", "lab"),
            case["id"],
            "lab_positive",
            {"lab_id": "L-1", "result": "positive"},
        )
        connection = http.client.HTTPConnection("127.0.0.1", self.port)
        connection.request("GET", "/api/cases/summary")
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 200)
        row = payload["items"][0]
        self.assertEqual(row["case_id"], case["id"])
        self.assertEqual(row["contact_count"], 1)
        self.assertEqual(row["overdue_count"], 1)
        self.assertEqual(row["latest_due_at"], "2026-03-11")


if __name__ == "__main__":
    unittest.main()
