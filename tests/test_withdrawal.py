import tempfile, unittest, threading
from pathlib import Path

from src.repository import Repository
from src.service import Service
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.rules import STATES, TRANSITION_ROLES


class WithdrawalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item(
            {"title": "剂量事件", "description": "超剂量照射", "severity": "high",
             "quantity": 12, "threshold": 6, "external_ref": "EVT-1"},
            "creator", "dosimetrist")
        current = self.item
        for target in STATES[1:]:
            current = self.service.transition(
                current["id"], target, current["version"],
                "officer", TRANSITION_ROLES[target][0])
        self.closed = current
        self.assertEqual(self.closed["status"], "closed")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _make_application(self):
        return self.service.create_withdrawal(
            self.closed["id"],
            {"close_version": self.closed["version"],
             "reason": "误点结案，实际仍有随访事项未完成",
             "supplementary_items": ["补充剂量计读数", "复查血常规"]},
            "applicant", "radiation_officer")

    def test_full_withdrawal_flow(self):
        app = self._make_application()
        self.assertEqual(app["status"], "pending")
        self.assertTrue(app["app_number"].startswith("WD-"))
        self.assertEqual(app["close_snapshot"]["status"], "closed")
        self.assertEqual(app["close_snapshot"]["quantity"], 12)

        submitted = self.service.submit_withdrawal(
            self.closed["id"], app["id"], 1, "officer", "radiation_officer")
        self.assertEqual(submitted["status"], "submitted")

        item = self.service.get_item(self.closed["id"], "viewer")
        self.assertEqual(item["status"], "follow_up")
        records = self.service.list_records(self.closed["id"], "viewer")
        self.assertEqual(len([r for r in records if r["status"] == "open"]), 2)

        corrected = self.service.correct_dose(
            self.closed["id"],
            {"quantity": 18, "threshold": 6, "reason": "修正剂量计校准系数",
             "expected_version": item["version"]},
            "dosimetrist", "dosimetrist")
        self.assertEqual(corrected["quantity"], 18)

        reviewed = self.service.review_withdrawal(
            self.closed["id"], app["id"], 2, "physicist", "health_physicist")
        self.assertEqual(reviewed["status"], "reviewed")
        self.assertEqual(reviewed["basis"]["quantity"], 18)
        self.assertEqual(reviewed["basis"]["open_records"], 2)

        fetched = self.service.get_withdrawal(self.closed["id"], app["id"], "viewer")
        self.assertEqual(fetched["close_snapshot"]["status"], "closed")
        self.assertEqual(fetched["close_snapshot"]["quantity"], 12)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_close_snapshot_and_list_queryable(self):
        app = self._make_application()
        fetched = self.service.get_withdrawal(self.closed["id"], app["id"], "viewer")
        self.assertEqual(fetched["close_snapshot"]["version"], self.closed["version"])
        self.assertIn("priority", fetched["close_snapshot"])
        self.assertIn("deadline_hours", fetched["close_snapshot"])
        listing = self.service.list_withdrawals(self.closed["id"], "viewer")
        self.assertEqual(len(listing["withdrawals"]), 1)
        self.assertEqual(listing["withdrawals"][0]["app_number"], app["app_number"])

    def test_concurrent_submit_first_wins_later_receives_current_version(self):
        app = self._make_application()
        results = []

        def do_submit():
            try:
                r = self.service.submit_withdrawal(
                    self.closed["id"], app["id"], 1, "officer", "radiation_officer")
                results.append(("ok", r))
            except Exception as exc:
                results.append(("err", exc))

        t1 = threading.Thread(target=do_submit)
        t2 = threading.Thread(target=do_submit)
        t1.start(); t2.start(); t1.join(); t2.join()

        self.assertEqual(len(results), 2)
        ok = [r for r in results if r[0] == "ok"]
        err = [r for r in results if r[0] == "err"]
        self.assertEqual(len(ok), 1)
        self.assertEqual(len(err), 1)
        self.assertEqual(err[0][1].details["current_revision"], 2)
        self.assertEqual(ok[0][1]["revision"], 2)

    def test_audit_failure_restores_snapshot_and_retries_same_app_number(self):
        app = self._make_application()
        original = self.repo.append_audit

        def failing_append(*args, **kwargs):
            if args[0] == "withdrawal_submitted":
                raise RuntimeError("审计链写入失败")
            return original(*args, **kwargs)

        self.repo.append_audit = failing_append
        with self.assertRaises(RuntimeError):
            self.service.submit_withdrawal(
                self.closed["id"], app["id"], 1, "officer", "radiation_officer")

        item = self.service.get_item(self.closed["id"], "viewer")
        self.assertEqual(item["status"], "closed")
        restored = self.service.get_withdrawal(self.closed["id"], app["id"], "viewer")
        self.assertEqual(restored["status"], "pending")
        self.assertEqual(restored["app_number"], app["app_number"])
        self.assertEqual(restored["revision"], 1)
        records = self.service.list_records(self.closed["id"], "viewer")
        self.assertEqual(len([r for r in records if r["status"] == "open"]), 0)

        self.repo.append_audit = original
        retried = self.service.submit_withdrawal(
            self.closed["id"], app["id"], 1, "officer", "radiation_officer")
        self.assertEqual(retried["status"], "submitted")
        self.assertEqual(retried["app_number"], app["app_number"])
        item = self.service.get_item(self.closed["id"], "viewer")
        self.assertEqual(item["status"], "follow_up")
        self.assertTrue(self.repo.verify_audit_chain())

    def test_review_requires_health_physicist(self):
        app = self._make_application()
        self.service.submit_withdrawal(
            self.closed["id"], app["id"], 1, "officer", "radiation_officer")
        with self.assertRaises(PermissionDenied):
            self.service.review_withdrawal(
                self.closed["id"], app["id"], 2, "officer", "radiation_officer")
        with self.assertRaises(PermissionDenied):
            self.service.review_withdrawal(
                self.closed["id"], app["id"], 2, "dosim", "dosimetrist")

    def test_submit_requires_radiation_officer(self):
        app = self._make_application()
        with self.assertRaises(PermissionDenied):
            self.service.submit_withdrawal(
                self.closed["id"], app["id"], 1, "dosim", "dosimetrist")

    def test_create_requires_closed_item(self):
        other = self.service.create_item(
            {"title": "在访事件", "description": "未结案", "severity": "low",
             "quantity": 1, "threshold": 10, "external_ref": "EVT-2"},
            "creator", "dosimetrist")
        with self.assertRaises(ConflictError):
            self.service.create_withdrawal(
                other["id"],
                {"close_version": other["version"], "reason": "x",
                 "supplementary_items": ["y"]},
                "applicant", "radiation_officer")

    def test_create_validates_fields(self):
        with self.assertRaises(ConflictError):
            self.service.create_withdrawal(
                self.closed["id"],
                {"close_version": 99, "reason": "x", "supplementary_items": ["y"]},
                "applicant", "radiation_officer")
        with self.assertRaises(ValidationError):
            self.service.create_withdrawal(
                self.closed["id"],
                {"close_version": self.closed["version"], "reason": "",
                 "supplementary_items": []},
                "applicant", "radiation_officer")

    def test_dose_correction_requires_follow_up(self):
        with self.assertRaises(ConflictError):
            self.service.correct_dose(
                self.closed["id"],
                {"quantity": 10, "expected_version": self.closed["version"]},
                "dosim", "dosimetrist")

    def test_second_withdrawal_blocked_while_active(self):
        self._make_application()
        with self.assertRaises(ConflictError):
            self.service.create_withdrawal(
                self.closed["id"],
                {"close_version": self.closed["version"], "reason": "x",
                 "supplementary_items": ["y"]},
                "applicant", "radiation_officer")

    def test_live_priority_recalculated_after_dose_correction(self):
        app = self._make_application()
        self.service.submit_withdrawal(
            self.closed["id"], app["id"], 1, "officer", "radiation_officer")
        before = self.service.get_item(self.closed["id"], "viewer")
        self.assertEqual(before["quantity"], 12)
        self.service.correct_dose(
            self.closed["id"],
            {"quantity": 1, "threshold": 6, "reason": "剂量更正",
             "expected_version": before["version"]},
            "dosimetrist", "dosimetrist")
        after = self.service.get_item(self.closed["id"], "viewer")
        self.assertNotEqual(before["priority"], after["priority"])
        self.assertNotEqual(before["deadline_hours"], after["deadline_hours"])
        self.assertNotEqual(before["escalation_required"], after["escalation_required"])


if __name__ == "__main__":
    unittest.main()
