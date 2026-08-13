"""Cloud API for Atomberg with persistent provider quota protection."""

import asyncio
import datetime
import functools
import time
from copy import deepcopy
from logging import getLogger
from typing import Any, Literal

import jwt
import requests
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.util.dt import utcnow
from requests import Response

from .const import CLOUD_CALL_BUDGET, DEVICE_CACHE, DOMAIN

_LOGGER = getLogger(__name__)

CLOUD_CALL_LIMIT = 100
CLOUD_POLL_CALL_LIMIT = 24
CLOUD_CALL_WINDOW = datetime.timedelta(hours=24)
CLOUD_MIN_CALL_INTERVAL = 0.21
STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.cloud_api_calls"
DEVICE_CACHE_STORAGE_KEY = f"{DOMAIN}.devices"

CloudCallType = Literal["auth", "setup", "poll", "command"]

SUPPORTED_SERIES = [
    "R1",
    "R2",
    "R3",
    "K1",
    "I1",
    "I2",
    "I3",
    "I4",
    "I5",
    "M1",
    "M2",
    "S1",
    "S2",
]


class AtombergCloudCallBudget:
    """Persist and enforce the Atomberg cloud API limits."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the rolling call budget."""
        self._store = Store[dict[str, Any]](hass, STORAGE_VERSION, STORAGE_KEY)
        self._lock = asyncio.Lock()
        self._calls: list[dict[str, Any]] = []
        self._blocked_until: float | None = None
        self._last_call_started: float | None = None

    async def async_load(self) -> None:
        """Load persisted usage and discard expired records."""
        stored = await self._store.async_load() or {}
        calls = stored.get("calls", [])
        self._calls = [
            call
            for call in calls
            if isinstance(call, dict)
            and isinstance(call.get("timestamp"), (int, float))
            and call.get("kind") in {"auth", "setup", "poll", "command"}
        ]
        blocked_until = stored.get("blocked_until")
        self._blocked_until = (
            float(blocked_until) if isinstance(blocked_until, (int, float)) else None
        )
        self._prune(utcnow().timestamp())

    def _prune(self, now: float) -> None:
        """Remove calls outside the rolling provider quota window."""
        cutoff = now - CLOUD_CALL_WINDOW.total_seconds()
        self._calls = [call for call in self._calls if call["timestamp"] > cutoff]
        if self._blocked_until is not None and self._blocked_until <= now:
            self._blocked_until = None

    async def _async_save(self) -> None:
        """Persist the quota before allowing another API call."""
        await self._store.async_save(
            {"calls": self._calls, "blocked_until": self._blocked_until}
        )

    async def async_acquire(self, kind: CloudCallType) -> None:
        """Reserve one API call while enforcing daily and burst limits."""
        async with self._lock:
            now = utcnow().timestamp()
            self._prune(now)

            if self._blocked_until is not None:
                raise CloudApiQuotaExceeded(
                    "Atomberg reported its cloud API quota exhausted; calls are "
                    "paused for 24 hours"
                )

            if len(self._calls) >= CLOUD_CALL_LIMIT:
                raise CloudApiQuotaExceeded(
                    "Atomberg cloud API rolling limit of 100 calls per 24 hours "
                    "has been reached"
                )

            poll_calls = sum(call["kind"] == "poll" for call in self._calls)
            if kind == "poll" and poll_calls >= CLOUD_POLL_CALL_LIMIT:
                raise CloudPollQuotaExceeded(
                    "Atomberg cloud polling allocation of 24 calls per 24 hours "
                    "has been reached"
                )

            if self._last_call_started is not None:
                elapsed = time.monotonic() - self._last_call_started
                if delay := CLOUD_MIN_CALL_INTERVAL - elapsed:
                    if delay > 0:
                        await asyncio.sleep(delay)

            call_started = utcnow().timestamp()
            self._calls.append({"timestamp": call_started, "kind": kind})
            await self._async_save()
            self._last_call_started = time.monotonic()

    async def async_mark_provider_quota_exhausted(self) -> None:
        """Stop retries after Atomberg returns HTTP 429."""
        async with self._lock:
            self._blocked_until = (utcnow() + CLOUD_CALL_WINDOW).timestamp()
            await self._async_save()

    @property
    def blocked(self) -> bool:
        """Return whether cloud calls are currently circuit-broken."""
        self._prune(utcnow().timestamp())
        return self._blocked_until is not None or len(self._calls) >= CLOUD_CALL_LIMIT


