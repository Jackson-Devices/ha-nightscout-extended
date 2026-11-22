"""Stock-authority expectations and allowlisted Home Assistant observations."""

from datetime import UTC, datetime, timedelta
import json
import math
import re


DOMAIN = "nightscout_extended"
MODES = {"upstream_legacy", "candidate_legacy", "candidate_v3"}
AGES = {
    "insulin_age": ("Insulin Change",),
    "cannula_age": ("Site Change",),
    "sensor_age": ("Sensor Start", "Sensor Change"),
    "pump_battery_age": ("Pump Battery Change",),
}
THRESHOLDS = {
    "urgent_low": "bgLow", "low": "bgTargetBottom",
    "high": "bgTargetTop", "urgent_high": "bgHigh",
}
TRENDS = {
    "DoubleUp": "double_up", "SingleUp": "single_up", "FortyFiveUp": "forty_five_up",
    "Flat": "flat", "FortyFiveDown": "forty_five_down", "SingleDown": "single_down",
    "DoubleDown": "double_down", "NOT COMPUTABLE": "not_computable",
    "RATE OUT OF RANGE": "rate_out_of_range", "NONE": "none",
}
STATUS = {"unknown", "unavailable"}
UNITS = {"mg/dL", "mmol/L", "U", "g", "U/h", "h"}
COMMON_STATES = (
    "glucose", "glucose_delta", "glucose_trend", "last_reading", "iob", "cob", "basal",
    "last_carbs", "last_insulin", "forecast_15", "forecast_30",
)
FORECAST_ATTRIBUTES = ("source_time", "forecast_time")
PROBE_STATES = (*COMMON_STATES, *AGES, "insulin_remaining")
MISSING = "<missing>"
INVALID = "<invalid>"
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
MILLISECOND = timedelta(milliseconds=1)
SAFE_WORDS = re.compile(r"[A-Za-z0-9 .+&/()_-]{1,80}")
SOURCE_ID = re.compile(r"(?:[a-fA-F0-9]{24}|[a-fA-F0-9-]{36})")


def instant(value):
    """Parse a timezone-aware public or stock timestamp without changing its instant."""
    when = datetime.fromisoformat(value)
    if when.utcoffset() is None:
        raise ValueError("offset required")
    return when.astimezone(UTC)


def _time(value):
    if value is None:
        return None
    try:
        return instant(value).isoformat()
    except (TypeError, ValueError, OverflowError):
        return INVALID


def _number(value):
    if value is None or (type(value) in (int, float) and math.isfinite(value)):
        return value
    return INVALID


def _raw_number(value):
    value = _number(value)
    if value == INVALID:
        raise ValueError("stock quantity must be finite or null")
    return value


def _public_number(value):
    try:
        result = json.loads(value)
    except (TypeError, ValueError):
        return INVALID
    return _number(result) if result is not None else INVALID


def _word(value, allowed):
    if value is None or (isinstance(value, str) and value in allowed):
        return value
    return INVALID


def _safe_words(value):
    if value is None or (isinstance(value, str) and SAFE_WORDS.fullmatch(value)):
        return value
    raise ValueError("stock label outside synthetic fixture vocabulary")


def _fields(source, fields):
    return {key: project(source[key]) for key, project in fields.items() if key in source}


def _record(state, unit=None, **attributes):
    return {"state": state, "unit": unit, "attributes": attributes}


def _quantity(value, unit, *, missing="unknown", **attributes):
    value = _raw_number(value)
    return _record(value if value is not None else missing, unit, **attributes)


def _window(value):
    start, end = map(instant, value)
    if start >= end:
        raise ValueError("invalid observation window")
    return start, end


def _ordered_calendar(rows):
    return sorted(rows, key=lambda row: tuple(str(row.get(key)) for key in ("start", "end", "summary")))


def _calendar_and_intake(authority, now):
    start, end = _window(authority["calendar_window"])
    calendar = []
    intake = {"carbs": [], "insulin": []}
    for event in authority["intake"]:
        at, kind = instant(event["at"]), _safe_words(event["event_type"])
        carbs, insulin = _raw_number(event["carbs"]), _raw_number(event["insulin"])
        duration = _raw_number(event["duration"])
        finish = at + timedelta(minutes=duration if duration is not None and duration > 0 else 5)
        labels = []
        for key, amount, unit in (("carbs", carbs, "g"), ("insulin", insulin, "U")):
            if amount is None or amount == 0:
                continue
            labels.append(f"{amount:g} {unit} {key}")
            if at <= now:
                intake[key].append((at, kind, amount))
        if finish > start and at < end:
            summary = f"{kind}: {', '.join(labels)}" if labels else kind
            calendar.append({"start": at.isoformat(), "end": finish.isoformat(), "summary": summary})
    states = {}
    for key, rows in intake.items():
        unit = "g" if key == "carbs" else "U"
        if not rows:
            states[f"last_{key}"] = _quantity(None, unit, time=None)
            continue
        newest = max(row[0] for row in rows)
        latest = {(kind, amount) for at, kind, amount in rows if at == newest}
        if len(latest) != 1:
            raise ValueError("ambiguous latest stock intake")
        kind, amount = latest.pop()
        states[f"last_{key}"] = _quantity(amount, unit, time=newest.isoformat(), event_type=kind)
    return _ordered_calendar(calendar), states


