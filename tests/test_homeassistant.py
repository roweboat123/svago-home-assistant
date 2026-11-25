"""Platform and setup checks using actual Home Assistant 2026.10 classes."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.const import CONF_ADDRESS
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError

from custom_components import svago
from custom_components.svago.button import SvagoButton
from custom_components.svago.config_flow import SvagoConfigFlow
from custom_components.svago.controller import ControlError
from custom_components.svago.select import MassageSelect
from custom_components.svago.sensor import PositionSensor
from custom_components.svago.switch import HeatSwitch


def entry_stub():
    return SimpleNamespace(
        data={CONF_ADDRESS: "02:00:00:00:00:AB"},
        entry_id="test-svago-entry",
        async_on_unload=Mock(),
    )


async def test_setup_and_unload_wire_platforms_and_cleanup(monkeypatch):
    monkeypatch.setattr(svago, "async_remove_retired_entities", Mock())
    connection = SimpleNamespace(
        start=AsyncMock(), close=AsyncMock(), notify_listeners=Mock(), load_preferences=AsyncMock()
    )
    monkeypatch.setattr(svago, "Connection", Mock(return_value=connection))
    memories = SimpleNamespace(
        async_load=AsyncMock(),
        async_import_missing=AsyncMock(),
        async_restore_once=AsyncMock(),
        async_remove=AsyncMock(),
    )
    monkeypatch.setattr(svago, "MemoryStore", Mock(return_value=memories))
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_forward_entry_setups=AsyncMock(),
            async_unload_platforms=AsyncMock(return_value=True),
        ),
        bus=SimpleNamespace(async_listen_once=Mock()),
    )
    entry = entry_stub()
    assert await svago.async_setup_entry(hass, entry)
    assert entry.runtime_data is connection
    svago.async_remove_retired_entities.assert_called_once_with(hass, entry)
    connection.start.assert_awaited_once()
    connection.load_preferences.assert_awaited_once_with(entry.entry_id)
    memories.async_load.assert_awaited_once()
    assert set(hass.config_entries.async_forward_entry_setups.call_args.args[1]) == {
        "button",
        "number",
        "select",
        "sensor",
        "switch",
    }
    assert await svago.async_unload_entry(hass, entry)
    connection.close.assert_awaited_once()
    memories.async_remove.assert_not_awaited()


async def test_setup_failure_cleans_connection_for_retry(monkeypatch):
    connection = SimpleNamespace(
        start=AsyncMock(side_effect=ControlError("absent")),
        close=AsyncMock(),
        notify_listeners=Mock(),
        load_preferences=AsyncMock(),
    )
    monkeypatch.setattr(svago, "Connection", Mock(return_value=connection))
    memories = SimpleNamespace(
        async_load=AsyncMock(),
        async_import_missing=AsyncMock(),
        async_restore_once=AsyncMock(),
        async_remove=AsyncMock(),
    )
    monkeypatch.setattr(svago, "MemoryStore", Mock(return_value=memories))
    entry = entry_stub()
    with pytest.raises(ConfigEntryNotReady):
        await svago.async_setup_entry(Mock(), entry)
    assert entry.runtime_data is connection
    connection.close.assert_awaited_once()


async def test_entities_reflect_chair_and_turn_stale_unavailable(chair):
    connection = SimpleNamespace(controller=chair.controller, address="02:00:00:00:00:AB")
    heat = HeatSwitch(connection, "heat", "Heat")
    massage = MassageSelect(connection, "massage", "Massage")
    position = PositionSensor(connection, 0, "headrest")
    assert heat.is_on is True
    assert massage.current_option == "Off"
    assert position.native_value == 5
    await heat.async_turn_off()
    await massage.async_select_option("Vigorous")
    assert heat.is_on is False and massage.current_option == "Vigorous"
    chair.time += 4
    assert not heat.available and not massage.available and not position.available
    button = SvagoButton(connection, "head_in", "Headrest inward", 0, 1)
    with pytest.raises(HomeAssistantError, match="Fresh"):
        await button.async_press()


async def test_config_flow_rejects_bad_address():
    flow = SvagoConfigFlow()
    result = await flow.async_step_user({CONF_ADDRESS: "not-an-address"})
    assert result["errors"] == {CONF_ADDRESS: "invalid_address"}


async def test_config_flow_connection_test_is_read_only(monkeypatch):
    from custom_components.svago import config_flow

    connection = SimpleNamespace(start=AsyncMock(), close=AsyncMock())
    monkeypatch.setattr(config_flow, "Connection", Mock(return_value=connection))
    flow = SvagoConfigFlow()
    flow.hass = Mock()
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = Mock()
    result = await flow.async_step_user({CONF_ADDRESS: "02:00:00:00:00:ab"})
    assert result["type"] == "create_entry"
    assert result["data"][CONF_ADDRESS] == "02:00:00:00:00:AB"
    connection.start.assert_awaited_once_with(monitor=False)
    connection.close.assert_awaited_once()


async def test_real_ha_loader_discovers_integration_and_all_platforms(tmp_path):
    from homeassistant import loader
    from homeassistant.core import HomeAssistant

    hass = HomeAssistant(str(tmp_path))
    loader.async_setup(hass)
    integration = await loader.async_get_integration(hass, "svago")
    assert integration.version == "1.1.0rc8"
    assert integration.config_flow
    assert "svago" in await loader.async_get_config_flows(hass)
    await integration.async_get_component()
    for platform in (
        "button",
        "number",
        "select",
        "sensor",
        "switch",
        "config_flow",
        "diagnostics",
    ):
        await integration.async_get_platform(platform)


@pytest.mark.parametrize(
    "reason", ["No available connection slot", "Bluetooth maximum command size is 20 bytes"]
)
async def test_setup_form_displays_and_logs_the_specific_failure(monkeypatch, caplog, reason):
    from custom_components.svago import config_flow

    connection = SimpleNamespace(
        start=AsyncMock(side_effect=ControlError(reason)), close=AsyncMock()
    )
    monkeypatch.setattr(config_flow, "Connection", Mock(return_value=connection))
    flow = SvagoConfigFlow()
    flow.hass = Mock()
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = Mock()
    result = await flow.async_step_user({CONF_ADDRESS: "02:00:00:00:00:AB"})
    assert result["type"] == "form"
    assert result["errors"]["base"] == "cannot_connect"
    assert result["description_placeholders"]["error_detail"] == reason
    assert reason in caplog.text
    connection.close.assert_awaited_once()


async def test_manual_setup_has_no_personal_address_default():
    flow = SvagoConfigFlow()
    result = await flow.async_step_user()
    assert result["data_schema"]({}) == {CONF_ADDRESS: ""}


@pytest.mark.parametrize("address", ["02:00:00:00:00:11", "02:00:00:00:00:22"])
async def test_discovery_prefills_each_chairs_own_address(address):
    flow = SvagoConfigFlow()
    flow.context = {"source": "bluetooth"}
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = Mock()
    result = await flow.async_step_bluetooth(SimpleNamespace(address=address.lower()))
    assert result["data_schema"]({}) == {CONF_ADDRESS: address}
    flow.async_set_unique_id.assert_awaited_once_with(address)
    assert flow.context["title_placeholders"] == {"name": "Svago Motion Mastery"}
