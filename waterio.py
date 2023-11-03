from typing import Tuple
from bleak import BleakClient, BleakScanner
import traceback
import asyncio
from datetime import timedelta

from .const import LOGGER

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType


READ_CHARACTERISTIC_UUIDS  = ["0000ffd0-0000-1000-8000-00805f9b34fb", "0000ffd4-0000-1000-8000-00805f9b34fb", "0000ffe0-0000-1000-8000-00805f9b34fb", "0000ffe4-0000-1000-8000-00805f9b34fb"]

async def discover():
    """Discover Bluetooth LE devices."""
    devices = await BleakScanner.discover()
    LOGGER.debug("Discovered devices: %s", [{"address": device.address, "name": device.name} for device in devices])
    return [device for device in devices if device.name and (device.name.startswith("Water-IO-Cap"))]

def create_status_callback(future: asyncio.Future):
    def callback(sender: int, data: bytearray):
        if not future.done():
            future.set_result(data)
    return callback

class WaterioInstance:
    def __init__(self, mac: str) -> None:
        self._mac = mac
        self._device = BleakClient(self._mac)
        self._read_uuid = None
        self._battery = None
        asyncio.create_task(self.periodic_update(10))

    @property
    def mac(self):
        return self._mac
    
    @property
    def battery(self):
        return self._battery  # Return the hardcoded battery value
    
    async def update(self):
        try:
            LOGGER.info("Trying to Update")
            if not self._device.is_connected:
                await self._device.connect(timeout=20)
                await asyncio.sleep(1)

                LOGGER.info(f"Read UUID: {self._read_uuid}")

            battery_level_uuid = "00002a19-0000-1000-8000-00805f9b34fb"

            # Read the battery level
            battery_level = await self._device.read_gatt_char(battery_level_uuid)
            
            # The battery level is a single byte value (0-100%)
            battery_level_percentage = battery_level[0]

            self._battery = battery_level_percentage

            LOGGER.debug("Battery %s", battery_level_percentage)

            await asyncio.sleep(1)


        except (Exception) as error:
            self._is_on = None
            LOGGER.error("Error getting status: %s", error)
            track = traceback.format_exc()
            LOGGER.debug(track)

    async def disconnect(self):
        if self._device.is_connected:
            await self._device.disconnect()
            
    async def periodic_update(self, interval):
        LOGGER.debug("Periodic updates start")
        while True:
            LOGGER.debug("Check!!")
            await self.update()
            await asyncio.sleep(interval)