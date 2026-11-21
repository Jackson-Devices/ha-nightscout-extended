"""Home Assistant wiring for the SGV history response action and options."""

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.auth.permissions.const import POLICY_CONTROL, POLICY_READ
from homeassistant.const import (
    CONF_URL,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Context, HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.exceptions import ServiceValidationError, Unauthorized
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nightscout_extended.api import (
    ForecastPoint,
    NightscoutAuthError,
    NightscoutClient,
    NightscoutConnectionError,
    NightscoutData,
)
from custom_components.nightscout_extended.const import (
    CONF_SCAN_INTERVAL,
    CONF_SGV_HISTORY_DAYS,
    DOMAIN,
    SERVICE_GET_GLUCOSE_HISTORY,
)
from custom_components.nightscout_extended.sgv_source import NightscoutSGVSource

START = "2026-01-08T12:00:00+00:00"
END = "2026-01-08T12:10:00+00:00"
READING = datetime.fromisoformat("2026-01-08T12:05:00+00:00")
READING_MS = int(READING.timestamp() * 1000)
PROJECTION = "sensor.alpha_glucose"


@pytest.fixture(autouse=True)
def sites(monkeypatch):
    """Mock the transport while leaving HA, the source contract and cache wiring active."""
    async def connect(client):
        client._use_v3 = client.url != "https://legacy.example.invalid"
        return {"settings": {"units": "mg/dl"}}

    async def properties(client, *, calculated_only=False):
        assert calculated_only
        anchor = datetime.fromisoformat(START)
        return NightscoutData(
            glucose=120,
            glucose_time=anchor,
            iob=1.5,
            forecast=[ForecastPoint(datetime.fromisoformat("2026-01-08T12:15:00+00:00"), 141)],
        )

    async def read_range(source, start, end):
        if not source.uses_v3:
            raise NightscoutAuthError("Fixture legacy history access denied", status=403)
        if not start <= READING_MS < end:
            return []
        return [{
            "id": "fixture-reading",
            "date": READING_MS,
            "sgv": 130.0,
            "direction": "Flat",
            "modified": READING_MS,
        }]

    monkeypatch.setattr(NightscoutClient, "async_get_status", connect)
    monkeypatch.setattr(NightscoutClient, "async_get_data", properties)
    monkeypatch.setattr(NightscoutClient, "async_get_treatments", AsyncMock(return_value=[]))
    monkeypatch.setattr(NightscoutSGVSource, "read_range", read_range)
    monkeypatch.setattr(NightscoutSGVSource, "change_fence", AsyncMock(return_value=READING_MS))
    monkeypatch.setattr(NightscoutSGVSource, "read_changes", AsyncMock(return_value=([], None)))


def _entry(hass: HomeAssistant, name="Alpha", *, hostname=None, options=None):
    url = f"https://{hostname or name.lower()}.example.invalid"
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=url, title=name, data={CONF_URL: url}, options=options,
    )
    entry.add_to_hass(hass)
    return entry


async def _load(hass: HomeAssistant, entry):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _history(hass: HomeAssistant, entity_id=PROJECTION, *, user_id=None, **bounds):
    return await hass.services.async_call(
        DOMAIN,
        SERVICE_GET_GLUCOSE_HISTORY,
        {"entity_id": entity_id, "start": START, "end": END, **bounds},
        blocking=True,
        context=Context(user_id=user_id),
        return_response=True,
    )


async def test_history_permission_is_read_and_specific_to_requested_site(hass, hass_admin_user, freezer):
    freezer.move_to(END)
    await _load(hass, _entry(hass))
    await _load(hass, _entry(hass, "Beta"))
    with patch.object(
        type(hass_admin_user.permissions), "check_entity", autospec=True,
        side_effect=lambda permissions, entity_id, policy: entity_id == PROJECTION and policy == POLICY_READ,
    ) as check:
        payload = await _history(hass, user_id=hass_admin_user.id)
        assert payload["status"] == "complete" and payload["unit"] == "mg/dL"
        assert payload["readings"] == [{
            "id": "fixture-reading", "date": READING_MS, "sgv": 130.0, "direction": "Flat"
        }]
        assert payload["missing_ranges"] == []
        with pytest.raises(Unauthorized):
            await _history(hass, "sensor.beta_glucose", user_id=hass_admin_user.id)
        policies = [call.args[-1] for call in check.call_args_list]
        assert policies == [POLICY_READ, POLICY_READ]
        assert POLICY_CONTROL not in policies


async def test_history_validates_entity_and_bounds_and_survives_site_unload(hass, freezer):
    freezer.move_to(END)
    alpha = _entry(hass)
    beta = _entry(hass, "Beta")
    await _load(hass, alpha)
    await _load(hass, beta)
    with pytest.raises(ServiceValidationError):
        await _history(hass, "sensor.alpha_glucose_change")
    with pytest.raises(ServiceValidationError):
        await _history(hass, start=END)
    with pytest.raises(ServiceValidationError):
        await _history(hass, start="2026-01-08T12:00:00")
    assert await hass.config_entries.async_unload(alpha.entry_id)
    with pytest.raises(ServiceValidationError):
        await _history(hass)
    assert (await _history(hass, "sensor.beta_glucose"))["status"] == "complete"


