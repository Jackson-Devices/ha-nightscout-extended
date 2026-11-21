"""Exercise the product SGV reader against a pinned disposable Nightscout."""

import asyncio
import json
from urllib.parse import quote


ENTRIES = "/api/v3/entries"


def require(value, phase):
    if not value:
        raise AssertionError(f"ordered SGV {phase}")


def reading(row):
    return row["id"], row["date"], row["sgv"], row["direction"]


async def expect_range(source, start, end, expected):
    rows = await source.read_range(start, end)
    require([reading(row) for row in rows] == sorted(map(tuple, expected), key=lambda row: (row[1], row[0])),
            "measurement range identity/order")


async def ordered_ranges(source, fixture):
    recent = fixture["recent"]
    start, end = fixture["window"]
    far = fixture["far"]
    await expect_range(source, start, end, recent)
    await expect_range(source, start, recent[-1][1], recent[:-1])
    await expect_range(source, far[1], far[1] + 60_000, [far])
    print(json.dumps({"ordered_sgv_range": {
        "mode": "v3" if source.uses_v3 else "legacy", "recent_rows": len(recent),
        "half_open": True, "on_demand_older_than_four_days": True,
    }}))


async def prove_legacy_sgv(client, fixture):
    from custom_components.nightscout_extended.sgv_source import NightscoutSGVSource

    source = NightscoutSGVSource(client)
    require(not source.uses_v3 and await source.change_fence() is None, "legacy mode")
    await ordered_ranges(source, fixture)


async def expect_change(source, cursor, expected, *, deleted=False, native_stamp=None):
    changes, next_cursor = await source.read_changes(cursor)
    require(len(changes) == 1, "history mutation count")
    row = changes[0]
    if deleted:
        require(row.get("deleted") is True and row["id"] == expected, "history tombstone identity")
    else:
        require(reading(row) == expected, "history source reading")
    modified = row["modified"]
    require(type(modified) is int and modified > cursor and next_cursor == modified, "history storage clock")
    if native_stamp is not None:
        require(modified == native_stamp, "native insertion storage clock")
    return next_cursor


async def prove_v3_sgv(client, fixture, mutate):
    from custom_components.nightscout_extended.sgv_source import NightscoutSGVSource

    source = NightscoutSGVSource(client)
    require(source.uses_v3, "v3 mode")
    fence = await source.change_fence()
    require(type(fence) is int and fence > 0, "server change fence")
    await ordered_ranges(source, fixture)
    require(await source.read_changes(fence) == ([], None), "empty history poll")

    # Mutate the source only after the reader has finished its initial SEARCH.
    # Let the real server clock cross a millisecond before each native write.
    fixture_row = fixture["late"]
    await asyncio.sleep(0.02)
    created = await mutate("POST", ENTRIES, fixture_row["document"])
    item_id = created["identifier"]
    late = (item_id, fixture_row["document"]["date"], fixture_row["document"]["sgv"],
            fixture_row["document"]["direction"])
    cursor = await expect_change(source, fence, late, native_stamp=created["lastModified"])

    start, end = fixture["window"]
    recent = fixture["recent"]
    await expect_range(source, start, end, [late, *recent])
    # Same-ID native PATCH changes the value. Its measurement date is immutable.
    route = f"{ENTRIES}/{quote(item_id, safe='')}"
    await asyncio.sleep(0.02)
    await mutate("PATCH", route, {"sgv": fixture_row["edited_sgv"]})
    edited = (late[0], late[1], fixture_row["edited_sgv"], late[3])
    cursor = await expect_change(source, cursor, edited)
    await expect_range(source, start, end, [edited, *recent])

    await asyncio.sleep(0.02)
    await mutate("DELETE", route, None)
    await expect_change(source, cursor, item_id, deleted=True)
    await expect_range(source, start, end, recent)
    print(json.dumps({"ordered_sgv_history": {
        "empty_poll": True, "older_backfill_preserves_newest": True,
        "value_edit": True, "tombstone": True,
    }}))
