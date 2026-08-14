"""Device as wrapper for Atomberg Cloud APIs."""

import json
import socket
from copy import deepcopy
from logging import getLogger
from typing import Any

from homeassistant.components.light import ATTR_BRIGHTNESS
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_HOME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import format_mac

from .api import AtombergCloudAPI, CloudApiQuotaExceeded
from .const import CONF_USE_CLOUD_CONTROL

_LOGGER = getLogger(__name__)

SUPPORTED_BRIGHTNESS_CONTROL_SERIES = ["I1", "I5", "M1", "S1", "S2"]
SUPPORTED_COLOR_EFFECT_SERIES = ["I1", "I5"]

ATTR_IS_ONLINE = "is_online"
ATTR_POWER = "power"
ATTR_SPEED = "speed"
ATTR_SLEEP = "sleep"
ATTR_LIGHT_MODE = "light_mode"
ATTR_LED = "led"
ATTR_TIMER_HOURS = "timer_hours"
ATTR_TIMER_TIME_ELAPSED_MINS = "timer_time_elapsed_mins"
LIGHT_MODE_DAYLIGHT = "daylight"
LIGHT_MODE_COOL = "cool"
LIGHT_MODE_WARM = "warm"
LED_BRIGHTNESS_SCALE = (1, 100)
TIMER_MAPPING = [
    (0, "Off"),
    (1, "1 hour"),
    (2, "2 hours"),
    (3, "3 hours"),
    (6, "6 hours"),
]