async def test_setup_retry_serves_committed_cache_without_network(hass, freezer, monkeypatch):
    freezer.move_to(END)
    entry = _entry(hass)
    await _load(hass, entry)
    committed = await _history(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    fail = AsyncMock(side_effect=NightscoutConnectionError("Fixture status unavailable"))
    no_remote = AsyncMock(side_effect=AssertionError("Recovery action tried a remote range"))
    monkeypatch.setattr(NightscoutClient, "async_get_status", fail)
    monkeypatch.setattr(NightscoutSGVSource, "read_range", no_remote)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is config_entries.ConfigEntryState.SETUP_RETRY

    assert await _history(hass) == committed
    result = await _history(hass, start="2026-01-07T11:59:00+00:00")
    assert result["status"] == "partial"
    assert result["readings"] == committed["readings"]
    assert len(result["missing_ranges"]) == 1
    no_remote.assert_not_awaited()

    await hass.config_entries.async_remove(entry.entry_id)
    with pytest.raises(ServiceValidationError):
        await _history(hass)


async def test_history_days_options_keep_poll_seconds_separate(hass, monkeypatch):
    entry = _entry(hass)
    monkeypatch.setattr(hass.config_entries, "async_reload", AsyncMock())
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    schema = result["data_schema"]
    assert schema({}) == {CONF_SCAN_INTERVAL: 60, CONF_SGV_HISTORY_DAYS: 1}
    for invalid in (0, 31):
        with pytest.raises(InvalidData):
            await hass.config_entries.options.async_configure(
                result["flow_id"], {CONF_SCAN_INTERVAL: 90, CONF_SGV_HISTORY_DAYS: invalid}
            )
    fractional = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 90, CONF_SGV_HISTORY_DAYS: 1.5}
    )
    assert fractional["errors"] == {CONF_SGV_HISTORY_DAYS: "invalid_history_days"}
    accepted = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 90, CONF_SGV_HISTORY_DAYS: 30}
    )
    assert accepted["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_SCAN_INTERVAL: 90, CONF_SGV_HISTORY_DAYS: 30}


async def test_live_v3_projection_and_calculated_failures_are_independent(hass, freezer, monkeypatch):
    freezer.move_to(END)
    entry = _entry(hass)
    await _load(hass, entry)
    glucose = hass.states.get(PROJECTION)
    assert glucose.state == "130"
    assert glucose.attributes["sgv_history_revision"]
    assert hass.states.get("sensor.alpha_last_reading").state == READING.isoformat()
    assert hass.states.get("sensor.alpha_forecast_in_15_minutes").state == "141"
    assert hass.states.get("sensor.alpha_glucose_change").state == STATE_UNKNOWN

    freezer.move_to("2026-01-08T12:11:00+00:00")
    await entry.runtime_data.async_refresh()
    unchanged = hass.states.get(PROJECTION)
    assert unchanged.last_updated == glucose.last_updated
    assert unchanged.attributes["sgv_history_revision"] == glucose.attributes["sgv_history_revision"]

    lost = AsyncMock(side_effect=NightscoutConnectionError("Fixture connection failure"))
    with monkeypatch.context() as ctx:
        ctx.setattr(NightscoutClient, "async_get_data", lost)
        await entry.runtime_data.async_refresh()
        assert hass.states.get(PROJECTION).state == "130"
        assert hass.states.get("sensor.alpha_insulin_on_board").state == STATE_UNAVAILABLE
        assert hass.states.get("sensor.alpha_forecast_in_15_minutes").state == STATE_UNAVAILABLE

    with monkeypatch.context() as ctx:
        ctx.setattr(NightscoutSGVSource, "read_changes", lost)
        await entry.runtime_data.async_refresh()
        assert hass.states.get(PROJECTION).state == STATE_UNAVAILABLE
        assert hass.states.get("sensor.alpha_insulin_on_board").state == "1.5"
        assert hass.states.get("sensor.alpha_forecast_in_15_minutes").state == "141"
        assert (await _history(hass))["readings"]


async def test_legacy_v2_display_keeps_working_when_raw_access_is_denied(hass, freezer):
    freezer.move_to(END)
    await _load(hass, _entry(hass, "Legacy", hostname="legacy"))
    assert hass.states.get("sensor.legacy_glucose").state == "120"
    assert hass.states.get("sensor.legacy_insulin_on_board").state == "1.5"
    result = await _history(hass, "sensor.legacy_glucose")
    assert result["status"] == "unavailable" and result["readings"] == []


async def test_site_outage_leaves_live_entities_unavailable_and_preserves_cache(
    hass, freezer, monkeypatch,
):
    freezer.move_to(END)
    entry = _entry(hass)
    await _load(hass, entry)
    before = await _history(hass)
    failed = AsyncMock(side_effect=NightscoutConnectionError("Fixture site outage"))
    monkeypatch.setattr(NightscoutSGVSource, "read_changes", failed)
    monkeypatch.setattr(NightscoutClient, "async_get_data", failed)
    monkeypatch.setattr(NightscoutClient, "async_get_treatments", failed)
    await entry.runtime_data.async_refresh()

    assert not entry.runtime_data.last_update_success
    assert hass.states.get(PROJECTION).state == STATE_UNAVAILABLE
    assert hass.states.get("sensor.alpha_insulin_on_board").state == STATE_UNAVAILABLE
    assert hass.states.get("calendar.alpha_treatments").state == STATE_UNAVAILABLE
    assert await _history(hass) == before


async def test_loaded_v3_permission_denial_is_scoped(hass, freezer, monkeypatch):
    freezer.move_to(END)
    entry = _entry(hass)
    await _load(hass, entry)
    denied = AsyncMock(side_effect=NightscoutAuthError("Fixture access denied", status=403))
    monkeypatch.setattr(NightscoutClient, "async_get_treatments", denied)
    await entry.runtime_data.async_refresh()

    assert hass.states.get(PROJECTION).state == "130"
    assert hass.states.get("sensor.alpha_insulin_on_board").state == "1.5"
    assert hass.states.get("sensor.alpha_last_insulin").state == STATE_UNAVAILABLE
    assert hass.states.get("calendar.alpha_treatments").state == STATE_UNAVAILABLE