class AtombergDeviceCache:
    """Persist cloud device metadata and last known state for offline startup."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the device cache."""
        self._store = Store[dict[str, Any]](
            hass, STORAGE_VERSION, DEVICE_CACHE_STORAGE_KEY
        )

    async def async_load(self) -> dict[str, dict]:
        """Load validated cached devices."""
        stored = await self._store.async_load() or {}
        devices = stored.get("devices", {})
        if not isinstance(devices, dict):
            return {}
        return {
            device_id: data
            for device_id, data in devices.items()
            if isinstance(device_id, str)
            and isinstance(data, dict)
            and isinstance(data.get("state"), dict)
        }

    async def async_save(self, devices: dict[str, dict]) -> None:
        """Persist device metadata and state."""
        await self._store.async_save({"devices": devices})


async def async_get_cloud_call_budget(
    hass: HomeAssistant,
) -> AtombergCloudCallBudget:
    """Return the shared account-level cloud call budget."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if budget := domain_data.get(CLOUD_CALL_BUDGET):
        return budget

    budget = AtombergCloudCallBudget(hass)
    await budget.async_load()
    domain_data[CLOUD_CALL_BUDGET] = budget
    return budget


def get_device_cache(hass: HomeAssistant) -> AtombergDeviceCache:
    """Return the shared Atomberg device cache."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if cache := domain_data.get(DEVICE_CACHE):
        return cache
    cache = AtombergDeviceCache(hass)
    domain_data[DEVICE_CACHE] = cache
    return cache


