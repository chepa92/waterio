import asyncio
from datetime import timedelta
from .waterio import discover, WaterioInstance
from typing import Any

from homeassistant import config_entries
from homeassistant.const import CONF_MAC
import voluptuous as vol
from homeassistant.helpers.device_registry import format_mac

from .const import (
    DOMAIN, LOGGER, UPDATE_INTERVAL,
)

CONF_POLL_INTERVAL = "poll_interval"

DATA_SCHEMA = vol.Schema({("host"): str})

MANUAL_MAC = "manual"

class WaterIoFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    @staticmethod
    def async_get_options_flow(config_entry):
        return WaterIoOptionsFlow(config_entry)

    def __init__(self) -> None:
        self.mac = None
        self.waterio_instance = None
        self.name = None

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        if user_input is not None:
            if user_input["mac"] == MANUAL_MAC:
                return await self.async_step_manual()
            
            self.mac = user_input["mac"]
            self.name = user_input["name"]
            await self.async_set_unique_id(format_mac(self.mac))
            return self.async_create_entry(title=self.name, data={CONF_MAC: self.mac, "name": self.name})

        already_configured = self._async_current_ids(False)
        devices = await discover()
        devices = [device for device in devices if format_mac(device.address) not in already_configured]

        if not devices:
            return await self.async_step_manual()
        
        return self.async_show_form(
            step_id="user", data_schema=vol.Schema(
                {
                    vol.Required("mac"): vol.In(
                        {
                            **{device.address: device.name for device in devices},
                            MANUAL_MAC: "Manually add a MAC address",
                        }
                    ),
                    vol.Required("name"): str
                }
            ),
            errors={})

    async def async_step_manual(self, user_input: "dict[str, Any] | None" = None):
        if user_input is not None:            
            self.mac = user_input["mac"]
            self.name = user_input["name"]
            await self.async_set_unique_id(format_mac(self.mac))
            return self.async_create_entry(title=self.name, data={CONF_MAC: self.mac, "name": self.name})

        return self.async_show_form(
            step_id="manual", data_schema=vol.Schema(
                {
                    vol.Required("mac"): str,
                    vol.Required("name"): str
                }
            ), errors={})


class WaterIoOptionsFlow(config_entries.OptionsFlow):
    """Options flow — lets users change poll interval after setup."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(self, user_input: "dict[str, Any] | None" = None):
        if user_input is not None:
            new_options = dict(self._entry.options)
            new_options[CONF_POLL_INTERVAL] = user_input[CONF_POLL_INTERVAL]
            return self.async_create_entry(title="", data=new_options)

        opts = self._entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(
                    CONF_POLL_INTERVAL,
                    default=int(opts.get(CONF_POLL_INTERVAL, UPDATE_INTERVAL)),
                    description={"suggested_value": int(opts.get(CONF_POLL_INTERVAL, UPDATE_INTERVAL))},
                ): vol.All(vol.Coerce(int), vol.Range(min=10, max=3600)),
            }),
        )