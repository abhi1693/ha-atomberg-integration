"""Regression tests for immediate state publication after commands."""

import unittest
from unittest.mock import AsyncMock, Mock, patch

from custom_components.atomberg.coordinator import AtombergDataUpdateCoordinator
from custom_components.atomberg.device import (
    ATTR_SLEEP,
    ATTR_TIMER_HOURS,
    ATTR_TIMER_TIME_ELAPSED_MINS,
    AtombergDevice,
)
from custom_components.atomberg.fan import AtombergFanEntity


class CommandStateTests(unittest.IsolatedAsyncioTestCase):
    """Verify a successful command updates Home Assistant immediately."""

    async def test_turn_on_publishes_acknowledged_state(self):
        """Do not wait for the next scheduled cloud poll after a command."""
        entity = object.__new__(AtombergFanEntity)
        entity._device = Mock()
        entity._device.async_turn_on = AsyncMock(return_value=True)
        entity.publish_command_state = Mock()

        await entity.async_turn_on()

        entity.publish_command_state.assert_called_once_with()

    async def test_turn_on_with_percentage_uses_split_device_command(self):
        """A speed selection while off delegates the provider-safe sequence."""
        entity = object.__new__(AtombergFanEntity)
        entity._device = Mock()
        entity._device.async_turn_on_at_speed = AsyncMock(return_value=True)
        entity.publish_command_state = Mock()

        await entity.async_turn_on(percentage=66)

        entity._device.async_turn_on_at_speed.assert_awaited_once_with(4)
        entity.publish_command_state.assert_called_once_with()

    def test_publish_preserves_other_device_states(self):
        """Publishing one command must not discard other fan states."""
        coordinator = object.__new__(AtombergDataUpdateCoordinator)
        coordinator.data = {
            "source": "cloud",
            "devices": {"bedroom": {"power": False}},
        }
        coordinator._command_revision = 0
        coordinator.async_set_updated_data = Mock()
        device = Mock()
        device.id = "office"
        device.state = {"power": True, "speed": 3}
        device.last_command_used_cloud = True
        coordinator.async_schedule_command_reconciliation = Mock()

        coordinator.async_publish_device_state(device)

        coordinator.async_set_updated_data.assert_called_once_with(
            {
                "source": "command",
                "devices": {
                    "bedroom": {"power": False},
                    "office": {"power": True, "speed": 3},
                },
            }
        )
        coordinator.async_schedule_command_reconciliation.assert_called_once_with()

    def test_publish_local_command_does_not_schedule_cloud_confirmation(self):
        """A local command relies on UDP state instead of spending cloud quota."""
        coordinator = object.__new__(AtombergDataUpdateCoordinator)
        coordinator.data = {"source": "cloud", "devices": {}}
        coordinator._command_revision = 0
        coordinator.async_set_updated_data = Mock()
        coordinator.async_schedule_command_reconciliation = Mock()
        device = Mock()
        device.id = "office"
        device.state = {"power": True, "speed": 3}
        device.last_command_used_cloud = False

        coordinator.async_publish_device_state(device)

        coordinator.async_schedule_command_reconciliation.assert_not_called()

    def test_command_confirmation_is_debounced(self):
        """Rapid household controls must share one delayed cloud refresh."""
        coordinator = object.__new__(AtombergDataUpdateCoordinator)
        coordinator.hass = Mock()
        coordinator._cancel_command_reconciliation = None
        coordinator._command_revision = 1
        first_cancel = Mock()
        second_cancel = Mock()

        with patch(
            "custom_components.atomberg.coordinator.async_call_later",
            side_effect=[first_cancel, second_cancel],
        ) as call_later:
            coordinator.async_schedule_command_reconciliation()
            coordinator.async_schedule_command_reconciliation()

        first_cancel.assert_called_once_with()
        second_cancel.assert_not_called()
        assert call_later.call_count == 2

    async def test_older_confirmation_cannot_overwrite_a_newer_command(self):
        """Ignore a cloud response started before the latest household action."""
        coordinator = object.__new__(AtombergDataUpdateCoordinator)
        coordinator.hass = Mock()
        coordinator.api = Mock()
        coordinator.api.device_list = {
            "office": {"name": "Office Fan", "state": {"led": False}}
        }
        coordinator.api.async_get_device_state = AsyncMock(
            return_value=[
                {
                    "device_id": "office",
                    "is_online": True,
                    "power": False,
                    "led": True,
                }
            ]
        )
        coordinator._command_revision = 2
        device = Mock()
        device.id = "office"
        coordinator.devices = [device]

        data = await coordinator._async_refresh_cloud_state(
            "command",
            "command-confirmed",
            expected_command_revision=1,
        )

        assert data is None
        device.update_state.assert_not_called()
        assert coordinator.api.device_list["office"]["state"]["led"] is False

    async def test_command_confirmation_publishes_and_persists_cloud_state(self):
        """Authoritative cloud state replaces and persists optimistic state."""
        coordinator = object.__new__(AtombergDataUpdateCoordinator)
        coordinator.hass = Mock()
        coordinator.api = Mock()
        coordinator.api.device_list = {
            "office": {"name": "Office Fan", "state": {"speed": 1}}
        }
        coordinator.api.async_get_device_state = AsyncMock(
            return_value=[
                {
                    "device_id": "office",
                    "is_online": True,
                    "power": True,
                    "speed": 4,
                }
            ]
        )
        device = Mock()
        device.id = "office"
        device.state = {
            "is_online": True,
            "power": True,
            "speed": 4,
        }
        coordinator.devices = [device]
        coordinator._command_revision = 0
        cache = Mock()
        cache.async_save = AsyncMock()

        with patch(
            "custom_components.atomberg.coordinator.get_device_cache",
            return_value=cache,
        ):
            data = await coordinator._async_refresh_cloud_state(
                "command", "command-confirmed"
            )

        coordinator.api.async_get_device_state.assert_awaited_once_with(
            ["office"], call_type="command"
        )
        device.update_state.assert_called_once_with(data["devices"]["office"])
        cache.async_save.assert_awaited_once_with(coordinator.api.device_list)
        assert data["source"] == "command-confirmed"
        assert coordinator.api.device_list["office"]["state"]["speed"] == 4

    async def test_sleep_mode_clears_timer_in_acknowledged_state(self):
        """The fan cancels its timer when sleep mode is enabled."""
        device = object.__new__(AtombergDevice)
        device._name = "Office Fan"
        device._state = {
            ATTR_SLEEP: False,
            ATTR_TIMER_HOURS: 6,
            ATTR_TIMER_TIME_ELAPSED_MINS: 14,
        }
        device._async_send_command = AsyncMock(return_value=True)

        changed = await device.async_turn_on_sleep_mode()

        assert changed
        device._async_send_command.assert_awaited_once_with({ATTR_SLEEP: True})
        assert device.state[ATTR_SLEEP]
        assert device.state[ATTR_TIMER_HOURS] == 0
        assert device.state[ATTR_TIMER_TIME_ELAPSED_MINS] == 0

    async def test_timer_clears_sleep_mode_in_acknowledged_state(self):
        """The fan cancels sleep mode when a timer is enabled."""
        device = object.__new__(AtombergDevice)
        device._name = "Office Fan"
        device._state = {
            ATTR_SLEEP: True,
            ATTR_TIMER_HOURS: 0,
            ATTR_TIMER_TIME_ELAPSED_MINS: 0,
        }
        device._async_send_command = AsyncMock(return_value=True)

        changed = await device.async_set_timer(1)

        assert changed
        device._async_send_command.assert_awaited_once_with({"timer": 1})
        assert not device.state[ATTR_SLEEP]
        assert device.state[ATTR_TIMER_HOURS] == 1
        assert device.state[ATTR_TIMER_TIME_ELAPSED_MINS] == 0

    async def test_cancelling_timer_preserves_sleep_mode(self):
        """Turning a timer off must not also disable sleep mode."""
        device = object.__new__(AtombergDevice)
        device._name = "Office Fan"
        device._state = {
            ATTR_SLEEP: True,
            ATTR_TIMER_HOURS: 1,
            ATTR_TIMER_TIME_ELAPSED_MINS: 10,
        }
        device._async_send_command = AsyncMock(return_value=True)

        changed = await device.async_set_timer(0)

        assert changed
        assert device.state[ATTR_SLEEP]
        assert device.state[ATTR_TIMER_HOURS] == 0
        assert device.state[ATTR_TIMER_TIME_ELAPSED_MINS] == 0


if __name__ == "__main__":
    unittest.main()
