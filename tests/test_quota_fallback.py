"""Regression tests for quota-safe cached and local fan operation."""

import unittest
from unittest.mock import AsyncMock, Mock, patch

from custom_components.atomberg import _devices_from_registry
from custom_components.atomberg.api import CloudApiQuotaExceeded
from custom_components.atomberg.device import AtombergDevice

OFFICE_DEVICE_ID = "50787d8798cc"
OFFICE_MAC = "50:78:7d:87:98:cc"
OFFICE_IP = "192.168.4.141"


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

    async def test_cloud_quota_falls_back_to_local_udp(self):
        """A circuit-broken cloud command must use the known LAN address."""
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
                "ip_address": OFFICE_IP,
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
        udp_socket.sendto.assert_called_once()


if __name__ == "__main__":
    unittest.main()
