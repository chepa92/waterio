from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import CONF_MAC
from homeassistant.const import Platform

from .const import DOMAIN, LOGGER
from .waterio import WaterioInstance

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up WaterIo from a config entry."""
    instance = WaterioInstance(entry.data[CONF_MAC])
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = instance
    hass.data[DOMAIN][entry.entry_id] = instance
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        instance = hass.data[DOMAIN].pop(entry.entry_id)
        await instance.disconnect()
    return unload_ok