"""Synthetic wire regressions for the raw Nightscout SGV source adapter."""

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

# Like test_api.py, load the pure client without importing Home Assistant.
_ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "nightscout_extended"
_PACKAGE = "_ns_sgv_test"
_package = ModuleType(_PACKAGE)
_package.__path__ = [str(_ROOT)]
sys.modules[_PACKAGE] = _package


def _load(module):
    name = f"{_PACKAGE}.{module}"
    spec = importlib.util.spec_from_file_location(name, _ROOT / f"{module}.py")
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


api = _load("api")
wire = _load("sgv_source")
BASE = 1_700_000_000_000


def _entry(key, date, sgv=105, **fields):
    return {"identifier": key, "date": date, "type": "sgv", "sgv": sgv, **fields}


def _result(body):
    return {"status": 200, "result": body}


class _Client:
    url = "https://example.invalid/nightscout"

    def __init__(self, *, v3=True):
        self._use_v3 = v3
        self.snapshot = []
        self.history = []
        self.cap = 1000
        self.fence = {"srvDate": BASE, "collections": {"entries": BASE + 999_000}}
        self.calls = []
        self.fail_history_after = None
        self.shift_range_tie = False
        self.overwrite_date_bounds = False
        self.reverse_descending_page = False

    async def _protected_v3(self, path, query):
        self.calls.append((path, query))
        if path == "/api/v3/lastModified":
            return _result(self.fence)
        limit = int(query["limit"])
        if path == "/api/v3/entries":
            if limit > self.cap:
                raise api._V3PageLimitError("limit", status=400)
            assert query["type"] == "sgv"
            skip = int(query["skip"])
            if "date" in query:
                assert "date$lt" not in query and "date$gte" not in query
                assert query["sort"] == "_id"
                date = int(query["date"])
                matches = [row for row in self.snapshot if row["date"] == date]
                matches.sort(key=lambda row: row.get("_id", row.get("identifier")))
                if skip and self.shift_range_tie:
                    matches.pop(0)
            else:
                assert skip == 0
                upper = int(query["date$lt"])
                matches = [row for row in self.snapshot if row["date"] < upper]
                if "date$gte" in query:
                    assert query["sort"] == "date"
                    # Nightscout 15.0.6 applies the last date predicate only.
                    if not self.overwrite_date_bounds:
                        lower = int(query["date$gte"])
                        matches = [row for row in matches if row["date"] >= lower]
                else:
                    assert query["sort$desc"] == "date" and "sort" not in query
                    matches.sort(key=lambda row: row["date"], reverse=not self.reverse_descending_page)
            return _result(matches[skip:skip + limit])
        assert path.startswith("/api/v3/entries/history/")
        assert set(query) == {"limit"}
        after = int(path.rsplit("/", 1)[-1])
        if after == self.fail_history_after:
            raise api.NightscoutConnectionError("Synthetic interrupted page")
        if limit > self.cap:
            raise api.NightscoutConnectionError("limit", status=400)
        return _result([row for row in self.history if row["srvModified"] > after][:limit])

    async def _get(self, path, query):
        self.calls.append((path, query))
        assert path == "/api/v1/entries.json"
        assert set(query) == {"find[type]", "find[date][$gte]", "find[date][$lt]", "count"}
        assert query["find[type]"] == "sgv"
        lower, upper = (int(query[key]) for key in ("find[date][$gte]", "find[date][$lt]"))
        matches = sorted(
            (row for row in self.snapshot if lower <= row["date"] < upper),
            key=lambda row: row["date"], reverse=True,
        )
        return matches[:int(query["count"])]


def test_v3_fence_ignores_event_time_hint_and_empty_history():
    client = _Client()
    source = wire.NightscoutSGVSource(client)

    async def check():
        fence = await source.change_fence()
        assert fence == BASE
        assert await source.read_changes(fence) == ([], None)
        assert await source.read_changes(fence) == ([], None)

    asyncio.run(check())
    assert client.calls == [
        ("/api/v3/lastModified", {}),
        (f"/api/v3/entries/history/{BASE - 60_000}", {"limit": "1000"}),
        (f"/api/v3/entries/history/{BASE - 60_000}", {"limit": "1000"}),
    ]


