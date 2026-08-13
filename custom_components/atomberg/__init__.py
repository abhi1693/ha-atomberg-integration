"""The Atomberg integration."""

from __future__ import annotations

from logging import getLogger

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, STATE_HOME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import format_mac

from .api import (
    AtombergCloudAPI,
    CloudApiQuotaExceeded,
    async_get_cloud_call_budget,
    get_device_cache,
)
from .const import (
    CONF_CONTROL_METHOD,
    CONF_REFRESH_TOKEN,
    DOMAIN,
    ENTRIES,
    UDP_LISTENER,
    ControlMethod,
)
from .coordinator import AtombergDataUpdateCoordinator
from .udp_listener import UDPListener

_LOGGER = getLogger(__name__)

CLOUD_PLATFORMS: list[Platform] = [
    Platform.FAN,
    Platform.SWITCH,
    Platform.LIGHT,
    Platform.SENSOR,
    Platform.SELECT,
]

IR_PLATFORMS: list[Platform] = [
    Platform.FAN,
    Platform.BUTTON,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Atomberg from a config entry."""
    control_method = entry.data.get(CONF_CONTROL_METHOD, ControlMethod.CLOUD)

    if control_method == ControlMethod.IR:
        return await _async_setup_ir_entry(hass, entry)

    return await _async_setup_cloud_entry(hass, entry)


async def _async_setup_cloud_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Atomberg using cloud API."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    domain_data.setdefault(UDP_LISTENER, None)
    domain_data.setdefault(ENTRIES, {})

    call_budget = await async_get_cloud_call_budget(hass)

    api = AtombergCloudAPI(
        hass,
        entry.data[CONF_API_KEY],
        entry.data[CONF_REFRESH_TOKEN],
        call_budget,
    )

    try:
        await api.test_connection()
    except CloudApiQuotaExceeded as err:
        api.device_list = await get_device_cache(hass).async_load()
        if not api.device_list:
            api.device_list = _devices_from_registry(hass)
        else:
            _add_network_presence(hass, api.device_list)
        if not api.device_list:
            raise ConfigEntryNotReady(
                "Atomberg cloud quota unavailable and no cached devices exist"
            ) from err
        await get_device_cache(hass).async_save(api.device_list)
        _LOGGER.warning(
            "Starting Atomberg from %d cached devices while cloud quota is unavailable",
            len(api.device_list),
        )
    except Exception as e:
        raise ConfigEntryNotReady("Failed to initialize Atomberg integration.") from e

    if not domain_data[UDP_LISTENER]:
        udp_listener = UDPListener(hass)
        domain_data[UDP_LISTENER] = udp_listener

        try:
            await udp_listener.start()
        except Exception:
            raise ConfigEntryError("Failed to start udp listener.")  # noqa: B904
    else:
        udp_listener = domain_data[UDP_LISTENER]

    coordinator = AtombergDataUpdateCoordinator(
        hass=hass,
        api=api,
        udp_listener=udp_listener,
        config_entry=entry,
    )
    domain_data[ENTRIES][entry.entry_id] = coordinator

    coordinator.async_set_initial_data()

    await hass.config_entries.async_forward_entry_setups(entry, CLOUD_PLATFORMS)

    return True


def _devices_from_registry(hass: HomeAssistant) -> dict[str, dict]:
    """Reconstruct cloud devices from Home Assistant's persistent registry."""
    devices: dict[str, dict] = {}
    registry = dr.async_get(hass)
    for entry in registry.devices.values():
        identifier = next(
            (
                value
                for domain, value in entry.identifiers
                if domain == DOMAIN and value.startswith("Atomberg.")
            ),
            None,
        )
        if identifier is None:
            continue
        device_id = identifier.removeprefix("Atomberg.")
        devices[device_id] = {
            "device_id": device_id,
            "color": "",
            "series": "",
            "model": entry.model or "Atomberg fan",
            "name": entry.name_by_user or entry.name or "Atomberg Fan",
            "state": {
                "is_online": False,
                "power": False,
                "speed": 1,
                "sleep": False,
                "led": False,
                "timer_hours": 0,
                "timer_time_elapsed_mins": 0,
            },
        }
    _add_network_presence(hass, devices)
    return devices


def _add_network_presence(hass: HomeAssistant, devices: dict[str, dict]) -> None:
    """Attach LAN addresses from network device trackers without API calls."""
    entity_registry = er.async_get(hass)
    tracker_entity_ids = {
        entry.entity_id
        for entry in entity_registry.entities.values()
        if entry.domain == "device_tracker"
    }
    trackers_by_mac = {}
    for entity_id in tracker_entity_ids:
        if (state := hass.states.get(entity_id)) is None:
            continue
        mac = state.attributes.get("mac")
        ip_address = state.attributes.get("ip")
        if mac and ip_address and state.state == STATE_HOME:
            trackers_by_mac[format_mac(mac)] = ip_address

    for device_id, data in devices.items():
        if ip_address := trackers_by_mac.get(format_mac(device_id)):
            data["ip_address"] = ip_address
            data["state"]["is_online"] = True
            continue
        data.pop("ip_address", None)
        data["state"]["is_online"] = False


async def _async_setup_ir_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Atomberg using IR control."""
    await hass.config_entries.async_forward_entry_setups(entry, IR_PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    control_method = entry.data.get(CONF_CONTROL_METHOD, ControlMethod.CLOUD)

    if control_method == ControlMethod.IR:
        return await hass.config_entries.async_unload_platforms(entry, IR_PLATFORMS)

    domain_data = hass.data[DOMAIN]
    udp_listener: UDPListener = domain_data[UDP_LISTENER]

    unload_ok = await hass.config_entries.async_unload_platforms(entry, CLOUD_PLATFORMS)
    if not unload_ok:
        return False

    domain_data[ENTRIES].pop(entry.entry_id, None)

    udp_listener.remove_callback(entry)

    if not domain_data[ENTRIES]:
        udp_listener.close()
        domain_data[UDP_LISTENER] = None

    return unload_ok
