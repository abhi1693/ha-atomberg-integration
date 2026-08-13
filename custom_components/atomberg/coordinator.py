"""Data update coordinator for the Atomberg integration."""

from datetime import timedelta
from logging import getLogger

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import AtombergCloudAPI, CloudApiQuotaExceeded
from .const import MANUFACTURER
from .device import AtombergDevice
from .udp_listener import UDPListener

_LOGGER = getLogger(__name__)


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

        # Add callback on udp listener
        self.udp_listener.add_callback(self.config_entry, self.async_set_updated_data)

    async def _async_update_data(self) -> dict:
        """Refresh device availability and state from the cloud API."""
        device_ids = [device.id for device in self.devices]
        try:
            states = await self.api.async_get_device_state(device_ids)
        except CloudApiQuotaExceeded as err:
            _LOGGER.warning("Skipping Atomberg cloud refresh: %s", err)
            return self.data or self._all_device_states("quota")
        except Exception as err:
            raise UpdateFailed("Unable to refresh Atomberg cloud state") from err

        if states is None:
            raise UpdateFailed("Atomberg cloud state response was unsuccessful")

        states_by_id = {state["device_id"]: state for state in states}
        for device in self.devices:
            if state := states_by_id.get(device.id):
                device.update_state(state)

        return {"source": "cloud", "devices": states_by_id}

    @callback
    def async_publish_device_state(self, device: AtombergDevice) -> None:
        """Publish a command-confirmed state without another cloud call."""
        devices = dict((self.data or {}).get("devices", {}))
        devices[device.id] = device.state
        self.async_set_updated_data({"source": "command", "devices": devices})

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
