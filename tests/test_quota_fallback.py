"""Regression tests for quota-safe cached and local fan operation."""

import unittest
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.const import STATE_HOME

from custom_components.atomberg import _devices_from_registry
from custom_components.atomberg.api import CloudApiQuotaExceeded
from custom_components.atomberg.coordinator import AtombergDataUpdateCoordinator
from custom_components.atomberg.device import AtombergDevice

OFFICE_DEVICE_ID = "50787d8798cc"
OFFICE_MAC = "50:78:7d:87:98:cc"
OFFICE_IP = "192.168.5.141"


class QuotaFallbackTests(unittest.IsolatedAsyncioTestCase):
    """Verify fans remain available and locally controllable without cloud."""

    def test_registry_cache_uses_unifi_presence(self):
        """Reconstruct Office and attach its current tracker IP."""
        device = Mock()
        device.identifiers = {("atomberg", f"Atomberg.{OFFICE_DEVICE_ID}")}
        device.model = "aris_gladius"
        device.name_by_user = None
        device.name = "Office Fan"
        device_registry = Mock()
        device_registry.devices = {"office": device}

        tracker = Mock()
        tracker.domain = "device_tracker"
        tracker.entity_id = "device_tracker.atomberg"
        entity_registry = Mock()
        entity_registry.entities = {"tracker": tracker}

        tracker_state = Mock()
        tracker_state.state = STATE_HOME
        tracker_state.attributes = {"mac": OFFICE_MAC, "ip": OFFICE_IP}
        hass = Mock()
        hass.states.get.return_value = tracker_state

        with (
            patch(
                "custom_components.atomberg.dr.async_get", return_value=device_registry
            ),
            patch(
                "custom_components.atomberg.er.async_get", return_value=entity_registry
            ),
        ):
            devices = _devices_from_registry(hass)

        assert devices[OFFICE_DEVICE_ID]["name"] == "Office Fan"
        assert devices[OFFICE_DEVICE_ID]["ip_address"] == OFFICE_IP
        assert devices[OFFICE_DEVICE_ID]["state"]["is_online"] is True

    def test_registry_cache_marks_unpowered_fan_offline(self):
        """A cached fan without current network presence must be unavailable."""
        device = Mock()
        device.identifiers = {("atomberg", f"Atomberg.{OFFICE_DEVICE_ID}")}
        device.model = "aris_gladius"
        device.name_by_user = None
        device.name = "Office Fan"
        device_registry = Mock()
        device_registry.devices = {"office": device}
        entity_registry = Mock()
        entity_registry.entities = {}
        hass = Mock()

        with (
            patch(
                "custom_components.atomberg.dr.async_get", return_value=device_registry
            ),
            patch(
                "custom_components.atomberg.er.async_get", return_value=entity_registry
            ),
        ):
            devices = _devices_from_registry(hass)

        assert devices[OFFICE_DEVICE_ID]["state"]["is_online"] is False
        assert "ip_address" not in devices[OFFICE_DEVICE_ID]

    async def test_cloud_quota_falls_back_to_local_udp(self):
        """A circuit-broken cloud command must use the known LAN address."""
        api = Mock()
        api.async_send_command = AsyncMock(side_effect=CloudApiQuotaExceeded)
        hass = Mock()
        tracker_state = Mock()
        tracker_state.state = STATE_HOME
        tracker_state.attributes = {"mac": OFFICE_MAC, "ip": OFFICE_IP}
        hass.states.async_all.return_value = [tracker_state]
        device = AtombergDevice(
            data={
                "device_id": OFFICE_DEVICE_ID,
                "color": "",
                "series": "",
                "model": "aris_gladius",
                "name": "Office Fan",
                "ip_address": "192.168.4.141",
                "state": {"is_online": True, "power": False, "speed": 2},
            },
            api=api,
            hass=hass,
        )
        device._options = {"use_cloud_control": True}
        udp_socket = Mock()
        udp_socket.sendto.return_value = 15
        socket_context = Mock()
        socket_context.__enter__ = Mock(return_value=udp_socket)
        socket_context.__exit__ = Mock(return_value=False)

        with patch(
            "custom_components.atomberg.device.socket.socket",
            return_value=socket_context,
        ):
            changed = await device.async_turn_on()

        assert changed is True
        assert device.state["power"] is True
        udp_socket.sendto.assert_called_once_with(b'{"power": true}', (OFFICE_IP, 5600))

    async def test_cloud_control_is_default(self):
        """Cloud commands are preferred even before options are explicitly saved."""
        api = Mock()
        api.async_send_command = AsyncMock(return_value=True)
        hass = Mock()
        hass.states.async_all.return_value = []
        device = AtombergDevice(
            data={
                "device_id": OFFICE_DEVICE_ID,
                "color": "",
                "series": "",
                "model": "aris_gladius",
                "name": "Office Fan",
                "state": {"is_online": False, "power": False, "speed": 2},
            },
            api=api,
            hass=hass,
        )

        changed = await device.async_turn_on()

        assert changed is True
        api.async_send_command.assert_awaited_once_with(
            OFFICE_DEVICE_ID, {"power": True}
        )

    async def test_unpowered_fan_does_not_use_stale_local_address(self):
        """A quota failure must not send locally when the fan is no longer present."""
        api = Mock()
        api.async_send_command = AsyncMock(side_effect=CloudApiQuotaExceeded)
        hass = Mock()
        hass.states.async_all.return_value = []
        device = AtombergDevice(
            data={
                "device_id": OFFICE_DEVICE_ID,
                "color": "",
                "series": "",
                "model": "aris_gladius",
                "name": "Office Fan",
                "ip_address": "192.168.4.141",
                "state": {"is_online": False, "power": False, "speed": 2},
            },
            api=api,
            hass=hass,
        )
        device._options = {"use_cloud_control": True}

        with (
            patch("custom_components.atomberg.device.socket.socket") as udp_socket,
            pytest.raises(CloudApiQuotaExceeded),
        ):
            await device.async_turn_on()

        udp_socket.assert_not_called()

    def test_tracker_update_restores_powered_fan(self):
        """A late UniFi tracker update must restore a cached fan without cloud I/O."""
        coordinator = object.__new__(AtombergDataUpdateCoordinator)
        device = Mock()
        device.id = OFFICE_DEVICE_ID
        device.mac = OFFICE_MAC
        device.state = {"is_online": True, "power": False, "speed": 2}
        coordinator.devices = [device]
        coordinator._devices_by_mac = {OFFICE_MAC: device}
        coordinator._cloud_state_available = False
        coordinator.async_set_updated_data = Mock()
        state = Mock()
        state.state = STATE_HOME
        state.attributes = {"mac": OFFICE_MAC, "ip": OFFICE_IP}
        event = Mock()
        event.data = {
            "entity_id": "device_tracker.atomberg",
            "old_state": None,
            "new_state": state,
        }

        coordinator._async_handle_tracker_state(event)

        device.update_ip_address.assert_called_once_with(OFFICE_IP)
        device.update_state.assert_called_once_with({"is_online": True})
        coordinator.async_set_updated_data.assert_called_once_with(
            {
                "source": "presence",
                "devices": {OFFICE_DEVICE_ID: device.state},
            }
        )


if __name__ == "__main__":
    unittest.main()
