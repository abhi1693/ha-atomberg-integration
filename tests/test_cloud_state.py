"""Regression tests for Atomberg cloud state handling."""

import unittest
from unittest.mock import AsyncMock

from custom_components.atomberg.api import AtombergCloudAPI


class FakeResponse:
    """Minimal requests response used by cloud API tests."""

    def json(self):
        """Return a successful cloud state response."""
        return {
            "status": "Success",
            "message": {
                "device_state": [
                    {
                        "device_id": "office-fan",
                        "is_online": True,
                        "last_recorded_speed": 2,
                        "power": True,
                        "sleep_mode": False,
                        "led": True,
                        "timer_hours": 0,
                        "timer_time_elapsed_mins": 0,
                    }
                ]
            },
        }


class CloudStateTests(unittest.IsolatedAsyncioTestCase):
    """Verify cloud availability is preserved for Home Assistant entities."""

    async def test_cloud_online_state_is_not_overwritten(self):
        """An online cloud device must remain online after normalization."""
        api = AtombergCloudAPI(None, "api-key", "refresh-token")
        api.async_make_request = AsyncMock(return_value=FakeResponse())

        states = await api.async_get_device_state(["office-fan"])

        assert len(states) == 1
        assert states[0]["is_online"] is True
        assert states[0]["power"] is True
        assert states[0]["speed"] == 2


if __name__ == "__main__":
    unittest.main()
