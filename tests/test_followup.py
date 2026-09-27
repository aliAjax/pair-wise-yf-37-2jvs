import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from src.domain import Actor, ConflictError, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class FollowupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def _case(self, person_id="P-1"):
        return self.service.create(
            self.admin,
            "case",
            {
                "person_id": person_id,
                "onset_date": "2026-03-01",
                "location": "District-A",
                "symptoms": ["fever"],
            },
        )

    def _investigating_case(self):
        case = self._case()
        self.service.transition(self.admin, case["id"], "triage", {"clinician": "C-1"})
        return self.service.get(case["id"])

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

    def _confirm(self, case):
        return self.service.transition(
            self.admin,
            case["id"],
            "lab_positive",
            {"lab_id": "L-1", "result": "positive"},
        )

    def test_confirm_converts_contacts_with_14_day_due(self):
        case = self._investigating_case()
        first = self._contact(case["id"], "P-2", "2026-03-01")
        second = self._contact(case["id"], "P-3", "2026-03-05")

        confirmed = self._confirm(case)

        self.assertEqual(confirmed["status"], "confirmed")
        for contact_id, expected_due in (
            (first["id"], "2026-03-15"),
            (second["id"], "2026-03-19"),
        ):
            contact = self.service.get(contact_id)
            self.assertEqual(contact["status"], "following")
            self.assertEqual(contact["data"]["due_at"], expected_due)
            self.assertTrue(contact["data"].get("followup_start"))

    def test_duplicate_contact_keeps_single_record(self):
        case = self._investigating_case()
        first = self._contact(case["id"], "P-2", "2026-03-01")
        second = self._contact(case["id"], "P-2", "2026-03-02")

        self.assertEqual(first["id"], second["id"])
        contacts = [
            item
            for item in self.service.list("contact")
            if item["data"]["case_id"] == case["id"]
        ]
        self.assertEqual(len(contacts), 1)

    def test_late_registered_contact_enters_followup_list(self):
        case = self._investigating_case()
        self._confirm(case)

        late = self._contact(case["id"], "P-9", "2026-03-02")

        self.assertEqual(late["status"], "following")
        self.assertEqual(late["data"]["due_at"], "2026-03-16")

    def test_failed_confirmation_rolls_back_everything(self):
        case = self._investigating_case()
        good = self._contact(case["id"], "P-2", "2026-03-01")
        # 直接写库造一个接触开始日非法的存量接触者，让级联计算中途失败。
        self.repo.create_entity(
            "bad-contact",
            "contact",
            "identified",
            {"case_id": case["id"], "person_id": "P-BAD", "exposure_start": "not-a-date"},
            "seed",
        )

        with self.assertRaises(ValidationError):
            self._confirm(case)

        self.assertEqual(self.service.get(case["id"])["status"], "investigating")
        self.assertEqual(self.service.get(good["id"])["status"], "identified")

    def test_transaction_rolls_back_on_contact_conflict(self):
        case = self._investigating_case()
        contact = self._contact(case["id"], "P-2", "2026-03-01")

        def plan(current_case, contacts):
            return {
                "case": {"status": "confirmed", "data": current_case["data"]},
                "contacts": [
                    {
                        "id": contact["id"],
                        "expected_version": 999,
                        "status": "following",
                        "data": contact["data"],
                    }
                ],
                "audits": [],
            }

        with self.assertRaises(ConflictError):
            self.repo.confirm_case_with_followups(case["id"], case["version"], plan)

        self.assertEqual(self.service.get(case["id"])["status"], "investigating")
        self.assertEqual(self.service.get(contact["id"])["status"], "identified")

    def test_case_summary_counts_and_due_dates(self):
        today = date.today()
        old_exposure = (today - timedelta(days=30)).isoformat()
        recent_exposure = (today - timedelta(days=3)).isoformat()
        recent_due = (today + timedelta(days=11)).isoformat()
        case = self._investigating_case()
        overdue_contact = self._contact(case["id"], "P-2", old_exposure)
        self._contact(case["id"], "P-3", recent_exposure)
        done = self._contact(case["id"], "P-4", old_exposure)
        self._confirm(case)
        self.service.transition(
            self.admin, done["id"], "complete_followup", {"outcome": "no symptoms"}
        )

        summary = self.service.case_summary(today=today)

        self.assertEqual(len(summary), 1)
        item = summary[0]
        self.assertEqual(item["case_id"], case["id"])
        self.assertEqual(item["contact_count"], 3)
        self.assertEqual(item["overdue_count"], 1)
        self.assertEqual(item["latest_due_at"], recent_due)
        self.assertEqual(
            self.service.get(overdue_contact["id"])["status"], "following"
        )

    def test_confirmation_writes_audit_for_case_and_contacts(self):
        case = self._investigating_case()
        contact = self._contact(case["id"], "P-2", "2026-03-01")
        self._confirm(case)

        actions = {
            (row["entity_id"], row["action"]) for row in self.service.audit_log()
        }
        self.assertIn((case["id"], "lab_positive"), actions)
        self.assertIn((contact["id"], "auto_followup"), actions)


if __name__ == "__main__":
    unittest.main()
