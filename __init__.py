"""Water.io Smart Bottle integration."""
from __future__ import annotations

import asyncio

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_MAC, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.event import async_track_time_change

from .const import DOMAIN, LOGGER
from .waterio import WaterioCoordinator

FIRST_REFRESH_TIMEOUT = 45  # seconds; stays well under HA's setup watchdog

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
    # Count journal entries off the event loop (the file can be several MB).
    await coordinator.async_init_journal_count()

    try:
        # Bound the first BLE sync.  establish_connection() makes up to 3
        # attempts, each of which can take 30s+ through an ESPHome proxy; left
        # unbounded, HA's own setup watchdog cancels the setup mid-connect
        # (CancelledError, "Setup ... cancelled") and the entry never retries
        # cleanly.  On timeout we raise ConfigEntryNotReady so HA retries
        # with backoff.
        async with asyncio.timeout(FIRST_REFRESH_TIMEOUT):
            await coordinator.async_config_entry_first_refresh()
    except TimeoutError as exc:
        await coordinator.disconnect()
        LOGGER.warning("Water.io %s: first sync timed out, will retry", mac)
        raise ConfigEntryNotReady(f"Water.io {mac} connect timed out") from exc
    except Exception as exc:
        LOGGER.error("Cannot initialise Water.io %s: %s", mac, exc)
        raise ConfigEntryNotReady(f"Water.io {mac} not reachable: {exc}") from exc

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    # Extra sync shortly before local midnight.  Drinks are attributed to a
    # day when they are synced, and drinks from an earlier day are excluded
    # from "Water Intake Today" - so without this, everything drunk after
    # the last hourly sync of the day would never appear in that day's total.
    async def _pre_midnight_sync(_now) -> None:
        LOGGER.debug("Pre-midnight sync for %s", name)
        await coordinator.async_request_refresh()

    entry.async_on_unload(
        async_track_time_change(hass, _pre_midnight_sync, hour=23, minute=57, second=30)
    )
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