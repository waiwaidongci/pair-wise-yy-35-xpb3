from __future__ import annotations

from typing import Any, Dict, Optional

from .audit import utc_now
from .domain import (ConflictError, ValidationError, ensure_role,
                     normalize_severity, require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CORRECTION_ROLES, CREATE_ROLES, ENTITY,
                    RECORD_ROLES, TITLE, VIEW_ROLES, WITHDRAW_REVIEW_ROLES,
                    WITHDRAW_ROLES, completion_blockers, escalation_required,
                    priority_score, response_deadline_hours,
                    role_for_transition, validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        if target == "closed":
            basis = self._current_basis(updated)
            records = self.repository.list_records(item_id)
            basis["open_record_ids"] = [r["id"] for r in records
                                        if r["status"] == "open"]
            basis["closed_record_ids"] = [r["id"] for r in records
                                          if r["status"] == "closed"]
            basis["closed_by"] = actor
            basis["closed_at"] = utc_now()
            self.repository.save_closure_snapshot(item_id, updated["version"],
                                                  basis, actor)
            self.repository.save_basis_revision(item_id, "closure", basis, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def request_withdrawal(self, item_id: int, payload: Dict[str, Any],
                           actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, WITHDRAW_ROLES)
        actor = require_text(actor, "actor", 100)
        reason = require_text(payload.get("reason"), "reason")
        planned_items = require_text(payload.get("planned_items"), "planned_items")
        closure_version = payload.get("closure_version")
        expected_version = payload.get("expected_version")
        if not isinstance(closure_version, int) or closure_version < 1:
            raise ValidationError("closure_version必须是正整数")
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValidationError("expected_version必须是正整数")
        item = self.repository.get_item(item_id)
        if item["status"] != "closed":
            raise ConflictError(
                f"仅结案事件可撤回结案，当前版本为{item['version']}")
        snapshot = self.repository.get_closure_snapshot(item_id, closure_version)
        if snapshot is None:
            raise ValidationError("结案版本不存在")
        reopen_ids = snapshot["basis"].get("closed_record_ids", [])
        application = self.repository.apply_withdrawal(
            item_id, expected_version, closure_version, reason, planned_items,
            actor, reopen_ids)
        try:
            self.repository.append_audit("withdraw_closure", ENTITY, item_id, actor, {
                "application_id": application["id"],
                "revision": application["revision"],
                "closure_version": closure_version, "reason": reason,
                "planned_items": planned_items,
                "reopened_record_ids": reopen_ids, "retry": False,
            })
        except Exception:
            self.repository.restore_closure(item_id, application["id"])
            raise
        return self.repository.update_withdrawal_status(item_id, application["id"],
                                                        "applied")

    def retry_withdrawal(self, item_id: int, application_id: int, actor: str,
                         role: str) -> Dict[str, Any]:
        ensure_role(role, WITHDRAW_ROLES)
        actor = require_text(actor, "actor", 100)
        application = self.repository.get_withdrawal(item_id, application_id)
        if application["status"] != "pending":
            raise ConflictError("撤回申请已处理，不能重试")
        item = self.repository.get_item(item_id)
        if item["status"] != "closed":
            raise ConflictError("剂量事件未处于结案状态，不能重试")
        snapshot = self.repository.get_closure_snapshot(
            item_id, application["closure_version"])
        if snapshot is None:
            raise ValidationError("结案版本不存在")
        reopen_ids = snapshot["basis"].get("closed_record_ids", [])
        application = self.repository.reapply_withdrawal(item_id, application_id,
                                                         reopen_ids)
        try:
            self.repository.append_audit("withdraw_closure", ENTITY, item_id, actor, {
                "application_id": application["id"],
                "revision": application["revision"],
                "closure_version": application["closure_version"],
                "reason": application["reason"],
                "planned_items": application["planned_items"],
                "reopened_record_ids": reopen_ids, "retry": True,
            })
        except Exception:
            self.repository.restore_closure(item_id, application_id)
            raise
        return self.repository.update_withdrawal_status(item_id, application_id,
                                                        "applied")

    def review_withdrawal(self, item_id: int, application_id: int,
                          payload: Dict[str, Any], actor: str,
                          role: str) -> Dict[str, Any]:
        ensure_role(role, WITHDRAW_REVIEW_ROLES)
        actor = require_text(actor, "actor", 100)
        comment = payload.get("comment")
        if comment is not None:
            comment = require_text(comment, "comment")
        application = self.repository.get_withdrawal(item_id, application_id)
        if application["status"] != "applied":
            raise ConflictError("撤回申请未处于待复核状态")
        item = self.repository.get_item(item_id)
        basis = self._current_basis(item)
        basis["application_id"] = application_id
        basis["revision"] = application["revision"]
        reviewed = self.repository.update_withdrawal_status(
            item_id, application_id, "reviewed", actor)
        basis_revision = self.repository.save_basis_revision(
            item_id, "withdrawal_review", basis, actor)
        self.repository.append_audit("review_withdrawal", ENTITY, item_id, actor, {
            "application_id": application_id, "revision": application["revision"],
            "comment": comment, "basis_revision": basis_revision,
        })
        return reviewed

    def correct_dose(self, item_id: int, payload: Dict[str, Any], actor: str,
                     role: str) -> Dict[str, Any]:
        ensure_role(role, CORRECTION_ROLES)
        actor = require_text(actor, "actor", 100)
        reason = require_text(payload.get("reason"), "reason")
        expected_version = payload.get("expected_version")
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValidationError("expected_version必须是正整数")
        item = self.repository.get_item(item_id)
        if item["status"] == "closed":
            raise ConflictError("已结案事件需先撤回结案后再更正剂量")
        severity = payload.get("severity", item["severity"])
        severity = normalize_severity(severity)
        quantity = require_number(payload.get("quantity", item["quantity"]), "quantity")
        threshold = require_number(payload.get("threshold", item["threshold"]),
                                   "threshold", 0.000001)
        updated = self.repository.correct_dose(item_id, expected_version, severity,
                                               quantity, threshold, actor)
        basis = self._current_basis(updated)
        basis["reason"] = reason
        basis_revision = self.repository.save_basis_revision(
            item_id, "dose_correction", basis, actor)
        self.repository.append_audit("dose_correction", ENTITY, item_id, actor, {
            "reason": reason,
            "before": {"severity": item["severity"], "quantity": item["quantity"],
                       "threshold": item["threshold"]},
            "after": {"severity": severity, "quantity": quantity,
                      "threshold": threshold},
            "basis_revision": basis_revision,
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def list_withdrawals(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_withdrawals(item_id)

    def list_closure_snapshots(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_closure_snapshots(item_id)

    def list_basis_revisions(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_basis_revisions(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    def _current_basis(self, item: Dict[str, Any]) -> Dict[str, Any]:
        open_records = self.repository.open_record_count(item["id"])
        return {
            "item_id": item["id"], "version": item["version"],
            "severity": item["severity"], "quantity": item["quantity"],
            "threshold": item["threshold"], "open_records": open_records,
            "priority": priority_score(item["severity"], item["quantity"],
                                       item["threshold"], open_records),
            "deadline_hours": response_deadline_hours(
                item["severity"], item["quantity"], item["threshold"]),
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        }

    def enrich(self, item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        basis = self._current_basis(item)
        result["open_records"] = basis["open_records"]
        result["priority"] = basis["priority"]
        result["deadline_hours"] = basis["deadline_hours"]
        result["escalation_required"] = basis["escalation_required"]
        return result
