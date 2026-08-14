"""Data update coordinator for the Atomberg integration."""

from datetime import timedelta
from functools import partial
from logging import getLogger
from typing import Literal

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_STATE_CHANGED, STATE_HOME
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import AtombergCloudAPI, CloudApiQuotaExceeded, get_device_cache
from .const import MANUFACTURER
from .device import AtombergDevice
from .udp_listener import UDPListener

_LOGGER = getLogger(__name__)

COMMAND_RECONCILE_DELAY_SECONDS = 2


class AtombergDataUpdateCoordinator(DataUpdateCoordinator):
    """Atomberg data update coordinator."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: AtombergCloudAPI,
        udp_listener: UDPListener,
        config_entry: ConfigEntry,
    ) -> None:
        """Init data update coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=f"{MANUFACTURER} Coordinator",
            update_interval=timedelta(hours=1),
        )

        self.api = api
        self.udp_listener = udp_listener
        self.devices = [
            AtombergDevice(
                data=data,
                api=self.api,
                hass=self.hass,
                config_entry=self.config_entry,
            )
            for data in self.api.device_list.values()
        ]
        self._devices_by_mac = {device.mac: device for device in self.devices}
        self._cloud_state_available = False
        self._cancel_command_reconciliation = None
        self._command_revision = 0

        # Add callback on udp listener
        self.udp_listener.add_callback(self.config_entry, self.async_set_updated_data)
        self.config_entry.async_on_unload(
            self.hass.bus.async_listen(
                EVENT_STATE_CHANGED, self._async_handle_tracker_state
            )
        )
        self.config_entry.async_on_unload(self._async_cancel_command_reconciliation)

    async def _async_update_data(self) -> dict:
        """Refresh device availability and state from the cloud API."""
        return await self._async_refresh_cloud_state("poll", "cloud")

    async def _async_refresh_cloud_state(
        self,
        call_type: Literal["setup", "poll", "command"],
        source: str,
        expected_command_revision: int | None = None,
    ) -> dict | None:
        """Fetch, publish and persist authoritative cloud device states."""
        device_ids = [device.id for device in self.devices]
        try:
            states = await self.api.async_get_device_state(
                device_ids, call_type=call_type
            )
        except CloudApiQuotaExceeded as err:
            self._cloud_state_available = False
            _LOGGER.warning("Skipping Atomberg cloud refresh: %s", err)
            return self.data or self._all_device_states("quota")
        except Exception as err:
            raise UpdateFailed("Unable to refresh Atomberg cloud state") from err

        if states is None:
            raise UpdateFailed("Atomberg cloud state response was unsuccessful")

        self._cloud_state_available = True
        if (
            expected_command_revision is not None
            and expected_command_revision != self._command_revision
        ):
            _LOGGER.debug(
                "Discarding Atomberg cloud state for superseded command revision %d",
                expected_command_revision,
            )
            return None

        states_by_id = {state["device_id"]: state for state in states}
        for device in self.devices:
            if state := states_by_id.get(device.id):
                device.update_state(state)
                if cached_device := self.api.device_list.get(device.id):
                    cached_device["state"] = device.state

        await get_device_cache(self.hass).async_save(self.api.device_list)
        return {"source": source, "devices": states_by_id}

    @callback
    def async_schedule_command_reconciliation(self) -> None:
        """Debounce one authoritative cloud refresh after successful commands."""
        self._async_cancel_command_reconciliation()
        command_revision = self._command_revision
        self._cancel_command_reconciliation = async_call_later(
            self.hass,
            COMMAND_RECONCILE_DELAY_SECONDS,
            partial(self._async_reconcile_command_states, command_revision),
        )

    @callback
    def _async_cancel_command_reconciliation(self) -> None:
        """Cancel a pending command reconciliation callback."""
        if self._cancel_command_reconciliation is None:
            return
        self._cancel_command_reconciliation()
        self._cancel_command_reconciliation = None

    async def _async_reconcile_command_states(
        self, command_revision: int, _now
    ) -> None:
        """Replace optimistic command state with authoritative cloud state."""
        self._cancel_command_reconciliation = None
        try:
            data = await self._async_refresh_cloud_state(
                "command",
                "command-confirmed",
                expected_command_revision=command_revision,
            )
        except UpdateFailed as err:
            _LOGGER.warning("Unable to confirm Atomberg command state: %s", err)
            return
        if data is None:
            return
        self.async_set_updated_data(data)

    @callback
    def _async_handle_tracker_state(self, event: Event) -> None:
        """Refresh LAN presence when a matching network tracker changes."""
        entity_id = event.data.get("entity_id", "")
        if not entity_id.startswith("device_tracker."):
            return

        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        state_with_mac = new_state or old_state
        if state_with_mac is None or not (mac := state_with_mac.attributes.get("mac")):
            return
        device = self._devices_by_mac.get(format_mac(mac))
        if device is None:
            return

        online = bool(
            new_state is not None
            and new_state.state == STATE_HOME
            and (ip_address := new_state.attributes.get("ip"))
        )
        device.update_ip_address(ip_address if online else None)

        if not online and self._cloud_state_available:
            return

        device.update_state({"is_online": online})
        self.async_set_updated_data(self._all_device_states("presence"))

    @callback
    def async_publish_device_state(self, device: AtombergDevice) -> None:
        """Publish requested state now and schedule cloud confirmation."""
        self._command_revision += 1
        devices = dict((self.data or {}).get("devices", {}))
        devices[device.id] = device.state
        self.async_set_updated_data({"source": "command", "devices": devices})
        if device.last_command_used_cloud:
            self.async_schedule_command_reconciliation()

    def _all_device_states(self, source: str) -> dict:
        """Build coordinator data from the most recently known device state."""
        return {
            "source": source,
            "devices": {device.id: device.state for device in self.devices},
        }

    @callback
    def async_set_initial_data(self) -> None:
        """Seed entities from setup without repeating the cloud state call."""
        self.async_set_updated_data(self._all_device_states("setup"))
