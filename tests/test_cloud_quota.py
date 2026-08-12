"""Regression tests for Atomberg cloud quota enforcement."""

import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.atomberg.api import (
    CLOUD_CALL_LIMIT,
    CLOUD_POLL_CALL_LIMIT,
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

    async def test_total_call_limit_is_hard_capped(self):
        """The 101st call in a rolling day must be rejected."""
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


if __name__ == "__main__":
    unittest.main()
