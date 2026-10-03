from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, ValidationError
from .repository import utcnow
from .rules import RuleEngine


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
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        if entity["kind"] == "event":
            if action == "supplement":
                payload = data or {}
                return self.supplement(
                    actor,
                    entity_id,
                    payload.get("reports") or [],
                    expected_version,
                    payload.get("idempotency_key"),
                )
            if action == "reconcile":
                payload = data or {}
                return self.reconcile(actor, entity_id, payload.get("communication_id"))
            if action == "review":
                return self.review(actor, entity_id, data or {}, expected_version)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
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

    def review(self, actor, event_id, data, expected_version=None):
        event = self.repository.get_entity(event_id)
        if not event:
            raise NotFoundError("entity not found: " + event_id)
        next_status, patch = self.rules.validate_transition(
            actor, event, "review", dict(data), self._lookup
        )
        review_obj = {
            "reviewer": patch["reviewer"],
            "magnitude": patch["magnitude"],
            "magnitude_source": patch["magnitude_source"],
            "report_version": int(event["data"].get("report_version", 1)),
            "reviewed_at": utcnow(),
        }
        new_data = dict(event["data"])
        new_data["review"] = review_obj
        expected = int(expected_version) if expected_version is not None else event["version"]
        updated = self.repository.update_entity(event_id, expected, next_status, new_data)
        self.audit.record(
            event_id,
            actor,
            "review",
            event["status"],
            next_status,
            {"review": review_obj},
        )
        return updated

    @staticmethod
    def _merge_reports(existing, intake_rows):
        result = [dict(report) for report in existing]
        index = {}
        for position, report in enumerate(result):
            station = report.get("station")
            if station:
                index[station] = position
        for row in intake_rows:
            report = dict(row["payload"])
            station = report.get("station")
            if station in index:
                result[index[station]] = report
            elif station:
                index[station] = len(result)
                result.append(report)
        return result

    def supplement(self, actor, event_id, reports, expected_version=None, idempotency_key=None):
        event = self.repository.get_entity(event_id)
        if not event:
            raise NotFoundError("entity not found: " + event_id)
        self.rules.validate_transition(
            actor, event, "supplement", {"reports": list(reports)}, self._lookup
        )
        client_key = idempotency_key or str(uuid4())
        for report in reports:
            self.repository.create_intake(event_id, report.get("station"), report, client_key)
        pending = self.repository.list_intake(event_id, status="pending")
        if not pending:
            return event
        merged_reports = self._merge_reports(event["data"].get("reports") or [], pending)
        new_data = dict(event["data"])
        new_data["reports"] = merged_reports
        new_data["report_version"] = int(new_data.get("report_version", 1)) + 1
        old_review = new_data.get("review")
        new_data["review"] = None
        if event["status"] in ("published", "revised"):
            next_status = event["status"]
        else:
            next_status = "associated"
        expected = int(expected_version) if expected_version is not None else event["version"]
        try:
            updated = self.repository.commit_supplement(
                event_id, expected, next_status, new_data, [row["id"] for row in pending]
            )
        except ConflictError:
            self.audit.record(
                event_id,
                actor,
                "supplement",
                event["status"],
                event["status"],
                {
                    "result": "conflict",
                    "report_version": int(event["data"].get("report_version", 1)),
                    "pending": [row["station"] for row in pending],
                },
            )
            raise
        station_count = len({report.get("station") for report in merged_reports if report.get("station")})
        self.audit.record(
            event_id,
            actor,
            "supplement",
            event["status"],
            updated["status"],
            {
                "result": "accepted",
                "report_version": updated["data"]["report_version"],
                "added": [row["station"] for row in pending],
                "station_count": station_count,
                "old_review": old_review,
            },
        )
        return updated

    def reconcile(self, actor, event_id, communication_id):
        event = self.repository.get_entity(event_id)
        if not event:
            raise NotFoundError("entity not found: " + event_id)
        self.rules.validate_transition(
            actor, event, "reconcile", {"communication_id": communication_id}, self._lookup
        )
        data = event["data"]
        publication = data.get("publication")
        if not publication or publication.get("communication_id") != communication_id:
            raise ValidationError(
                "no publication found for communication_id: " + str(communication_id)
            )
        current_version = int(data.get("report_version", 1))
        published_version = int(publication.get("report_version", 1))
        if current_version == published_version:
            self.audit.record(
                event_id,
                actor,
                "reconcile",
                event["status"],
                event["status"],
                {
                    "result": "matched",
                    "communication_id": communication_id,
                    "report_version": current_version,
                },
            )
            return event
        new_data = dict(data)
        new_data["review"] = None
        updated = self.repository.update_entity(event_id, event["version"], "associated", new_data)
        self.audit.record(
            event_id,
            actor,
            "reconcile",
            event["status"],
            updated["status"],
            {
                "result": "mismatch",
                "communication_id": communication_id,
                "expected_report_version": published_version,
                "actual_report_version": current_version,
            },
        )
        return updated

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
