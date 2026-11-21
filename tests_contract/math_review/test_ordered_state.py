"""Synthetic, shrinkable history scripts against the real HA SGV store.

Only Nightscout transport and the HA path/executor capability are substituted.
The store, its restart path, and the native HA atomic file writer are real.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from unittest.mock import patch

from homeassistant.util.file import WriteError
from hypothesis import example, given, settings, strategies as st
import pytest

from custom_components.nightscout_extended import sgv_history
from custom_components.nightscout_extended.api import NightscoutConnectionError
from custom_components.nightscout_extended.sgv_history import SGVHistory

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
START_MS = 1_737_288_000_123
DAY_MS = 86_400_000
SERVER_ORIGIN = START_MS + 500_000
ENTRY = "synthetic-state-model"


def instant(value: int) -> datetime:
    seconds, remainder = divmod(value, 1000)
    return EPOCH + timedelta(seconds=seconds, microseconds=remainder * 1000)


@dataclass(frozen=True)
class Put:
    identity: str
    offset_ms: int
    glucose: int
    direction: str | None = "Flat"


@dataclass(frozen=True)
class Drop:
    identity: str
    invalid: bool = False


@dataclass(frozen=True)
class Tick:
    elapsed_ms: int = 17
    failure: str = "none"


@dataclass(frozen=True)
class Restart:
    pass


@dataclass(frozen=True)
class _Event:
    identity: str
    version: int
    date: int | None
    glucose: int | None
    direction: str | None = None
    invalid: bool = False

    def envelope(self) -> dict:
        envelope = {"id": self.identity, "modified": self.version}
        if self.glucose is None:
            envelope["invalid" if self.invalid else "deleted"] = True
        else:
            envelope.update(date=self.date, sgv=self.glucose, direction=self.direction)
        return envelope


@dataclass(frozen=True)
class _Config:
    directory: Path

    def path(self, *components: str) -> str:
        return str(self.directory.joinpath(*components))


class _DiskHass:
    def __init__(self, directory: Path) -> None:
        self.config = _Config(directory)
        self.data: dict = {}

    def async_add_executor_job(self, target, *args):
        return asyncio.get_running_loop().run_in_executor(None, target, *args)


class _SyntheticWire:
    """Expose normalized snapshots and replay of the latest source documents."""

    site_id = "synthetic-ns-fixture"

    def __init__(self, uses_v3: bool) -> None:
        self.uses_v3 = uses_v3
        self.latest: dict[str, _Event] = {}
        self.after: list[int] = []
        self.delivered: list[int] = []
        self.ranges: list[tuple[int, int]] = []
        self.replay_overlap = True
        self.fail_poll = False
        self.fail_range = False
        self.failures = 0

    async def read_range(self, start: int, end: int) -> list[dict]:
        self.ranges.append((start, end))
        if self.fail_range or (self.fail_poll and not self.uses_v3):
            self.failures += 1
            raise NightscoutConnectionError("synthetic interrupted range")
        return [
            event.envelope()
            for event in self.latest.values()
            if event.date is not None and start <= event.date < end
        ]

    async def change_fence(self) -> int:
        assert self.uses_v3
        return max((entry.version for entry in self.latest.values()), default=SERVER_ORIGIN)

    async def read_changes(self, after: int) -> tuple[list[dict], int | None]:
        assert self.uses_v3
        self.after.append(after)
        if self.fail_poll:
            self.failures += 1
            raise NightscoutConnectionError("synthetic interrupted HISTORY drain")
        cutoff = after - 60_000 if self.replay_overlap else after
        batch = [
            entry.envelope()
            for entry in reversed(self.latest.values())
            if entry.version > cutoff
        ]
        # Opposite ID order and identical repeated envelopes exercise replay at
        # the normalized store boundary without inventing stale server versions.
        repeats = batch[::2]
        self.delivered.append(len(batch) + len(repeats))
        high = max((item["modified"] for item in batch), default=after)
        return batch + repeats, high if high > after else None


class _SqliteReference:
    """Keep an independent source log and a separately durable local projection."""

    def __init__(self, path: Path, uses_v3: bool) -> None:
        self.path = path
        self.uses_v3 = uses_v3
        self.db = sqlite3.connect(path)
        self.db.executescript(
            """
            CREATE TABLE upload (
                version INTEGER PRIMARY KEY,
                id TEXT NOT NULL,
                measured INTEGER,
                glucose INTEGER,
                direction TEXT
            );
            CREATE INDEX uploaded_id ON upload(id, version);
            CREATE VIEW present AS
                SELECT * FROM upload AS event
                WHERE glucose IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM upload AS later
                    WHERE later.id = event.id AND later.version > event.version
                );
            CREATE TABLE retained (
                id TEXT PRIMARY KEY,
                measured INTEGER NOT NULL,
                glucose INTEGER NOT NULL,
                direction TEXT
            );
            CREATE TABLE checkpoint (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                checked INTEGER NOT NULL,
                low INTEGER NOT NULL,
                high INTEGER NOT NULL,
                write_version INTEGER
            );
            """
        )

    def upload(self, event: _Event) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO upload VALUES (?, ?, ?, ?, ?)",
                (event.version, event.identity, event.date, event.glucose, event.direction),
            )

    def check(self, now: int, *, durable: bool = True) -> None:
        low, high = now - DAY_MS, now + 60_000
        try:
            self.db.execute("BEGIN")
            if self.uses_v3:
                self.db.execute("DELETE FROM retained")
            else:
                self.db.execute(
                    "DELETE FROM retained WHERE measured < ? OR measured >= ?", (low, high)
                )
            self.db.execute(
                """
                INSERT INTO retained
                SELECT id, measured, glucose, direction
                FROM present WHERE measured >= ? AND measured < ?
                ON CONFLICT(id) DO UPDATE SET measured=excluded.measured,
                    glucose=excluded.glucose, direction=excluded.direction
                """,
                (low, high),
            )
            version = (
                self.db.execute(
                    "SELECT COALESCE(MAX(version), ?) FROM upload", (SERVER_ORIGIN,)
                ).fetchone()[0]
                if self.uses_v3
                else None
            )
            self.db.execute(
                "INSERT OR REPLACE INTO checkpoint VALUES (1, ?, ?, ?, ?)",
                (now, low, high, version),
            )
        except sqlite3.Error:
            self.db.rollback()
            raise
        if durable:
            self.db.commit()
        else:
            self.db.rollback()

    def reopen(self) -> None:
        self.db.close()
        self.db = sqlite3.connect(self.path)

    def version(self) -> int | None:
        return self.db.execute("SELECT write_version FROM checkpoint").fetchone()[0]

    def raw(self, start: int, end: int) -> dict:
        checked, low, high = self.db.execute("SELECT checked, low, high FROM checkpoint").fetchone()
        overlap_start, overlap_end = max(start, low), min(end, high)
        readings = [
            {"id": identity, "date": date, "sgv": value, "direction": direction}
            for identity, date, value, direction in self.db.execute(
                """
                SELECT id, measured, glucose, direction FROM retained
                WHERE measured >= ? AND measured < ? ORDER BY measured, id
                """,
                (start, end),
            )
        ]
        if overlap_start >= overlap_end:
            return {
                "readings": [],
                "unit": "mg/dL",
                "status": "unavailable",
                "missing_ranges": [{"start": start, "end": end}],
                "as_of": None,
            }
        gaps = []
        if start < low:
            gaps.append({"start": start, "end": low})
        if high < end:
            gaps.append({"start": high, "end": end})
        return {
            "readings": readings,
            "unit": "mg/dL",
            "status": "partial" if gaps else "complete",
            "missing_ranges": gaps,
            "as_of": instant(checked).isoformat(),
        }

    def current(
        self, now: int, corroboration: tuple[int, int] | None = None
    ) -> tuple[datetime, int, str | None] | None:
        future_date, future_value = corroboration if corroboration is not None else (None, None)
        # Nightscout 15.0.6 lib/plugins/bgnow.js:95-100 applies >39 to the
        # current projection. Finite raw source rows have no such filter.
        observation = self.db.execute(
            """
            SELECT measured, glucose, direction FROM retained
            WHERE glucose > 39 AND (
                measured <= :now OR
                (measured < :skew_end AND measured = :date AND glucose = :value)
            )
            ORDER BY measured DESC, id DESC LIMIT 1
            """,
            {"now": now, "skew_end": now + 60_000, "date": future_date, "value": future_value},
        ).fetchone()
        if observation is None:
            return None
        measured, glucose, direction = observation
        return instant(measured), glucose, direction

    def nearest_future(self, now: int) -> tuple[int, int] | None:
        return self.db.execute(
            """
            SELECT measured, glucose FROM retained WHERE measured > ?
            ORDER BY measured, id LIMIT 1
            """,
            (now,),
        ).fetchone()


class _HistoryRun:
    def __init__(self, directory: Path, uses_v3: bool) -> None:
        self.directory = directory
        self.wire = _SyntheticWire(uses_v3)
        self.reference = _SqliteReference(directory / "reference.sqlite3", uses_v3)
        self.history = self._open_store()
        self.now = START_MS
        self.next_version = SERVER_ORIGIN

    def _open_store(self) -> SGVHistory:
        return SGVHistory(_DiskHass(self.directory), ENTRY, self.wire, 1)

    async def bootstrap(self) -> None:
        await self.history.async_refresh(instant(self.now))
        self.reference.check(self.now)

    async def restart(self) -> None:
        self.reference.reopen()
        self.history = self._open_store()
        await self.history.async_load()

    def stage(self, change: Put | Drop) -> None:
        self.next_version += 1
        entry = (
            _Event(
                change.identity,
                self.next_version,
                self.now + change.offset_ms,
                change.glucose,
                change.direction,
            )
            if isinstance(change, Put)
            else _Event(change.identity, self.next_version, None, None, invalid=change.invalid)
        )
        self.wire.latest[change.identity] = entry
        self.reference.upload(entry)

    async def tick(self, step: Tick) -> None:
        self.now += step.elapsed_ms
        now = instant(self.now)
        revision = self.history.revision
        if step.failure == "none":
            await self.history.async_refresh(now)
            self.reference.check(self.now)
        elif step.failure == "wire":
            before = self.wire.failures
            self.wire.fail_poll = True
            try:
                with pytest.raises(NightscoutConnectionError):
                    await self.history.async_refresh(now)
            finally:
                self.wire.fail_poll = False
            assert self.wire.failures == before + 1
            assert self.history.revision == revision
        else:
            assert step.failure == "disk"
            with patch.object(
                sgv_history, "write_utf8_file_atomic", side_effect=WriteError("synthetic disk fault")
            ) as write:
                with pytest.raises(NightscoutConnectionError):
                    await self.history.async_refresh(now)
            assert write.call_count == 1
            self.reference.check(self.now, durable=False)
            assert self.history.revision == revision

    async def assert_matches_reference(self) -> None:
        start, end = self.now - DAY_MS - 137, self.now + 60_053
        actual = await self.history.async_range(instant(start), instant(end), allow_remote=False)
        assert actual == self.reference.raw(start, end)
        now = instant(self.now)
        assert self.history.select_current(now, None, None) == self.reference.current(self.now)
        possible = self.reference.nearest_future(self.now)
        if possible is not None:
            measured, glucose = possible
            for value in (glucose, glucose + 1):
                assert self.history.select_current(now, instant(measured), value) == (
                    self.reference.current(self.now, (measured, value))
                )


_IDS = st.sampled_from(("a", "b", "c", "d", "e"))
_EVENTS = st.one_of(
    st.builds(
        Put,
        _IDS,
        st.one_of(
            st.sampled_from((-DAY_MS - 1, -DAY_MS, -5007, -137, -1, 0, 59_999, 60_000)),
            st.integers(min_value=-120_000, max_value=75_000),
        ),
        st.one_of(st.sampled_from((0, 38, 39, 40)), st.integers(min_value=41, max_value=399)),
        st.sampled_from(("Flat", "SingleUp", "SingleDown", None)),
    ),
    st.builds(Drop, _IDS, st.booleans()),
    st.builds(
        Tick,
        st.one_of(st.integers(min_value=1, max_value=90_000), st.sampled_from((900_000, DAY_MS + 1))),
        st.sampled_from(("none", "none", "none", "wire", "disk")),
    ),
    st.just(Restart()),
)


async def _run_generated(uses_v3: bool, script: list[Put | Drop | Tick | Restart]) -> None:
    with TemporaryDirectory(prefix="sgv-model-") as directory:
        run = _HistoryRun(Path(directory), uses_v3)
        try:
            await run.bootstrap()
            for step in script:
                if isinstance(step, (Put, Drop)):
                    run.stage(step)
                elif isinstance(step, Tick):
                    await run.tick(step)
                else:
                    assert isinstance(step, Restart)
                    await run.restart()
                await run.assert_matches_reference()
            # Make even a generated script ending with staged uploads observable.
            await run.tick(Tick())
            await run.assert_matches_reference()
        finally:
            run.reference.db.close()


@settings(max_examples=80, deadline=None)
@given(uses_v3=st.booleans(), script=st.lists(_EVENTS, min_size=1, max_size=36))
@example(
    uses_v3=True,
    script=[
        Put("a", -137, 110), Tick(), Drop("a"), Put("b", -5007, 138), Tick(failure="disk"),
        Restart(), Tick(failure="wire"), Tick(), Drop("b", invalid=True), Tick(), Restart(),
    ],
)
@example(
    uses_v3=False,
    script=[
        Put("a", -DAY_MS + 300, 110), Put("b", -304, 120), Tick(), Drop("b"),
        Put("c", -5007, 145), Tick(300), Restart(), Tick(),
    ],
)
def test_state_history(uses_v3, script):
    asyncio.run(_run_generated(uses_v3, script))


def test_exact_quiet_late_and_same_time_history(tmp_path):
    async def scenario() -> None:
        run = _HistoryRun(tmp_path, uses_v3=True)
        try:
            run.stage(Put("latest", -137, 145))
            await run.bootstrap()
            revision = run.history.revision
            run.wire.replay_overlap = False
            for _ in range(2):
                await run.tick(Tick())
                assert run.history.revision == revision
                await run.assert_matches_reference()
            assert run.wire.delivered[-2:] == [0, 0]
            run.wire.replay_overlap = True
            older = START_MS - 5007
            for identity, value in (("b", 132), ("a", 130)):
                run.stage(Put(identity, older - run.now, value))
            await run.tick(Tick())
            await run.assert_matches_reference()
            await run.restart()
            result = await run.history.async_range(instant(older), instant(run.now))
            assert [(row["id"], row["date"], row["sgv"]) for row in result["readings"]] == [
                ("a", older, 130),
                ("b", older, 132),
                ("latest", START_MS - 137, 145),
            ]
            assert run.history.select_current(instant(run.now), None, None) == (
                instant(START_MS - 137), 145, "Flat"
            )
        finally:
            run.reference.db.close()

    asyncio.run(scenario())


def test_durability_failure_preserves_cursor_for_retry(tmp_path):
    async def scenario() -> None:
        run = _HistoryRun(tmp_path, uses_v3=True)
        try:
            await run.bootstrap()
            run.stage(Put("source", -137, 141))
            await run.tick(Tick())
            cursor = run.reference.version()
            assert cursor == SERVER_ORIGIN + 1
            revision = run.history.revision
            # An edit moves the same identity to an older measurement time.
            run.stage(Put("source", -5007, 119, "SingleDown"))
            await run.tick(Tick(failure="disk"))
            assert run.wire.after[-1] == cursor
            await run.assert_matches_reference()
            await run.restart()
            assert run.history.revision == revision
            await run.assert_matches_reference()
            await run.tick(Tick())
            assert run.wire.after[-2:] == [cursor, cursor]
            assert run.history.select_current(instant(run.now), None, None) == (
                instant(START_MS + 17 - 5007), 119, "SingleDown"
            )
            await run.restart()
            await run.assert_matches_reference()
        finally:
            run.reference.db.close()

    asyncio.run(scenario())


def test_legacy_omission_retains_unexpired_observations(tmp_path):
    async def scenario() -> None:
        run = _HistoryRun(tmp_path, uses_v3=False)
        try:
            run.stage(Put("expire", -DAY_MS + 27, 119))
            run.stage(Put("kept", -137, 142))
            await run.bootstrap()
            run.stage(Drop("kept"))
            await run.tick(Tick(13))
            await run.assert_matches_reference()
            run.stage(Put("late", -5007, 132))
            await run.tick(Tick(19))
            await run.restart()
            await run.assert_matches_reference()
            result = await run.history.async_range(
                instant(run.now - DAY_MS), instant(run.now), allow_remote=False
            )
            assert [row["id"] for row in result["readings"]] == ["late", "kept"]
            assert result["status"] == "complete"
            assert run.wire.after == []
        finally:
            run.reference.db.close()

    asyncio.run(scenario())


def test_exact_range_coverage_online_and_offline(tmp_path):
    async def scenario() -> None:
        run = _HistoryRun(tmp_path, uses_v3=True)
        try:
            older = START_MS - DAY_MS - 1437
            run.stage(Put("earlier", older - START_MS, 139))
            run.stage(Put("retained", -137, 145))
            await run.bootstrap()
            start, end = instant(older), instant(START_MS)
            run.wire.fail_range = True
            unavailable = await run.history.async_range(start, end)
            assert unavailable == run.reference.raw(older, START_MS)
            assert run.wire.ranges[-1] == (older, START_MS - DAY_MS)
            run.wire.fail_range = False
            with patch.object(sgv_history.dt_util, "utcnow", return_value=instant(START_MS + 2)):
                connected = await run.history.async_range(start, end)
            assert connected == {
                "readings": [
                    {"id": "earlier", "date": older, "sgv": 139, "direction": "Flat"},
                    {"id": "retained", "date": START_MS - 137, "sgv": 145, "direction": "Flat"},
                ],
                "unit": "mg/dL",
                "status": "complete",
                "missing_ranges": [],
                "as_of": instant(START_MS).isoformat(),
            }
            await run.restart()
            offline = await run.history.async_range(start, end, allow_remote=False)
            assert offline == unavailable
        finally:
            run.reference.db.close()

    asyncio.run(scenario())


def test_raw_sentinel_values_survive_while_current_obeys_source_cutoff(tmp_path):
    async def scenario() -> None:
        run = _HistoryRun(tmp_path, uses_v3=True)
        try:
            run.stage(Put("valid", -137, 110))
            await run.bootstrap()
            for identity, date, value in (
                ("zero", START_MS - 103, 0),
                ("error", START_MS - 95, 38),
                ("edge", START_MS - 61, 39),
            ):
                run.stage(Put(identity, date - run.now, value))
                await run.tick(Tick())
                await run.assert_matches_reference()
                assert run.history.select_current(instant(run.now), None, None) == (
                    instant(START_MS - 137), 110, "Flat"
                )
            run.stage(Put("next-valid", START_MS - 19 - run.now, 40))
            await run.tick(Tick())
            await run.assert_matches_reference()
            raw = await run.history.async_range(instant(START_MS - 137), instant(START_MS))
            assert [(r["date"], r["sgv"]) for r in raw["readings"]] == [
                (START_MS - 137, 110),
                (START_MS - 103, 0),
                (START_MS - 95, 38),
                (START_MS - 61, 39),
                (START_MS - 19, 40),
            ]
            assert run.history.select_current(instant(run.now), None, None) == (
                instant(START_MS - 19), 40, "Flat"
            )
        finally:
            run.reference.db.close()

    asyncio.run(scenario())
