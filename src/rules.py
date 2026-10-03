from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)

# 补报允许发生的事件状态：事件一旦创建，任何阶段都允许台站补报。
SUPPLEMENT_STATUSES = (
    "candidate",
    "associated",
    "reviewed",
    "pending_review",
    "published",
    "reconciled",
)
# 在这些状态下，已有复核/发布结论；补报一到，旧结论立即失效。
CONCLUSION_STATUSES = ("reviewed", "published", "reconciled")


def _require_fields(data, fields):
    for field in fields:
        value = data.get(field)
        if value is None or value == "" or value == [] or value == {}:
            raise ValidationError("missing required field: " + field)


def associate_reports(reports, max_delta=120, max_distance=3.0):
    """按时间差/距离阈值筛选同属于一个事件的台站报告。"""
    if not reports:
        return []
    anchor = reports[0]
    result = [anchor]
    for report in reports[1:]:
        if abs(float(report.get("time_offset", 0))) <= max_delta and float(
            report.get("distance_km", 0)
        ) <= max_distance:
            result.append(report)
    return result


def magnitude_median(amplitudes):
    values = sorted(float(value) for value in amplitudes if value is not None)
    if not values:
        return None
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    return (values[middle - 1] + values[middle]) / 2.0


def reports_station_count(reports):
    """事件采用的报告集合中，不同台站的数量（同一台站多版只算一个台）。"""
    return len({entry.get("station_code") for entry in reports.values()})


def recompute_magnitude(reports):
    """依据当前最新报告集合重算震级：取各台站最新一版振幅的中位数。"""
    amplitudes = []
    for entry in reports.values():
        amplitude = entry.get("amplitude")
        if amplitude is not None:
            amplitudes.append(amplitude)
    return magnitude_median(amplitudes)


def next_supplement_status(current_status):
    """补报写入后的事件状态：带结论的状态一律退回待复核。"""
    if current_status in CONCLUSION_STATUSES:
        return "pending_review", current_status in ("published", "reconciled")
    return current_status, False


def find_station(lookup, station_code):
    rows = lookup("station", "code", station_code) or []
    return rows[0] if rows else None


def assert_station_ownership(actor, station_code, lookup):
    """台站角色只能为归属自己的台站补报，越权直接拒绝。"""
    if actor.role == "admin":
        return
    if actor.role != "station":
        raise PermissionDenied(
            "role %s is not allowed to file station reports" % actor.role
        )
    station = find_station(lookup, station_code)
    if not station:
        raise PermissionDenied(
            "station %s is not registered for this actor" % station_code
        )
    if station.get("created_by") != actor.user_id:
        raise PermissionDenied(
            "actor %s may only file reports for its own station %s"
            % (actor.user_id, station_code)
        )


def _validate_station_create(actor, data, lookup):
    _require_fields(data, ("code",))
    # 台站角色只能注册自己的台站，且 user_id 与台站代号一一对应。
    if actor.role == "station":
        existing = find_station(lookup, data["code"])
        if existing and existing.get("created_by") != actor.user_id:
            raise PermissionDenied("station code is already claimed")


def _validate_event_create(actor, data, lookup):
    _require_fields(data, ("title", "origin_time", "location"))
    for entry in data.get("reports") or []:
        if not entry.get("station_code") and not entry.get("station"):
            raise ValidationError("every seed report needs a station_code")


def _validate_associate(actor, entity, data, lookup):
    reports = entity["data"].get("reports") or {}
    count = reports_station_count(reports)
    if count < 2:
        raise ValidationError(
            "association requires reports from at least two stations, got %s" % count
        )
    return {
        "station_count": count,
        "magnitude": recompute_magnitude(reports),
        "magnitude_basis": "auto",
    }


def _validate_review(actor, entity, data, lookup):
    try:
        magnitude = float(data["magnitude"])
    except (TypeError, ValueError):
        raise ValidationError("magnitude must be numeric")
    return {
        "magnitude": magnitude,
        "magnitude_basis": "manual",
        "reviewer": data["reviewer"],
    }


