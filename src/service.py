from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, PermissionDenied
from .repository import utcnow
from .rules import (
    CONCLUSION_STATUSES,
    RuleEngine,
    assert_station_ownership,
    next_supplement_status,
    recompute_magnitude,
    reports_station_count,
)


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field,
                                             value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    # ---- 创建 -----------------------------------------------------------

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

        if kind == "event":
            entity = self._create_event(actor, payload)
        else:
            entity_id = str(payload.pop("id", "") or uuid4())
            if self.repository.get_entity(entity_id):
                raise ConflictError("entity already exists: " + entity_id)
            status = self.rules.initial_status(kind)
            entity = self.repository.create_entity(
                entity_id, kind, status, payload, actor.user_id
            )
            self.audit.record(entity_id, actor, "create", None, status,
                              {"kind": kind})

        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key,
                                             entity["id"])
        return entity

    def _create_event(self, actor, payload):
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        seed = payload.pop("reports", None) or []
        reports = {}
        report_entities = []
        seq_by_station = {}
        for item in seed:
            entry = dict(item)
            station_code = entry.pop("station_code", None) or entry.pop("station")
            seq = seq_by_station.get(station_code, 0) + 1
            seq_by_station[station_code] = seq
            entry["station_code"] = station_code
            entry["seq"] = seq
            entry["received_at"] = entry.get("received_at") or utcnow()
            report_id = "%s@%s#v%s" % (station_code, entity_id, seq)
            # 初始集合同样只采用每个台站最新一版；旧版实体仍创建、留痕可查。
            superseded_seed = []
            for old_id, old_entry in list(reports.items()):
                if old_entry.get("station_code") == station_code:
                    reports.pop(old_id)
                    superseded_seed.append(old_id)
            reports[report_id] = entry
            report_entities.append(
                (
                    report_id,
                    {
                        "event_id": entity_id,
                        "station_code": station_code,
                        "seq": seq,
                        "status": "received",
                        "amplitude": entry.get("amplitude"),
                        "time_offset": entry.get("time_offset"),
                        "distance_km": entry.get("distance_km"),
                        "received_at": entry["received_at"],
                        "supersedes": superseded_seed,
                    },
                )
            )
        event_data = {
            **payload,
            "reports": reports,
            "report_set_version": 1,
            "station_count": reports_station_count(reports),
        }
        entity = self.repository.create_event_with_reports(
            entity_id, "candidate", event_data, report_entities, actor.user_id
        )
        self.audit.record(entity_id, actor, "create", None, "candidate",
                          {"kind": "event",
                           "report_set_version": 1,
                           "seeded": len(report_entities)})
        return entity

    # ---- 动作分发 -------------------------------------------------------

    def transition(self, actor, entity_id, action, data=None, expected_version=None,
                   idempotency_key=None):
        if action == "supplement":
            return self.supplement_report(
                actor, entity_id, data or {}, expected_version, idempotency_key
            )
        if action == "retry_pending":
            return self.retry_pending(actor, entity_id, data or {},
                                      idempotency_key=idempotency_key)

        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)

        if action == "reconcile":
            return self._reconcile(actor, entity, data or {})

        expected = int(expected_version) if expected_version is not None else entity[
            "version"
        ]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        from_status = entity["status"]
        merged = dict(entity["data"])
        merged.update(patch)
        if action == "publish":
            # 发布即冻结当时的报告集合版本，作为后续对账依据。
            merged["published_report_set_version"] = merged.get(
                "report_set_version"
            )
        updated = self.repository.update_entity(entity_id, expected, next_status,
                                                merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            from_status,
            updated["status"],
            {"patch": patch,
             "report_set_version": merged.get("report_set_version")},
        )
        return updated

    # ---- 补报联动 -------------------------------------------------------

    def supplement_report(self, actor, event_id, data, expected_version=None,
                          idempotency_key=None):
        event = self.repository.get_entity(event_id)
        if not event:
            raise NotFoundError("entity not found: " + event_id)
        station_code = self.rules.validate_supplement(event, data)
        assert_station_ownership(actor, station_code, self._lookup)

        ref = data.get("ref") or idempotency_key or "%s:%s:%s" % (
            event_id, station_code, uuid4()
        )
        idem_key = idempotency_key or "supplement:" + ref
        payload = dict(data)
        payload["station_code"] = station_code

        # 同一 ref 重放且仍在待重试：直接重试。
        pending = self.repository.get_pending_report(ref)
        if pending:
            if pending["event_id"] != event_id:
                raise ConflictError(
                    "ref %s already belongs to event %s"
                    % (ref, pending["event_id"])
                )
            return self._apply_pending(actor, pending, expected_version,
                                       from_status=event["status"])
        # 已成功采用的补报重放：幂等返回当前事件，不再生成新报告。
        completed = self.repository.get_idempotency(actor.user_id, idem_key)
        if completed:
            latest = self.repository.get_entity(completed)
            if latest:
                return latest

        # 写入失败时这条记录保留在 pending_reports 中待重试。
        pending = self.repository.save_pending_report(
            {
                "ref": ref,
                "event_id": event_id,
                "station_code": station_code,
                "actor_id": actor.user_id,
                "payload": payload,
            }
        )
        result = self._apply_pending(actor, pending, expected_version,
                                     from_status=event["status"])
        result["ref"] = ref
        return result

    def _apply_pending(self, actor, pending, expected_version=None,
                       from_status=None):
        """把待重试报告合并进事件；任何失败都保留 pending，不改变事件。"""

        def prepare(connection, event):
            reports = dict(event["data"].get("reports") or {})
            station_reports = [
                (rid, entry)
                for rid, entry in reports.items()
                if entry.get("station_code") == pending["station_code"]
            ]
            seq = (
                max(
                    (entry.get("seq", 0) for _, entry in station_reports),
                    default=0,
                )
                + 1
            )
            # 同一事件并发补报时只认最新一版报告集合：该台站旧版退出采用集合，
            # 旧报告实体标记 superseded 留痕，但不再计入台站数与震级。
            superseded_ids = [rid for rid, _ in station_reports]
            for rid in superseded_ids:
                reports.pop(rid, None)

            payload = pending["payload"]
            entry = {
                "station_code": pending["station_code"],
                "seq": seq,
                "amplitude": payload.get("amplitude"),
                "time_offset": payload.get("time_offset"),
                "distance_km": payload.get("distance_km"),
                "received_at": utcnow(),
                "ref": pending["ref"],
            }
            report_id = "%s@%s#v%s" % (
                pending["station_code"], event["id"], seq
            )
            reports[report_id] = entry
            set_version = int(event["data"].get("report_set_version", 0)) + 1
            new_status, _published_invalidated = next_supplement_status(
                event["status"]
            )
            new_data = dict(event["data"])
            new_data["reports"] = reports
            new_data["report_set_version"] = set_version
            new_data["station_count"] = reports_station_count(reports)
            # 报告一更新，先到的复核结果立即失效并重新算震级。
            if event["status"] in CONCLUSION_STATUSES:
                self._archive_conclusion(new_data, event, reason="report_updated",
                                         ref=pending["ref"])
                new_data.pop("reviewer", None)
            new_data["magnitude"] = recompute_magnitude(reports)
            new_data["magnitude_basis"] = "auto"
            report_data = {
                "event_id": event["id"],
                "station_code": pending["station_code"],
                "seq": seq,
                "status": "received",
                "amplitude": entry["amplitude"],
                "time_offset": entry["time_offset"],
                "distance_km": entry["distance_km"],
                "received_at": entry["received_at"],
                "ref": pending["ref"],
                "report_set_version": set_version,
                "supersedes": superseded_ids,
            }
            prepare.last_report_id = report_id
            prepare.set_version = set_version
            prepare.superseded_ids = superseded_ids
            return {
                "status": new_status,
                "data": new_data,
                "report_id": report_id,
                "report_data": report_data,
                "superseded_ids": superseded_ids,
            }

        current = self.repository.get_entity(pending["event_id"])
        if not current:
            raise NotFoundError("entity not found: " + pending["event_id"])
        if from_status is None:
            from_status = current["status"]
        updated = self.repository.apply_pending_report(
            pending, expected_version, prepare
        )
        report_id = getattr(prepare, "last_report_id", None)
        set_version = getattr(prepare, "set_version", None)
        superseded_ids = getattr(prepare, "superseded_ids", [])
        self.audit.record(
            pending["event_id"],
            actor,
            "supplement",
            from_status,
            updated["status"],
            {
                "ref": pending["ref"],
                "station_code": pending["station_code"],
                "report_id": report_id,
                "superseded": superseded_ids,
                "report_set_version": set_version,
                "magnitude": updated["data"].get("magnitude"),
                "conclusion_invalidated": from_status in CONCLUSION_STATUSES,
            },
        )
        if from_status in CONCLUSION_STATUSES:
            self.audit.record(
                pending["event_id"],
                actor,
                "invalidate_review",
                from_status,
                updated["status"],
                {
                    "reason": "report_updated",
                    "ref": pending["ref"],
                    "report_set_version": set_version,
                },
            )
        if report_id:
            self.audit.record(
                report_id,
                actor,
                "supplement_report",
                None,
                "received",
                {"event_id": pending["event_id"],
                 "station_code": pending["station_code"],
                 "ref": pending["ref"],
                 "supersedes": superseded_ids},
            )
        # 成功采用后，ref 才登记幂等；失败时仍留在 pending_reports。
        self.repository.save_idempotency(
            pending["actor_id"], "supplement:" + pending["ref"], pending["event_id"]
        )
        return updated

    @staticmethod
    def _archive_conclusion(new_data, event, reason, ref=None):
        """旧结论留痕：归档到事件历史，不覆盖、可审计。"""
        history = list(event["data"].get("conclusion_history") or [])
        history.append(
            {
                "reason": reason,
                "ref": ref,
                "status": event["status"],
                "reviewer": event["data"].get("reviewer"),
                "magnitude": event["data"].get("magnitude"),
                "magnitude_basis": event["data"].get("magnitude_basis"),
                "report_set_version": event["data"].get("report_set_version"),
                "communication_id": event["data"].get("communication_id"),
                "archived_at": utcnow(),
            }
        )
        new_data["conclusion_history"] = history

    # ---- 待重试 ---------------------------------------------------------

    def retry_pending(self, actor, event_id=None, data=None, idempotency_key=None):
        data = data or {}
        refs = data.get("refs")
        if isinstance(refs, str):
            refs = [refs]
        ref = data.get("ref") or (refs[0] if refs else None) or idempotency_key
        if ref and not refs:
            refs = [ref]

        pending_items = self.repository.list_pending_reports(event_id=event_id)
        if refs:
            wanted = set(refs)
            pending_items = [item for item in pending_items if item["ref"] in wanted]
            missing = wanted - {item["ref"] for item in pending_items}
            if missing:
                raise NotFoundError("pending report not found: " + ", ".join(sorted(missing)))

        if actor.role not in ("admin", "analyst", "reviewer"):
            for item in pending_items:
                if item["actor_id"] != actor.user_id:
                    raise PermissionDenied(
                        "actor %s may only retry its own pending reports"
                        % actor.user_id
                    )

        applied, failures = [], []
        for item in pending_items:
            try:
                # 重试不带事件版本假设：始终对当前最新报告集合合并；
                # 同 ref 的幂等由 pending 清除与 reports 只存一版保证，
                # 不会重复计入台站数。
                updated = self._apply_pending(actor, item, None)
                applied.append(
                    {
                        "ref": item["ref"],
                        "event_id": item["event_id"],
                        "station_code": item["station_code"],
                        "event_version": updated["version"],
                        "report_set_version": updated["data"].get(
                            "report_set_version"
                        ),
                    }
                )
            except Exception as exc:  # 单条失败不影响其它待重试项
                failures.append(
                    {"ref": item["ref"], "error": str(exc),
                     "type": type(exc).__name__}
                )
        return {"applied": applied, "failures": failures}

    def list_pending(self, event_id=None):
        return self.repository.list_pending_reports(event_id=event_id)

    # ---- 对账 -----------------------------------------------------------

    def _reconcile(self, actor, entity, data):
        next_status, detail = self.rules.evaluate_reconciliation(
            actor, entity, dict(data)
        )
        merged = dict(entity["data"])
        merged["reconciled_communication_id"] = detail["communication_id"]
        merged["reconciled_report_set_version"] = detail["report_set_version"]
        if not detail["matched"]:
            # 版本对不上：发布结论挂在过时报告上，一律退回待复核并留痕。
            self._archive_conclusion(merged, entity, reason="reconcile_mismatch",
                                     ref=detail["communication_id"])
            merged.pop("reviewer", None)
            merged["magnitude"] = recompute_magnitude(merged.get("reports") or {})
            merged["magnitude_basis"] = "auto"
        updated = self.repository.update_entity(
            entity["id"], entity["version"], next_status, merged
        )
        self.audit.record(
            entity["id"], actor, "reconcile", entity["status"], next_status, detail
        )
        if not detail["matched"]:
            self.audit.record(
                entity["id"], actor, "invalidate_review", "published",
                "pending_review",
                {"reason": "reconcile_mismatch", **detail},
            )
        return updated

    # ---- 查询 -----------------------------------------------------------

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