def _forecast(authority, now, unit):
    anchor = instant(authority["forecast_source_at"]) if authority["forecast_source_at"] is not None else None
    points = []
    for millis, value in authority["ar2_points"]:
        if type(millis) is not int or _raw_number(value) is None:
            raise ValueError("native AR2 points require a millisecond timestamp and value")
        points.append((EPOCH + millis * MILLISECOND, value))
    points.sort()
    if len({when for when, _ in points}) != len(points):
        raise ValueError("duplicate stock AR2 times")
    states = {}
    for horizon in (15, 30):
        selected = None
        if anchor is not None:
            target = anchor + timedelta(minutes=horizon)
            distances = sorted(
                (abs(when - target), when, value) for when, value in points
                if abs(when - target) <= timedelta(minutes=3)
            )
            if len(distances) > 1 and distances[0][0] == distances[1][0]:
                raise ValueError("equidistant stock AR2 points")
            if distances:
                _, when, value = distances[0]
                selected = when, value
        when, value = selected or (None, None)
        attributes = {
            "source_time": anchor.isoformat() if anchor is not None else None,
            "forecast_time": when.isoformat() if when is not None else None,
        }
        if horizon == 30:
            attributes.update(model="AR2", forecast=[{"time": at.isoformat(), "mgdl": bg} for at, bg in points])
        # An expired target is covered by the independent scheduled timeline.
        # The stock snapshot must have a usable target whenever it selected one.
        if when is not None and when <= now:
            raise ValueError("stock AR2 snapshot crossed a selected target")
        states[f"forecast_{horizon}"] = _quantity(value, unit, **attributes)
    return states


def _ages(authority, now):
    maintenance = authority["maintenance"]
    answers = {}
    for key, kinds in AGES.items():
        found = []
        for order, kind in enumerate(kinds):
            raw = maintenance.get(kind) if kind == "Sensor Start" else maintenance[kind]
            if raw is not None:
                when = instant(raw)
                if when <= now:
                    found.append((when, order, kind))
        if found:
            when, _, kind = max(found)
            answers[key] = _quantity((now - when) / timedelta(hours=1), "h", source_time=when.isoformat(), event_type=kind)
        else:
            answers[key] = _quantity(None, "h")
    return answers


def _history_row(row):
    return {
        "id": row.get("id") if isinstance(row.get("id"), str) and SOURCE_ID.fullmatch(row["id"]) else INVALID,
        "date": row.get("date") if type(row.get("date")) is int else INVALID,
        "sgv": _number(row.get("sgv")),
        "direction": _word(row.get("direction"), TRENDS),
    }


def expected_projection(authority, now):
    """Project only independent stock observations and documented public representations."""
    unit = authority.get("glucose_unit", "mg/dL")
    if unit != "mg/dL":
        raise ValueError("stock parity fixture must report mg/dL")
    direction = _word(authority["direction"], TRENDS)
    if direction == INVALID:
        raise ValueError("unknown stock direction")
    sgv_at = instant(authority["sgv_at"])
    calendar, intake = _calendar_and_intake(authority, now)
    reservoir = _raw_number(authority["reservoir"])
    reservoir_attrs = {}
    if reservoir is not None:
        reservoir_attrs["source_time"] = instant(authority["device_at"]).isoformat()
        if "uploader" in authority:
            reservoir_attrs["uploader"] = _safe_words(authority["uploader"])
    iob_attrs = {"source": _safe_words(authority["iob_source"])} if "iob_source" in authority else {}
    cob_attrs = {"source": _safe_words(authority["cob_source"])} if "cob_source" in authority else {}
    start, end = _window(authority["history_window"])
    readings = [_history_row(row) for row in authority["history"]]
    if any(INVALID in row.values() or row["sgv"] is None for row in readings):
        raise ValueError("invalid native history point")
    if readings != sorted(readings, key=lambda row: (row["date"], row["id"])) or any(
        not start <= EPOCH + row["date"] * MILLISECOND < end for row in readings
    ):
        raise ValueError("unordered or out-of-range stock history")
    return {
        "states": {
            "glucose": _quantity(
                authority["sgv"], unit, direction=direction, delta=_raw_number(authority["delta"]),
                thresholds_mgdl={key: _raw_number(authority["thresholds"][wire]) for key, wire in THRESHOLDS.items()},
            ),
            "glucose_delta": _quantity(authority["delta"], unit),
            "glucose_trend": _record(TRENDS.get(direction, "unknown")),
            # HA formats timestamp sensor states at second precision. History retains raw milliseconds.
            "last_reading": _record(sgv_at.replace(microsecond=0).isoformat()),
            "iob": _quantity(authority["iob"], "U", **iob_attrs),
            "cob": _quantity(authority["cob"], "g", **cob_attrs),
            "basal": _quantity(authority["basal"], "U/h"),
            "insulin_remaining": _quantity(reservoir, "U", missing="unavailable", **reservoir_attrs),
            **intake, **_forecast(authority, now, unit), **_ages(authority, now),
        },
        "calendar": calendar,
        "history": {"unit": "mg/dL", "status": "complete", "as_of": now.isoformat(), "readings": readings, "missing_ranges": []},
    }