def _validate_publish(actor, entity, data, lookup):
    communication_id = data["communication_id"]
    if lookup is not None:
        for other in lookup("event", "communication_id", communication_id) or []:
            if other["id"] != entity["id"] and other["status"] in (
                "published",
                "reconciled",
            ):
                raise ConflictError(
                    "communication_id %s is already used by event %s"
                    % (communication_id, other["id"])
                )
    return {}


class RuleEngine:
    ALIASES = {"stations": "station", "events": "event", "reports": "report"}
    INITIAL_STATUS = {"station": "online", "event": "candidate"}

    TRANSITIONS = {
        "station": {
            "offline": (("online",), "offline"),
            "online": (("offline",), "online"),
        },
        "event": {
            "associate": (("candidate", "pending_review"), "associated"),
            "review": (("associated", "pending_review"), "reviewed"),
            "publish": (("reviewed",), "published"),
            "withdraw": (("published", "reconciled"), "withdrawn"),
        },
    }
    CREATE_REQUIRED = {
        "station": ("code", "lat", "lon"),
        "event": ("title", "origin_time", "location"),
    }
    ACTION_REQUIRED = {
        ("station", "offline"): ("reason",),
        ("event", "review"): ("reviewer", "magnitude"),
        ("event", "publish"): ("communication_id",),
        ("event", "reconcile"): ("communication_id", "report_set_version"),
        ("event", "withdraw"): ("reason",),
    }
    CREATE_ROLES = {
        "station": ("admin", "station"),
        "event": ("admin", "analyst"),
    }
    ROLE_ACTIONS = {
        "offline": ("admin", "station"),
        "online": ("admin", "station"),
        "associate": ("admin", "analyst"),
        "review": ("admin", "reviewer"),
        "publish": ("admin", "reviewer"),
        "reconcile": ("admin", "reviewer"),
        "withdraw": ("admin", "reviewer"),
    }

    CUSTOM_CREATE = {
        "station": _validate_station_create,
        "event": _validate_event_create,
    }
    CUSTOM_TRANSITIONS = {
        ("event", "associate"): _validate_associate,
        ("event", "review"): _validate_review,
        ("event", "publish"): _validate_publish,
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        _require_fields(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = self.CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(action, ("admin",))
        self._ensure_role(actor, allowed_roles)
        _require_fields(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = self.CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch

    # ---- 补报 / 对账联动 -------------------------------------------------

    def validate_supplement(self, event, data):
        station_code = data.get("station_code") or data.get("station")
        if not station_code:
            raise ValidationError("station_code is required")
        amplitude = data.get("amplitude")
        if amplitude is not None:
            try:
                float(amplitude)
            except (TypeError, ValueError):
                raise ValidationError("amplitude must be numeric")
        if event["status"] not in SUPPLEMENT_STATUSES:
            raise InvalidTransition(
                "cannot supplement event from status %s" % event["status"]
            )
        return station_code

    def evaluate_reconciliation(self, actor, entity, data):
        """发布后对账：通信编号与报告集合版本必须同依据，否则退回待复核。"""
        self._ensure_role(actor, self.ROLE_ACTIONS["reconcile"])
        _require_fields(data, ("communication_id", "report_set_version"))
        if entity["status"] != "published":
            raise InvalidTransition(
                "cannot reconcile event from status %s" % entity["status"]
            )
        event_data = entity["data"]
        communication_ok = (
            event_data.get("communication_id") == data["communication_id"]
        )
        try:
            submitted_version = int(data["report_set_version"])
        except (TypeError, ValueError):
            raise ValidationError("report_set_version must be an integer")
        version_ok = event_data.get("report_set_version") == submitted_version
        matched = communication_ok and version_ok
        detail = {
            "communication_id": data["communication_id"],
            "expected_communication_id": event_data.get("communication_id"),
            "report_set_version": submitted_version,
            "expected_report_set_version": event_data.get("report_set_version"),
            "matched": matched,
        }
        return ("reconciled" if matched else "pending_review"), detail
