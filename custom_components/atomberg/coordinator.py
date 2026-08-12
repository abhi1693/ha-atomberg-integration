"""Data update coordinator for the Atomberg integration."""

from datetime import timedelta
from logging import getLogger

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import AtombergCloudAPI
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
            update_interval=timedelta(seconds=30),
        )

        self.api = api
        self.udp_listener = udp_listener
        self.devices = [
            AtombergDevice(data=data, api=self.api, config_entry=self.config_entry)
            for data in self.api.device_list.values()
        ]

        # Add callback on udp listener
        self.udp_listener.add_callback(self.config_entry, self.async_set_updated_data)

    async def _async_update_data(self) -> dict:
        """Refresh device availability and state from the cloud API."""
        device_ids = [device.id for device in self.devices]
        try:
            states = await self.api.async_get_device_state(device_ids)
        except Exception as err:
            raise UpdateFailed("Unable to refresh Atomberg cloud state") from err

        if states is None:
            raise UpdateFailed("Atomberg cloud state response was unsuccessful")

        states_by_id = {state["device_id"]: state for state in states}
        for device in self.devices:
            if state := states_by_id.get(device.id):
                device.update_state(state)

        return {"source": "cloud", "devices": states_by_id}
