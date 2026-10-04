import threading
import tempfile
import unittest
from pathlib import Path

from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.rules import STATES, TRANSITION_ROLES
from src.service import Service


class WithdrawalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item(
            {"title": "withdrawal item", "description": "withdrawal scenarios",
             "severity": "elevated", "quantity": 5, "threshold": 10,
             "external_ref": "WD-1"}, "creator", "dosimetrist")
        self.service.add_record(self.item["id"],
                                {"kind": "evidence", "detail": "first evidence",
                                 "status": "closed", "external_ref": "WD-EV-1"},
                                "recorder", "radiation_officer")
        self.service.add_record(self.item["id"],
                                {"kind": "evidence", "detail": "second evidence",
                                 "status": "closed", "external_ref": "WD-EV-2"},
                                "recorder", "health_physicist")
        current = self.item
        for target in STATES[1:]:
            current = self.service.transition(
                current["id"], target, current["version"], "reviewer",
                TRANSITION_ROLES[target][0])
        self.closed = current

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _closure_version(self):
        snapshots = self.service.list_closure_snapshots(self.item["id"], "viewer")
        self.assertEqual(len(snapshots), 1)
        return snapshots[0]["closure_version"]

    def _withdraw(self, actor="officer1"):
        item = self.service.get_item(self.item["id"], "viewer")
        return self.service.request_withdrawal(
            self.item["id"],
            {"closure_version": self._closure_version(), "reason": "值班员误点结案",
             "planned_items": "补录随访记录并更正剂量",
             "expected_version": item["version"]}, actor, "radiation_officer")

    def test_withdrawal_flow_snapshot_reopen_and_review(self):
        self.assertEqual(self.closed["status"], "closed")
        closure_version = self._closure_version()
        application = self._withdraw()
        self.assertEqual(application["status"], "applied")
        self.assertEqual(application["revision"], 1)
        self.assertEqual(application["closure_version"], closure_version)
        item = self.service.get_item(self.item["id"], "viewer")
        self.assertEqual(item["status"], "follow_up")
        self.assertEqual(item["version"], self.closed["version"] + 1)
        records = self.service.list_records(self.item["id"], "viewer")
        self.assertEqual([r["status"] for r in records], ["open", "open"])
        self.assertEqual(item["open_records"], 2)
        snapshots = self.service.list_closure_snapshots(self.item["id"], "viewer")
        self.assertEqual(snapshots[0]["closure_version"], closure_version)
        self.assertEqual(snapshots[0]["basis"]["closed_record_ids"],
                         [r["id"] for r in records])
        with self.assertRaises(PermissionDenied):
            self.service.review_withdrawal(self.item["id"], application["id"], {},
                                           "attacker", "radiation_officer")
        with self.assertRaises(PermissionDenied):
            self.service.review_withdrawal(self.item["id"], application["id"], {},
                                           "attacker", "viewer")
        reviewed = self.service.review_withdrawal(
            self.item["id"], application["id"], {"comment": "同意撤回"},
            "physicist", "health_physicist")
        self.assertEqual(reviewed["status"], "reviewed")
        self.assertEqual(reviewed["reviewed_by"], "physicist")
        revisions = self.service.list_basis_revisions(self.item["id"], "viewer")
        self.assertEqual([r["source"] for r in revisions],
                         ["closure", "withdrawal_review"])
        self.assertEqual(revisions[-1]["basis"]["open_records"], 2)
        with self.assertRaises(ConflictError):
            self.service.review_withdrawal(self.item["id"], application["id"], {},
                                           "physicist", "health_physicist")
        self.assertTrue(self.repo.verify_audit_chain())

    def test_concurrent_withdrawal_first_wins_revision(self):
        closure_version = self._closure_version()
        version = self.closed["version"]
        results = {}

        def submit(name):
            try:
                results[name] = ("ok", self.service.request_withdrawal(
                    self.item["id"],
                    {"closure_version": closure_version, "reason": "误点结案",
                     "planned_items": "补录随访", "expected_version": version},
                    name, "radiation_officer"))
            except ConflictError as exc:
                results[name] = ("conflict", str(exc))

        threads = [threading.Thread(target=submit, args=(f"officer{i}",))
                   for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        outcomes = sorted(value[0] for value in results.values())
        self.assertEqual(outcomes, ["conflict", "ok"])
        conflict = [value[1] for value in results.values()
                    if value[0] == "conflict"][0]
        self.assertIn(str(version + 1), conflict)
        applied = [value[1] for value in results.values() if value[0] == "ok"][0]
        self.assertEqual(applied["revision"], 1)
        self.assertEqual(len(self.service.list_withdrawals(self.item["id"],
                                                           "viewer")), 1)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_audit_failure_restores_snapshot_and_retry_keeps_application(self):
        original = self.repo.append_audit
        calls = {"n": 0}

        def fail_once(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("audit store unavailable")
            return original(*args, **kwargs)

        self.repo.append_audit = fail_once
        with self.assertRaises(RuntimeError):
            self._withdraw()
        self.repo.append_audit = original
        item = self.service.get_item(self.item["id"], "viewer")
        self.assertEqual(item["status"], "closed")
        self.assertEqual(item["version"], self.closed["version"])
        records = self.service.list_records(self.item["id"], "viewer")
        self.assertEqual([r["status"] for r in records], ["closed", "closed"])
        applications = self.service.list_withdrawals(self.item["id"], "viewer")
        self.assertEqual(len(applications), 1)
        self.assertEqual(applications[0]["status"], "pending")
        applied = self.service.retry_withdrawal(
            self.item["id"], applications[0]["id"], "officer1",
            "radiation_officer")
        self.assertEqual(applied["id"], applications[0]["id"])
        self.assertEqual(applied["revision"], applications[0]["revision"])
        self.assertEqual(applied["status"], "applied")
        item = self.service.get_item(self.item["id"], "viewer")
        self.assertEqual(item["status"], "follow_up")
        records = self.service.list_records(self.item["id"], "viewer")
        self.assertEqual([r["status"] for r in records], ["open", "open"])
        with self.assertRaises(ConflictError):
            self.service.retry_withdrawal(self.item["id"], applications[0]["id"],
                                          "officer1", "radiation_officer")
        self.assertTrue(self.repo.verify_audit_chain())

    def test_dose_correction_recalculates_basis(self):
        with self.assertRaises(ConflictError):
            self.service.correct_dose(
                self.item["id"],
                {"quantity": 20, "reason": "剂量更正",
                 "expected_version": self.closed["version"]},
                "dosimetrist1", "dosimetrist")
        application = self._withdraw()
        before = self.service.get_item(self.item["id"], "viewer")
        self.assertFalse(before["escalation_required"])
        with self.assertRaises(PermissionDenied):
            self.service.correct_dose(
                self.item["id"],
                {"quantity": 20, "reason": "剂量更正",
                 "expected_version": before["version"]},
                "attacker", "viewer")
        updated = self.service.correct_dose(
            self.item["id"],
            {"quantity": 20, "reason": "计数器复核后更正",
             "expected_version": before["version"]},
            "dosimetrist1", "dosimetrist")
        self.assertEqual(updated["quantity"], 20.0)
        self.assertEqual(updated["version"], before["version"] + 1)
        self.assertGreater(updated["priority"], before["priority"])
        self.assertLess(updated["deadline_hours"], before["deadline_hours"])
        self.assertTrue(updated["escalation_required"])
        revisions = self.service.list_basis_revisions(self.item["id"], "viewer")
        self.assertEqual(revisions[-1]["source"], "dose_correction")
        self.assertEqual(revisions[-1]["basis"]["quantity"], 20.0)
        reviewed = self.service.review_withdrawal(
            self.item["id"], application["id"], {}, "physicist",
            "health_physicist")
        self.assertEqual(reviewed["status"], "reviewed")
        revisions = self.service.list_basis_revisions(self.item["id"], "viewer")
        self.assertEqual(revisions[-1]["source"], "withdrawal_review")
        self.assertEqual(revisions[-1]["basis"]["quantity"], 20.0)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_withdrawal_guards(self):
        closure_version = self._closure_version()
        with self.assertRaises(PermissionDenied):
            self.service.request_withdrawal(
                self.item["id"],
                {"closure_version": closure_version, "reason": "误点结案",
                 "planned_items": "补录随访",
                 "expected_version": self.closed["version"]},
                "attacker", "health_physicist")
        with self.assertRaises(ValidationError):
            self.service.request_withdrawal(
                self.item["id"],
                {"closure_version": 99, "reason": "误点结案",
                 "planned_items": "补录随访",
                 "expected_version": self.closed["version"]},
                "officer1", "radiation_officer")
        with self.assertRaises(ValidationError):
            self.service.request_withdrawal(
                self.item["id"],
                {"closure_version": closure_version, "reason": "",
                 "planned_items": "补录随访",
                 "expected_version": self.closed["version"]},
                "officer1", "radiation_officer")
        with self.assertRaises(ConflictError):
            self.service.request_withdrawal(
                self.item["id"],
                {"closure_version": closure_version, "reason": "误点结案",
                 "planned_items": "补录随访", "expected_version": 99},
                "officer1", "radiation_officer")
        self._withdraw()
        with self.assertRaises(ConflictError):
            self._withdraw()
        self.assertTrue(self.repo.verify_audit_chain())


if __name__ == "__main__":
    unittest.main()
