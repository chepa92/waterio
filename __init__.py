"""Water.io Smart Bottle integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_MAC, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import DOMAIN, LOGGER
from .waterio import WaterioCoordinator

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.NUMBER,
    Platform.SWITCH,
    Platform.SELECT,
    Platform.BUTTON,
    Platform.LIGHT,
]

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Water.io from a config entry."""
    mac  = entry.data[CONF_MAC]
    name = entry.data.get("name", mac)

    coordinator = WaterioCoordinator(hass, entry, mac, name)

    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as exc:
        LOGGER.error("Cannot initialise Water.io %s: %s", mac, exc)
        raise ConfigEntryNotReady(f"Water.io {mac} not reachable: {exc}") from exc

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    LOGGER.info("Water.io integration set up  device=%s  mac=%s", name, mac)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry and disconnect BLE."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: WaterioCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.disconnect()
    return unload_ok