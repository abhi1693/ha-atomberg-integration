"""Regression tests for Atomberg cloud quota enforcement."""

import asyncio
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

import pytest

from custom_components.atomberg.api import (
    CLOUD_CALL_LIMIT,
    CLOUD_POLL_CALL_LIMIT,
    AtombergCloudAPI,
    AtombergCloudCallBudget,
    CloudApiQuotaExceeded,
    CloudPollQuotaExceeded,
)


def create_budget() -> AtombergCloudCallBudget:
    """Create a budget without requiring a running Home Assistant instance."""
    budget = object.__new__(AtombergCloudCallBudget)
    budget._store = AsyncMock()
    budget._lock = asyncio.Lock()
    budget._calls = []
    budget._blocked_until = None
    budget._last_call_started = None
    return budget


class CloudQuotaTests(unittest.IsolatedAsyncioTestCase):
    """Verify cloud calls stay within the provider limits."""

    def test_account_call_limit_matches_current_tier(self):
        """The local hard cap must match the current 1000-call account tier."""
        assert CLOUD_CALL_LIMIT == 1000

    async def test_total_call_limit_is_hard_capped(self):
        """The 1001st call in a rolling day must be rejected."""
        budget = create_budget()
        now = time.time()
        budget._calls = [
            {"timestamp": now, "kind": "command"} for _ in range(CLOUD_CALL_LIMIT)
        ]

        with pytest.raises(CloudApiQuotaExceeded):
            await budget.async_acquire("command")

    async def test_polling_cannot_consume_command_reserve(self):
        """Only 24 of the daily calls may be used by scheduled polling."""
        budget = create_budget()
        now = time.time()
        budget._calls = [
            {"timestamp": now, "kind": "poll"} for _ in range(CLOUD_POLL_CALL_LIMIT)
        ]

        with pytest.raises(CloudPollQuotaExceeded):
            await budget.async_acquire("poll")

        await budget.async_acquire("command")
        assert budget._calls[-1]["kind"] == "command"

    async def test_calls_are_spaced_below_five_per_second(self):
        """Concurrent call starts must be serialized and spaced by 210 ms."""
        budget = create_budget()
        budget._last_call_started = time.monotonic()

        original_sleep = asyncio.sleep
        slept_for = None

        async def capture_sleep(delay: float) -> None:
            nonlocal slept_for
            slept_for = delay
            await original_sleep(0)

        with patch("custom_components.atomberg.api.asyncio.sleep", capture_sleep):
            await budget.async_acquire("command")

        assert slept_for is not None
        assert slept_for > 0.2

    async def test_api_gateway_explicit_deny_opens_circuit_breaker(self):
        """Atomberg's HTTP 403 quota response must stop further retries."""
        response = Mock()
        response.status_code = 403
        response.ok = False
        response.json.return_value = {
            "Message": "User is not authorized with an explicit deny in an "
            "identity-based policy"
        }
        hass = Mock()
        hass.async_add_executor_job = AsyncMock(return_value=response)
        call_budget = Mock()
        call_budget.async_acquire = AsyncMock()
        call_budget.async_mark_provider_quota_exhausted = AsyncMock()
        api = AtombergCloudAPI(hass, "api-key", "refresh-token", call_budget)

        with pytest.raises(CloudApiQuotaExceeded):
            await api.async_make_request(
                "/v1/get_access_token",
                headers={"Authorization": "Bearer refresh-token"},
                call_type="auth",
            )

        call_budget.async_mark_provider_quota_exhausted.assert_awaited_once_with()


if __name__ == "__main__":
    unittest.main()
