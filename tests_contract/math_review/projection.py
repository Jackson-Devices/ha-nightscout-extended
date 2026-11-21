"""Collect real HA state and render the checkout's bundled card in the test process."""

import asyncio
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT
from homeassistant.helpers import entity_registry as er

from custom_components.nightscout_extended.const import DOMAIN

_PROBE = Path(__file__).with_name("card_probe.mjs")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_SIGNALS = {
    "glucose", "last_reading", "glucose_delta", "glucose_trend", "iob", "cob",
    "forecast_15", "forecast_30",
}


def capture_states(hass, entry):
    """Project only the public entity and registry fields used by the card."""
    states, entities, entity_ids = {}, {}, {}
    for row in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id):
        key = row.translation_key
        if row.platform != DOMAIN or key not in _SIGNALS:
            continue
        state = hass.states.get(row.entity_id)
        if state is None:
            continue
        entity_ids[key] = row.entity_id
        attributes = dict(state.attributes)
        states[row.entity_id] = {
            "entity_id": row.entity_id,
            "state": state.state,
            "attributes": attributes,
            "unit": attributes.get(ATTR_UNIT_OF_MEASUREMENT),
            "last_changed": state.last_changed.isoformat(),
            "last_updated": state.last_updated.isoformat(),
        }
        entities[row.entity_id] = {
            "platform": row.platform,
            "translation_key": key,
            "device_id": row.device_id,
            "sensor_options": {
                name: value
                for name, value in row.options.get("sensor", {}).items()
                if name in {"unit_of_measurement", "display_precision", "suggested_display_precision"}
            },
        }
    if "glucose" not in entity_ids:
        raise ValueError("The projection needs a loaded Nightscout Extended glucose entity")
    return {"states": states, "entities": entities, "entity_ids": entity_ids}


def _iso_ms(ms):
    return (_EPOCH + timedelta(milliseconds=ms)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _selection(bundle, config):
    config = dict(config or {})
    config.setdefault("night_mode", False)
    entity_id = bundle["entity_ids"]["glucose"]
    if config.setdefault("entity", entity_id) != entity_id:
        raise ValueError("The projection accepts only the captured entry's glucose entity")
    return config


async def render_card(bundle, *, now_ms, config=None, node="node", history=None, history_request=None):
    """Render the pinned card, optionally furnishing the real HA action response."""
    if type(now_ms) is not int:
        raise TypeError("now_ms must be an integer UTC epoch millisecond timestamp")
    config = _selection(bundle, config)
    if history is None:
        config.setdefault("show_chart", False)
    if config.get("show_chart", True) and (history is None or history_request is None):
        raise ValueError("Chart rendering needs the real history action response and request")
    data = json.dumps(
        {"ha": bundle, "config": config, "now_ms": now_ms,
         "history": history, "history_request": history_request},
        ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")
    process = await asyncio.create_subprocess_exec(
        str(node), str(_PROBE), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        output, _stderr = await asyncio.wait_for(process.communicate(data), timeout=20)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            process.kill()
        await process.communicate()
        raise
    if process.returncode:
        raise RuntimeError(f"Card projection process failed (status {process.returncode})")
    return json.loads(output)


async def observe_projection(hass, entry, *, now_ms, config=None, node="node"):
    """Return separate HA observations and card output, with real service history."""
    if type(now_ms) is not int:
        raise TypeError("now_ms must be an integer UTC epoch millisecond timestamp")
    bundle = capture_states(hass, entry)
    config = _selection(bundle, config)
    history = request = None
    if config.get("show_chart", True):
        # Mirror the card's bounded span solely for dispatch. The Node probe
        # checks the actual call against this exact interval before using it.
        hours = min(48, max(1, float(config.get("hours_to_show", 3))))
        request = {
            "entity_id": config["entity"],
            "start": _iso_ms(int(now_ms - hours * 3_600_000)),
            "end": _iso_ms(now_ms),
        }
        history = await hass.services.async_call(
            DOMAIN, "get_glucose_history", request, blocking=True, return_response=True,
        )
    card = await render_card(
        bundle, now_ms=now_ms, config=config, node=node, history=history, history_request=request,
    )
    return {"ha": {**bundle, "history": history}, "card": card}