def test_v3_history_replays_ties_and_out_of_order_measurements(monkeypatch):
    monkeypatch.setattr(wire, "_HISTORY_LIMIT", 8)
    client = _Client()
    client.cap = 3
    client.history = [
        {"type": "mbg", "srvModified": BASE + 10},
        _entry("new", BASE + 80, srvModified=BASE + 11),
        _entry("backfill", BASE - 20, srvModified=BASE + 11),
        {"identifier": "removed", "type": "sgv", "isValid": False, "srvModified": BASE + 12},
        {"type": "cal", "srvModified": BASE + 13},
    ]
    source = wire.NightscoutSGVSource(client)

    async def check():
        changes, cursor = await source.read_changes(BASE)
        assert changes == [
            {"id": "backfill", "date": BASE - 20, "sgv": 105.0, "direction": None, "modified": BASE + 11},
            {"id": "new", "date": BASE + 80, "sgv": 105.0, "direction": None, "modified": BASE + 11},
            {"id": "removed", "modified": BASE + 12, "deleted": True},
        ]
        assert cursor == BASE + 13
        assert await source.read_changes(cursor) == (changes, None)
        client.history.insert(0, _entry("late-visibility", BASE - 10, srvModified=BASE - 1))
        replayed, advanced = await source.read_changes(cursor)
        assert replayed[0]["id"] == "late-visibility"
        assert advanced is None

    asyncio.run(check())
    assert (f"/api/v3/entries/history/{BASE + 10}", {"limit": "3"}) in client.calls


def test_v3_history_source_versions_and_retry_are_independent_of_measurement(monkeypatch):
    monkeypatch.setattr(wire, "_HISTORY_LIMIT", 2)
    client = _Client()
    client.history = [
        _entry("edited", BASE + 30, 100, srvModified=BASE + 1),
        _entry("edited", BASE - 50, 112, srvModified=BASE + 20),
    ]
    source = wire.NightscoutSGVSource(client)
    client.fail_history_after = BASE + 19
    with pytest.raises(api.NightscoutConnectionError, match="interrupted page"):
        asyncio.run(source.read_changes(BASE))
    client.fail_history_after = None

    changes, cursor = asyncio.run(source.read_changes(BASE))
    assert cursor == BASE + 20
    assert [(row["id"], row["date"], row["sgv"]) for row in changes] == [
        ("edited", BASE + 30, 100.0), ("edited", BASE - 50, 112.0),
    ]


def test_v3_history_saturated_clock_requires_authoritative_reconciliation(monkeypatch):
    monkeypatch.setattr(wire, "_HISTORY_LIMIT", 2)
    client = _Client()
    client.history = [
        _entry(key, BASE + 50, srvModified=BASE + 10)
        for key in ("first", "second", "hidden")
    ]
    source = wire.NightscoutSGVSource(client)

    with pytest.raises(wire.SGVReconciliationRequired, match="Saturated"):
        asyncio.run(source.read_changes(BASE))
    assert len(client.calls) == 2


def test_v3_range_splits_an_unordered_full_prefix_and_reads_dense_ties(monkeypatch):
    monkeypatch.setattr(wire, "_V3_PAGE_SIZE", 6)
    client = _Client()
    client.cap = 2
    client.snapshot = [
        _entry("outside-low", BASE - 1),
        _entry("last", BASE + 20),
        _entry("b", BASE + 10, 113, srvModified=BASE + 50, _id="mongo-a"),
        {"_id": "legacy", "date": BASE, "type": "sgv", "sgv": 95, "srvModified": BASE},
        _entry("a", BASE + 10, "5.5", units="mmol/L", direction="Flat", _id="mongo-b"),
        _entry("c", BASE + 10, 116, _id="mongo-c"),
        _entry("outside-high", BASE + 21),
    ]
    source = wire.NightscoutSGVSource(client)
    readings = asyncio.run(source.read_range(BASE, BASE + 21))
    assert [row["id"] for row in readings] == ["legacy", "a", "b", "c", "last"]
    assert readings[0]["date"] == BASE and readings[0]["modified"] is None
    assert readings[1]["sgv"] == 99.088
    assert readings[1]["direction"] == "Flat" and readings[2]["modified"] == BASE + 50
    successful = [query for _, query in client.calls if int(query["limit"]) == client.cap]
    assert successful[0]["date$lt"] == str(BASE + 21)
    assert successful[1]["date$lt"] == str(BASE + 10)
    assert any(
        query["sort"] == "_id" and query["skip"] == "1"
        and query["date"] == str(BASE + 10)
        for query in successful
    )


def test_v3_range_accepts_an_unordered_short_whole_window():
    client = _Client()
    client.snapshot = [_entry("late", BASE + 10), _entry("early", BASE)]
    source = wire.NightscoutSGVSource(client)

    readings = asyncio.run(source.read_range(BASE, BASE + 11))
    assert [(row["id"], row["date"]) for row in readings] == [
        ("early", BASE), ("late", BASE + 10),
    ]
    assert len(client.calls) == 1


