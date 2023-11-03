from homeassistant.components.sensor import SensorEntity
from homeassistant.const import DEVICE_CLASS_BATTERY, PERCENTAGE
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
    STATE_CLASS_MEASUREMENT
)
from homeassistant.core import callback

from .const import DOMAIN, LOGGER

@callback
def async_get_waterio_battery(hass, entity_id):
    """Retrieve the battery level from the WaterIo instance."""
    LOGGER.debug("Retrieve the battery level from the WaterIo instance")
    waterio = hass.data[DOMAIN][entity_id]
    return waterio.battery

async def async_setup_entry(hass, config, async_add_entities, discovery_info=None):
    entities = []
    LOGGER.debug("Setup sensor")
    for entry_id, waterio in hass.data[DOMAIN].items():
        entity = WaterIoBatterySensor(waterio, entry_id)
        entities.append(entity)

    async_add_entities(entities, True)

class WaterIoBatterySensor(SensorEntity):
    def __init__(self, waterio, entry_id):
        self._waterio = waterio
        self._entry_id = entry_id

    @property
    def name(self):
        return f"WaterIo Battery Level {self._entry_id}"

    @property
    def unique_id(self):
        return f"{self._entry_id}-battery"

    @property
    def unit_of_measurement(self):
        return PERCENTAGE

    @property
    def device_class(self):
        return DEVICE_CLASS_BATTERY

    @property
    def state_class(self):
        return STATE_CLASS_MEASUREMENT

    @property
    def state(self):
        # Retrieve the battery level from the WaterioInstance
        return self._waterio.battery
    