class AtombergDevice:
    """Atomberg device."""

    def __init__(
        self,
        data: dict[str, Any],
        api: AtombergCloudAPI,
        hass: HomeAssistant,
        config_entry: ConfigEntry = None,
    ) -> None:
        """Init Atomberg device."""
        self._device_id = data["device_id"]
        self._color = data["color"]
        self._series = data["series"]
        self._model = data["model"]
        self._name = data["name"]
        self._api = api
        self._hass = hass
        self._state: dict = data["state"]
        self._last_seen: int = None
        self._ip_addr: str = data.get("ip_address")
        self._options = config_entry.options if config_entry else {}
        self._last_command_used_cloud = False

        # Add options update listener
        if config_entry:
            config_entry.async_on_unload(
                config_entry.add_update_listener(self._update_options)
            )

    @property
    def supports_brightness_control(self):
        """Check whether device supports brightness control."""
        return self.series in SUPPORTED_BRIGHTNESS_CONTROL_SERIES

    @property
    def supports_color_effect(self):
        """Check whether device supports color modes."""
        return self.series in SUPPORTED_COLOR_EFFECT_SERIES

    @property
    def state(self) -> dict[str, Any]:
        """Get state."""
        return deepcopy(self._state)

    @property
    def name(self) -> str:
        """Get name."""
        return self._name

    @property
    def id(self) -> str:
        """Get device_id."""
        return self._device_id

    @property
    def color(self) -> str:
        """Get color."""
        return self._color

    @property
    def series(self) -> str:
        """Get series."""
        return self._series

    @property
    def model(self) -> str:
        """Get model."""
        return self._model

    @property
    def last_seen(self) -> float:
        """Get last seen UTC timestamp."""
        return self._last_seen

    @property
    def ip_address(self) -> str | None:
        """Get IP address."""
        return self._ip_addr

    @property
    def mac(self) -> str:
        """Get MAC address."""
        return format_mac(self.id)

    @property
    def last_command_used_cloud(self) -> bool:
        """Return whether the latest successful command used the cloud API."""
        return self._last_command_used_cloud

    def update_last_seen(self, value: float):
        """Update last seen timestamp."""
        self._last_seen = value

    def update_ip_address(self, value: str | None):
        """Update IP address."""
        if self._ip_addr != value:
            _LOGGER.debug("IP address updated for %s: %s", self.name, value)
            self._ip_addr = value

    async def _update_options(self, hass: HomeAssistant, config_entry: ConfigEntry):
        """Update options."""
        self._options = config_entry.options
        _LOGGER.debug("Options updated for %s: %s", self.name, self._options)

    async def _async_send_command(self, command: dict) -> bool:
        """Send command to the device."""
        self._resolve_ip_address()
        use_cloud = self._options.get(CONF_USE_CLOUD_CONTROL, True)
        if not use_cloud and self.ip_address:
            changed = self._send_local_command(command)
            self._last_command_used_cloud = False
            return changed

        try:
            changed = await self._api.async_send_command(self.id, command)
        except CloudApiQuotaExceeded:
            if not self.ip_address:
                raise
            _LOGGER.warning(
                "Atomberg cloud quota unavailable; sending %s command locally",
                self.name,
            )
            changed = self._send_local_command(command)
            self._last_command_used_cloud = False
            return changed
        else:
            self._last_command_used_cloud = changed
            return changed

    def _resolve_ip_address(self) -> None:
        """Resolve the fan IP from a matching network tracker without cloud I/O."""
        ip_address = None
        for state in self._hass.states.async_all("device_tracker"):
            if state.attributes.get("mac", "").lower() != self.mac.lower():
                continue
            if state.state == STATE_HOME:
                ip_address = state.attributes.get("ip")
            break
        self.update_ip_address(ip_address)

    def _send_local_command(self, command: dict) -> bool:
        """Send one command directly to the fan over the LAN."""
        message = json.dumps(command).encode()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sent_bytes = sock.sendto(message, (self.ip_address, 5600))
        if sent_bytes > 0:
            _LOGGER.debug(
                "Command sent to %s (%s): %s",
                self.name,
                self.ip_address,
                command,
            )
            return True
        _LOGGER.error(
            "Failed to send command to %s (%s): %s",
            self.name,
            self.ip_address,
            command,
        )
        return False

    async def async_turn_on(self):
        """Turn on."""
        cmd = {ATTR_POWER: True}
        if await self._async_send_command(cmd):
            _LOGGER.debug("%s: turned on", self.name)
            self.update_state(cmd)
            return True
        return False

    async def async_turn_on_at_speed(self, value: int):
        """Turn on at one of the fan's six discrete speeds."""
        if value not in range(1, 7):
            raise ValueError("Value must in range of 1-6.")
        power_cmd = {ATTR_POWER: True}
        if not await self._async_send_command(power_cmd):
            return False

        power_used_cloud = self.last_command_used_cloud
        self.update_state(power_cmd)
        speed_cmd = {ATTR_SPEED: value}
        if not await self._async_send_command(speed_cmd):
            self._last_command_used_cloud = power_used_cloud
            return True

        _LOGGER.debug("%s: turned on at speed %d", self.name, value)
        self.update_state(speed_cmd)
        return True

    async def async_turn_off(self):
        """Turn off."""
        cmd = {ATTR_POWER: False}
        if await self._async_send_command(cmd):
            _LOGGER.debug("%s: turned off", self.name)
            self.update_state(cmd)
            return True
        return False

    async def async_set_speed(self, value: int):
        """Set speed."""
        if value not in range(1, 7):
            raise ValueError("Value must in range of 1-6.")
        cmd = {ATTR_SPEED: value}
        if await self._async_send_command(cmd):
            _LOGGER.debug("%s: set speed %d", self.name, value)
            self.update_state(cmd)
            return True
        return False

    async def async_send_light_command(self, cmd: dict):
        """Send combined light command."""
        supported_cmds = {ATTR_LED, ATTR_LIGHT_MODE, ATTR_BRIGHTNESS}
        if not set(cmd.keys()).issubset(supported_cmds):
            raise ValueError(f"Supported commands are: {', '.join(supported_cmds)}")

        if len(cmd) > 1 and ATTR_LED in cmd:
            del cmd[ATTR_LED]

        if await self._async_send_command(cmd):
            _LOGGER.debug("%s: Light command executed successfully.", self.name)
            self.update_state(cmd)
            return True
        return False

    async def async_turn_on_sleep_mode(self):
        """Turn on sleep mode."""
        cmd = {ATTR_SLEEP: True}
        if await self._async_send_command(cmd):
            _LOGGER.debug("%s: turned on sleep mode", self.name)
            self.update_state(
                {
                    **cmd,
                    ATTR_TIMER_HOURS: 0,
                    ATTR_TIMER_TIME_ELAPSED_MINS: 0,
                }
            )
            return True
        return False

    async def async_turn_off_sleep_mode(self):
        """Turn off sleep mode."""
        cmd = {ATTR_SLEEP: False}
        if await self._async_send_command(cmd):
            _LOGGER.debug("%s: turned off sleep mode", self.name)
            self.update_state(cmd)
            return True
        return False

    async def async_set_timer(self, value: int):
        """Set timer."""
        if value not in range(5):
            raise ValueError("Value must in range of 0-4.")
        if await self._async_send_command({"timer": value}):
            _LOGGER.debug("%s: set timer: %d", self.name, value)
            state = {
                ATTR_TIMER_HOURS: TIMER_MAPPING[value][0],
                ATTR_TIMER_TIME_ELAPSED_MINS: 0,
            }
            if value:
                state[ATTR_SLEEP] = False
            self.update_state(state)
            return True
        return False

    def update_state(self, new_state: dict):
        """Update states."""
        self._state.update(new_state)
