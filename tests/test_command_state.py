"""Regression tests for immediate state publication after commands."""

import unittest
from unittest.mock import AsyncMock, Mock

from custom_components.atomberg.coordinator import AtombergDataUpdateCoordinator
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

    async def test_turn_on_with_percentage_uses_one_combined_command(self):
        """A speed selection while off must not spend two provider calls."""
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
        coordinator.async_set_updated_data = Mock()
        device = Mock()
        device.id = "office"
        device.state = {"power": True, "speed": 3}

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


if __name__ == "__main__":
    unittest.main()