class AtombergCloudAPI:
    """Atomberg CloudAPI."""

    def __init__(
        self,
        hass: HomeAssistant,
        api_key: str,
        refresh_token: str,
        call_budget: AtombergCloudCallBudget | None = None,
    ) -> None:
        """Init Atomberg CloudAPI."""
        self._hass = hass
        self._base_url = "https://api.developer.atomberg-iot.com"
        self._api_key = api_key
        self._refresh_token = refresh_token
        self._call_budget = call_budget
        self._access_token = None
        self._access_token_lock = asyncio.Lock()
        self.device_list: dict[str, dict] = {}

    async def test_connection(self):
        """Test API connection."""
        try:
            await self.async_sync_list_of_devices()
        except CloudApiQuotaExceeded:
            raise
        except KeyError as e:
            _LOGGER.error("Atomberg Cloud authentication failed")
            raise InvalidAuth("Failed to authenticate") from e
        except Exception as e:
            _LOGGER.error("Atomberg Cloud connection test failed")
            raise CannotConnect("Failed to connect") from e

    async def async_get_access_token(self):
        """Get access token."""
        async with self._access_token_lock:
            access_token_expired = False
            if self._access_token:
                try:
                    access_token_data = jwt.decode(
                        self._access_token, options={"verify_signature": False}
                    )
                    exp_timestamp = access_token_data["exp"]
                    exp_datetime = datetime.datetime.fromtimestamp(
                        exp_timestamp, datetime.UTC
                    )
                    access_token_expired = utcnow() > exp_datetime
                except jwt.ExpiredSignatureError:
                    access_token_expired = True

                if not access_token_expired:
                    return self._access_token

            try:
                resp = await self.async_make_request(
                    "/v1/get_access_token",
                    headers={"Authorization": f"Bearer {self._refresh_token}"},
                    call_type="auth",
                )
            except requests.exceptions.ConnectionError:
                return ConnectionError

            if not resp.ok:
                return None

            data = resp.json()
            if data["status"] == "Success":
                self._access_token = data["message"]["access_token"]
                return self._access_token
            return None

    async def async_make_request(
        self,
        url: str,
        method: Literal["GET", "POST"] = "GET",
        body: dict | None = None,
        headers: dict | None = None,
        call_type: CloudCallType = "setup",
    ) -> Response:
        """Make a request."""
        headers_base = {
            "X-API-Key": self._api_key,
        }
        headers_extra = (
            headers
            if headers
            else {"Authorization": f"Bearer {await self.async_get_access_token()}"}
        )
        full_url = self._base_url + url

        if self._call_budget is not None:
            await self._call_budget.async_acquire(call_type)

        match method:
            case "POST":
                func = functools.partial(
                    requests.post,
                    full_url,
                    headers=dict(headers_base, **headers_extra),
                    json=body,
                )
            case _:
                func = functools.partial(
                    requests.get, full_url, headers=dict(headers_base, **headers_extra)
                )

        resp = await self._hass.async_add_executor_job(func)
        error_message = self._response_error_message(resp)
        provider_quota_denied = (
            resp.status_code == 403
            and "explicit deny in an identity-based policy" in error_message.lower()
        )
        if resp.status_code == 429 or provider_quota_denied:
            if self._call_budget is not None:
                await self._call_budget.async_mark_provider_quota_exhausted()
            raise CloudApiQuotaExceeded(
                f"Atomberg cloud API denied access with HTTP {resp.status_code}; "
                "calls are paused for 24 hours"
            )
        if not resp.ok and resp.status_code < 500:
            _LOGGER.error(
                "Atomberg cloud request failed with HTTP %d: %s",
                resp.status_code,
                error_message or "unknown response",
            )
        return resp

    @staticmethod
    def _response_error_message(resp: Response) -> str:
        """Extract Atomberg and API Gateway error messages safely."""
        try:
            data = resp.json()
        except (requests.exceptions.JSONDecodeError, ValueError):
            return ""
        if not isinstance(data, dict):
            return ""
        message = data.get("message", data.get("Message", ""))
        return message if isinstance(message, str) else ""

    async def async_sync_list_of_devices(self) -> bool:
        """Get list of all devices connected to the account."""
        resp = await self.async_make_request("/v1/get_list_of_devices")

        data = resp.json()
        status = False
        if data.get("status") == "Success":
            supported_devices = list(
                filter(
                    lambda d: d["series"] in SUPPORTED_SERIES,
                    data["message"]["devices_list"],
                )
            )
            states = await self.async_get_device_state(
                [d["device_id"] for d in supported_devices], call_type="setup"
            )
            for dev in supported_devices:
                state = next(
                    filter(lambda x: x["device_id"] == dev["device_id"], states)
                )
                states.remove(state)
                self.device_list[state.pop("device_id")] = {**dev, "state": state}
            _LOGGER.info("Found %d atomberg devices", len(self.device_list))
            await get_device_cache(self._hass).async_save(self.device_list)
            status = True
        else:  # noqa: RET505
            _LOGGER.error(
                "Atomberg devices sync failed due to '%s'. Please check API credentials",
                data["message"],
            )
        return status

    async def async_get_device_state(
        self,
        device_ids: list[str] | None = None,
        call_type: Literal["setup", "poll"] = "poll",
    ) -> list[dict] | None:
        """Get state of all/single device(s)."""
        resp = await self.async_make_request(
            "/v1/get_device_state?device_id=all", call_type=call_type
        )

        data = resp.json()
        if data["status"] == "Success":
            device_state = []
            for state in filter(
                lambda s: s["device_id"] in device_ids if device_ids else True,
                deepcopy(data["message"]["device_state"]),
            ):
                # Rename some keys for ease of access
                state["speed"] = state.pop("last_recorded_speed")
                state["sleep"] = state.pop("sleep_mode")
                if state.get("last_recorded_brightness"):
                    state["brightness"] = state.pop("last_recorded_brightness")
                if state.get("last_recorded_color"):
                    state["light_mode"] = state.pop("last_recorded_color")
                device_state.append(state)

            return device_state

    async def async_send_command(self, device_id: str, command: dict) -> bool:
        """Send command to a device."""
        _LOGGER.debug("Sending command: '%s' to %s", command, device_id)
        payload = {"device_id": device_id, "command": command}
        resp = await self.async_make_request(
            "/v1/send_command", "POST", body=payload, call_type="command"
        )
        data = resp.json()
        return data["status"] == "Success"


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""


class CloudApiQuotaExceeded(HomeAssistantError):
    """Error to indicate the account-level cloud quota is exhausted."""


class CloudPollQuotaExceeded(CloudApiQuotaExceeded):
    """Error to indicate the reserved cloud polling budget is exhausted."""