def _curve(points):
    if not isinstance(points, (list, tuple)) or any(not isinstance(row, dict) for row in points):
        return INVALID
    return [_fields(row, {"time": _time, "mgdl": _number}) for row in points]


def observe_state(role, state, reference):
    """Read only named public state and attribute fields from the real HA state machine."""
    if state is None:
        return None
    attrs = state.attributes
    fields = {
        "direction": lambda value: _word(value, TRENDS),
        "delta": _number,
        "thresholds_mgdl": lambda value: _fields(value, dict.fromkeys(THRESHOLDS, _number)) if isinstance(value, dict) else INVALID,
        "source_time": _time, "forecast_time": _time, "time": _time,
        "model": lambda value: _word(value, {"AR2"}),
        "forecast": _curve,
        "event_type": lambda value: _word(value, {reference.get("event_type")}),
        "source": lambda value: _word(value, {reference.get("source")}),
        "uploader": lambda value: _word(value, {reference.get("uploader")}),
    }
    value = state.state
    if value not in STATUS:
        if role == "last_reading":
            value = _time(value)
        elif role == "glucose_trend":
            value = _word(value, TRENDS.values())
        else:
            value = _public_number(value)
    return _record(value, _word(attrs.get("unit_of_measurement"), UNITS), **_fields(attrs, fields))


def observe_calendar(rows, reference):
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return INVALID
    summaries = {row["summary"] for row in reference}
    return _ordered_calendar([
        _fields(row, {"start": _time, "end": _time, "summary": lambda value: _word(value, summaries)})
        for row in rows
    ])


def observe_history(history):
    if not isinstance(history, dict):
        return INVALID
    def readings(rows):
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            return INVALID
        return [_history_row(row) for row in rows]

    def gaps(rows):
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            return INVALID
        return [_fields(row, {"start": _number, "end": _number}) for row in rows]

    return _fields(history, {
        "unit": lambda value: _word(value, {"mg/dL", "mmol/L"}),
        "status": lambda value: _word(value, {"complete", "partial", "unavailable"}),
        "as_of": _time, "readings": readings, "missing_ranges": gaps,
    })


def compare(expected, actual, path=""):
    """Emit field-level differences with already-projected expected and actual values."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        return [
            difference
            for name, value in expected.items()
            for difference in compare(value, actual.get(name, MISSING), f"{path}.{name}".lstrip("."))
        ]
    if expected == actual:
        return []
    return [{"path": path, "expected": expected, "actual": actual}]


def compare_mode(expected, actual, mode, history_supported):
    """The historical subject may lack explicitly identified additive capabilities."""
    if mode != "upstream_legacy":
        return compare(expected, actual), []
    expected = {**expected, "states": dict(expected["states"])}
    absent = []
    if not history_supported:
        del expected["history"]
        absent.append("get_glucose_history")
    for role in (*AGES, "insulin_remaining"):
        if actual["states"].get(role) is None:
            del expected["states"][role]
            absent.append(role)
    for role in ("forecast_15", "forecast_30"):
        observed = actual["states"].get(role)
        if observed is None:
            continue
        state = expected["states"][role]
        attrs = dict(state["attributes"])
        for attr in FORECAST_ATTRIBUTES:
            if attr not in observed["attributes"]:
                del attrs[attr]
                absent.append(f"{role}.{attr}")
        expected["states"][role] = {**state, "attributes": attrs}
    return compare(expected, actual), absent