def test_v3_range_refuses_a_shifted_same_timestamp_offset(monkeypatch):
    monkeypatch.setattr(wire, "_V3_PAGE_SIZE", 2)
    client = _Client()
    client.snapshot = [_entry(key, BASE) for key in ("first", "middle", "third")]
    client.shift_range_tie = True
    source = wire.NightscoutSGVSource(client)

    with pytest.raises(api.NightscoutConnectionError, match="Unstable.*timestamp"):
        asyncio.run(source.read_range(BASE, BASE + 1))


def test_v3_range_overwritten_lower_bound_restarts_capped_read(monkeypatch):
    monkeypatch.setattr(wire, "_V3_PAGE_SIZE", 2)
    client = _Client()
    client.overwrite_date_bounds = True
    client.snapshot = [
        _entry("five-days-old", BASE - 5 * 86_400_000),
        _entry("four-days-old", BASE - 4 * 86_400_000),
        _entry("late", BASE + 20),
        _entry("tie-c", BASE + 10),
        _entry("lower", BASE),
        _entry("tie-b", BASE + 10),
        _entry("at-exclusive-upper", BASE + 21),
        _entry("tie-a", BASE + 10),
    ]
    source = wire.NightscoutSGVSource(client)

    readings = asyncio.run(source.read_range(BASE, BASE + 21))
    assert [row["id"] for row in readings] == ["lower", "tie-a", "tie-b", "tie-c", "late"]
    queries = [query for _, query in client.calls]
    assert queries[0]["date$gte"] == str(BASE)
    assert queries[1]["date$lt"] == str(BASE + 21) and queries[1]["sort$desc"] == "date"
    assert [query["skip"] for query in queries if query.get("date") == str(BASE + 10)] == ["0", "1", "2"]
    assert queries[-1]["date$lt"] == str(BASE + 10)
    assert all("date$gte" not in query for query in queries[1:])

    # Even a capped page entirely below the lower bound proves empty when
    # Nightscout sorted it by descending date under the strict upper bound.
    prior = len(queries)
    assert asyncio.run(source.read_range(BASE - 1000, BASE)) == []
    assert len(client.calls) == prior + 1
    assert client.calls[-1][1]["sort$desc"] == "date"


def test_v3_range_never_accepts_a_full_unordered_compatibility_page(monkeypatch):
    monkeypatch.setattr(wire, "_V3_PAGE_SIZE", 2)
    client = _Client()
    client.overwrite_date_bounds = True
    client.reverse_descending_page = True
    client.snapshot = [
        _entry("old", BASE - 10),
        _entry("inside", BASE + 10),
        _entry("would-be-hidden", BASE + 20),
    ]
    source = wire.NightscoutSGVSource(client)

    with pytest.raises(api.NightscoutConnectionError, match="Unordered.*compatibility"):
        asyncio.run(source.read_range(BASE, BASE + 21))


def test_legacy_reads_old_exact_window_and_expands_timestamp_cohort(monkeypatch):
    monkeypatch.setattr(wire, "_V1_PAGE_SIZE", 2)
    monkeypatch.setattr(wire, "_V1_TIE_LIMIT", 4)
    client = _Client(v3=False)
    old = BASE - 10 * 86_400_000
    client.snapshot = [
        {"_id": "oldest", "date": old, "type": "sgv", "sgv": 98},
        _entry("b", old + 1),
        _entry("a", old + 1),
        _entry("c", old + 1),
        _entry("outside", old + 2),
    ]
    source = wire.NightscoutSGVSource(client)

    async def check():
        assert not source.uses_v3
        assert await source.change_fence() is None
        assert await source.read_changes(BASE) == ([], None)
        readings = await source.read_range(old, old + 2)
        assert [row["id"] for row in readings] == ["oldest", "a", "b", "c"]
        assert [row["date"] for row in readings] == [old, old + 1, old + 1, old + 1]

    asyncio.run(check())
    assert any(query["count"] == "4" and query["find[date][$gte]"] == str(old + 1)
               for _, query in client.calls)
    monkeypatch.setattr(wire, "_V1_TIE_LIMIT", 3)
    with pytest.raises(api.NightscoutConnectionError, match="Saturated legacy"):
        asyncio.run(source.read_range(old, old + 2))


