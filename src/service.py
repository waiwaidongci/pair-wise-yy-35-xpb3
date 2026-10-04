from __future__ import annotations

from typing import Any, Dict, Optional

from .audit import utc_now
from .domain import (ConflictError, ensure_role, normalize_severity, require_number,
                     require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, DOSE_CORRECTION_ROLES, ENTITY,
                    RECORD_ROLES, TITLE, VIEW_ROLES, WITHDRAWAL_CREATE_ROLES,
                    WITHDRAWAL_REVIEW_ROLES, WITHDRAWAL_SUBMIT_ROLES,
                    completion_blockers, escalation_required, priority_score,
                    response_deadline_hours, role_for_transition, validate_transition)


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
            from .domain import ConflictError
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
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

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    # ---- 撤回结案（受控复核） ----

    def _build_close_snapshot(self, item: Dict[str, Any]) -> Dict[str, Any]:
        records = self.repository.list_records(item["id"])
        open_records = sum(1 for r in records if r["status"] == "open")
        return {
            "severity": item["severity"],
            "quantity": item["quantity"],
            "threshold": item["threshold"],
            "status": item["status"],
            "version": item["version"],
            "open_records": open_records,
            "records": records,
            "priority": priority_score(
                item["severity"], item["quantity"], item["threshold"], open_records),
            "deadline_hours": response_deadline_hours(
                item["severity"], item["quantity"], item["threshold"]),
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
            "snapshotted_at": utc_now(),
        }

    def create_withdrawal(self, item_id: int, payload: Dict[str, Any],
                          actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, WITHDRAWAL_CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        if item["status"] != "closed":
            raise ConflictError("只有已结案的剂量事件才能撤回结案")
        close_version = payload.get("close_version")
        if not isinstance(close_version, int) or close_version < 1:
            from .domain import ValidationError
            raise ValidationError("close_version必须是正整数")
        if close_version != item["version"]:
            raise ConflictError("结案版本与当前版本不一致")
        reason = require_text(payload.get("reason"), "撤回原因")
        raw_items = payload.get("supplementary_items")
        if not isinstance(raw_items, list) or not raw_items:
            from .domain import ValidationError
            raise ValidationError("拟补事项不能为空")
        supplementary_items = [require_text(t, "拟补事项", 200) for t in raw_items]
        active = self.repository.get_active_withdrawal(item_id)
        if active is not None:
            raise ConflictError("该剂量事件已有进行中的撤回申请",
                                {"app_number": active["app_number"]})
        snapshot = self._build_close_snapshot(item)
        app = self.repository.create_withdrawal(
            item_id, close_version, reason, supplementary_items, snapshot, actor)
        self.repository.append_audit("withdrawal_created", ENTITY, item_id, actor, {
            "app_number": app["app_number"],
            "close_version": close_version,
            "reason": reason,
            "supplementary_items": supplementary_items,
        })
        return self._enrich_withdrawal(app)

    def submit_withdrawal(self, item_id: int, app_id: int, expected_revision: int,
                          actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, WITHDRAWAL_SUBMIT_ROLES)
        actor = require_text(actor, "actor", 100)
        app = self.repository.get_withdrawal(app_id)
        if app["item_id"] != item_id:
            from .domain import NotFoundError
            raise NotFoundError("撤回申请不存在")
        if not isinstance(expected_revision, int) or expected_revision < 1:
            from .domain import ValidationError
            raise ValidationError("expected_revision必须是正整数")
        snapshot = app["close_snapshot"]
        try:
            item, app, _opened_ids = self.repository.submit_withdrawal(
                app_id, expected_revision, app["supplementary_items"], actor)
        except ConflictError:
            raise
        try:
            self.repository.append_audit("withdrawal_submitted", ENTITY, item_id, actor, {
                "app_number": app["app_number"],
                "from": snapshot["status"],
                "to": "follow_up",
            })
        except Exception:
            # 审计链写入失败：从结案快照恢复，剂量事件保持结案，申请号不变
            self.repository.restore_withdrawal(app_id, snapshot, expected_revision, actor)
            raise
        return self._enrich_withdrawal(app)

    def review_withdrawal(self, item_id: int, app_id: int, expected_revision: int,
                          actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, WITHDRAWAL_REVIEW_ROLES)
        actor = require_text(actor, "actor", 100)
        app = self.repository.get_withdrawal(app_id)
        if app["item_id"] != item_id:
            from .domain import NotFoundError
            raise NotFoundError("撤回申请不存在")
        if not isinstance(expected_revision, int) or expected_revision < 1:
            from .domain import ValidationError
            raise ValidationError("expected_revision必须是正整数")
        item = self.repository.get_item(item_id)
        open_records = self.repository.open_record_count(item_id)
        basis = {
            "severity": item["severity"],
            "quantity": item["quantity"],
            "threshold": item["threshold"],
            "open_records": open_records,
            "priority": priority_score(
                item["severity"], item["quantity"], item["threshold"], open_records),
            "deadline_hours": response_deadline_hours(
                item["severity"], item["quantity"], item["threshold"]),
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
            "reviewed_at": utc_now(),
        }
        app = self.repository.review_withdrawal(app_id, expected_revision, basis, actor)
        self.repository.append_audit("withdrawal_reviewed", ENTITY, item_id, actor, {
            "app_number": app["app_number"],
            "basis": basis,
        })
        return self._enrich_withdrawal(app)

    def correct_dose(self, item_id: int, payload: Dict[str, Any],
                      actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, DOSE_CORRECTION_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        if item["status"] != "follow_up":
            raise ConflictError("只有随访中的剂量事件才能更正剂量")
        quantity = require_number(payload.get("quantity"), "quantity")
        threshold = require_number(payload.get("threshold", item["threshold"]),
                                    "threshold", 0.000001)
        reason = require_text(payload.get("reason"), "更正原因", 200)
        expected_version = payload.get("expected_version")
        if not isinstance(expected_version, int) or expected_version < 1:
            from .domain import ValidationError
            raise ValidationError("expected_version必须是正整数")
        old = {"quantity": item["quantity"], "threshold": item["threshold"]}
        updated = self.repository.correct_item_dose(
            item_id, quantity, threshold, expected_version, actor)
        self.repository.append_audit("dose_corrected", ENTITY, item_id, actor, {
            "old": old,
            "new": {"quantity": quantity, "threshold": threshold},
            "reason": reason,
        })
        return self.enrich(updated)

    def get_withdrawal(self, item_id: int, app_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        app = self.repository.get_withdrawal(app_id)
        if app["item_id"] != item_id:
            from .domain import NotFoundError
            raise NotFoundError("撤回申请不存在")
        return self._enrich_withdrawal(app)

    def list_withdrawals(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        self.repository.get_item(item_id)
        return {"withdrawals": [self._enrich_withdrawal(app)
                                for app in self.repository.list_withdrawals(item_id)]}

    @staticmethod
    def _enrich_withdrawal(app: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(app)
        return result

    def enrich(self, item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        open_records = self.repository.open_record_count(item["id"])
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"], open_records)
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