def test_missing_sgv_identity_fails_the_whole_range():
    client = _Client()
    client.snapshot = [_entry("intact", BASE), _entry(None, BASE + 20)]
    source = wire.NightscoutSGVSource(client)
    with pytest.raises(api.NightscoutConnectionError, match="source identity"):
        asyncio.run(source.read_range(BASE, BASE + 30))


def test_fence_fallback_and_raw_auth_remain_separate():
    class Restricted(_Client):
        async def _protected_v3(self, path, query):
            if path == "/api/v3/lastModified":
                raise api.NightscoutConnectionError("Unavailable", status=404)
            raise api.NightscoutAuthError("Restricted", status=403)

        async def _get(self, path, query):
            pytest.fail("A v3 entries denial must not fall back to legacy transport")

    source = wire.NightscoutSGVSource(Restricted())
    assert asyncio.run(source.change_fence()) is None
    with pytest.raises(api.NightscoutAuthError):
        asyncio.run(source.read_range(BASE, BASE + 30))


def test_v3_range_skips_invalid_amounts_and_fractional_times():
    client = _Client()
    client.snapshot = [
        _entry("valid-a", BASE, 101),
        _entry("bad-amount", BASE + 1, "invalid"),
        _entry("bad-time", BASE + 1.5, 111),
        {"identifier": "other-kind", "type": "mbg", "date": BASE + 2},
        _entry("valid-b", BASE + 3, 104),
    ]
    source = wire.NightscoutSGVSource(client)
    readings = asyncio.run(source.read_range(BASE, BASE + 4))
    assert [(row["id"], row["sgv"]) for row in readings] == [
        ("valid-a", 101.0), ("valid-b", 104.0),
    ]


@pytest.mark.parametrize("v3", [False, True])
def test_read_range_canonicalizes_only_explicit_mmol_sgv(v3):
    client = _Client(v3=v3)
    client.snapshot = [
        _entry("explicit", BASE, "5.5", units="mmol/L"),
        _entry("precise", BASE + 1, 5.51, units="MMOL"),
        _entry("mgdl", BASE + 2, 99.08, units="mg/dL"),
        _entry("implicit", BASE + 3, 99.09),
        _entry("overflow", BASE + 4, "1e308", units="mmol/L"),
        _entry("invalid-type", BASE + 5, True, units="mmol/L"),
        _entry("nonfinite", BASE + 6, "inf", units="mmol/L"),
    ]
    source = wire.NightscoutSGVSource(client)

    readings = asyncio.run(source.read_range(BASE, BASE + 7))
    assert [(row["id"], row["date"], row["sgv"]) for row in readings] == [
        ("explicit", BASE, 99.088),
        ("precise", BASE + 1, 99.26816),
        ("mgdl", BASE + 2, 99.08),
        ("implicit", BASE + 3, 99.09),
    ]


def test_v3_history_canonicalizes_explicit_mmol_sgv_at_source_measurement_time():
    client = _Client()
    client.history = [_entry("edited", BASE - 3, 5.5, units="mmol", srvModified=BASE + 1)]
    source = wire.NightscoutSGVSource(client)

    changes, cursor = asyncio.run(source.read_changes(BASE))
    assert changes == [
        {"id": "edited", "date": BASE - 3, "sgv": 99.088, "direction": None, "modified": BASE + 1},
    ]
    assert cursor == BASE + 1


def test_v3_history_invalid_corrections_do_not_hide_valid_siblings():
    client = _Client()
    client.history = [
        _entry("edited", BASE - 1, 99, srvModified=BASE + 1),
        _entry("edited", BASE - 1, float("inf"), srvModified=BASE + 2),
        {"identifier": "missing-date", "type": "sgv", "sgv": 100, "srvModified": BASE + 3},
        _entry("sibling", BASE - 2, 104, srvModified=BASE + 4),
        {"identifier": "deleted", "type": "sgv", "isValid": False, "srvModified": BASE + 5},
    ]
    source = wire.NightscoutSGVSource(client)
    changes, cursor = asyncio.run(source.read_changes(BASE))
    assert changes == [
        {"id": "edited", "date": BASE - 1, "sgv": 99.0, "direction": None, "modified": BASE + 1},
        {"id": "edited", "modified": BASE + 2, "invalid": True},
        {"id": "missing-date", "modified": BASE + 3, "invalid": True},
        {"id": "sibling", "date": BASE - 2, "sgv": 104.0, "direction": None, "modified": BASE + 4},
        {"id": "deleted", "modified": BASE + 5, "deleted": True},
    ]
    assert cursor == BASE + 5
