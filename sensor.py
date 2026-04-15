"""Water.io sensor platform – multi-sensor using DataUpdateCoordinator."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfVolume,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN, LOGGER,
    FIELD_BATTERY, FIELD_WATER_ML,
    FIELD_FIRMWARE, FIELD_HARDWARE, FIELD_MANUFACTURER, FIELD_SERIAL,
    FIELD_MANUAL_ML, FIELD_CAP_ML, FIELD_HYDRATION_STATUS, FIELD_HYDRATION_LEVEL,
    FIELD_DAILY_GOAL_ML, FIELD_EXTRA_GOAL_ML, FIELD_LAST_SYNC,
    FIELD_BATTERY_CELL, FIELD_DEVICE_CLOCK, FIELD_LOG_COUNT, FIELD_MAC_ADDRESS,
    FIELD_LAST_DRINK_ML, FIELD_LAST_DRINK_TS, FIELD_DRINK_COUNT_TODAY,
    FIELD_WATER_REMAINING_ML, FIELD_JOURNAL_ENTRIES,
)
from .waterio import WaterioCoordinator


@dataclass(frozen=True, kw_only=True)
class WaterioSensorDescription(SensorEntityDescription):
    """Extends SensorEntityDescription with the coordinator field key."""
    field: str = ""
    enabled_default: bool = True
    entity_cat: EntityCategory | None = None


SENSOR_DESCRIPTIONS: tuple[WaterioSensorDescription, ...] = (
    # ── Hydration ─────────────────────────────────────────────────────────
    WaterioSensorDescription(
        key="water_ml",
        field=FIELD_WATER_ML,
        name="Water Intake Today",
        native_unit_of_measurement=UnitOfVolume.MILLILITERS,
        device_class=SensorDeviceClass.VOLUME,
        # TOTAL: accumulated today, resets every midnight. HA allows TOTAL with
        # device_class=VOLUME; MEASUREMENT is rejected for VOLUME device class.
        state_class=SensorStateClass.TOTAL,
        icon="mdi:cup-water",
    ),
    WaterioSensorDescription(
        key="cap_hydration_total",
        field=FIELD_CAP_ML,
        name="Water Consumed (Total)",
        native_unit_of_measurement=UnitOfVolume.MILLILITERS,
        device_class=SensorDeviceClass.VOLUME,
        # TOTAL_INCREASING: never resets except on device CLEAR_LOGS.
        # HA's statistics engine computes the daily change automatically.
        # In Developer Tools > Statistics (or Energy dashboard) this shows
        # a bar chart of ml consumed per day / per week.
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:chart-bar",
    ),
    WaterioSensorDescription(
        key="manual_hydration_ml",
        field=FIELD_MANUAL_ML,
        name="Manual Hydration Today",
        native_unit_of_measurement=UnitOfVolume.MILLILITERS,
        device_class=SensorDeviceClass.VOLUME,
        state_class=SensorStateClass.TOTAL,
        icon="mdi:hand-water",
        enabled_default=False,
    ),
    WaterioSensorDescription(
        key="hydration_status",
        field=FIELD_HYDRATION_STATUS,
        name="Hydration Status Tier",
        # Raw tier value: 0=None, 1=Low, 2=Good, 3=Excellent
        # Not a percentage — no unit, shown as integer tier
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:water-percent",
        enabled_default=False,
    ),
    WaterioSensorDescription(
        key="hydration_level",
        field=FIELD_HYDRATION_LEVEL,
        name="Hydration Goal Progress",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:water-percent",
    ),
    WaterioSensorDescription(
        key="daily_goal_ml",
        field=FIELD_DAILY_GOAL_ML,
        name="Daily Hydration Goal",
        native_unit_of_measurement=UnitOfVolume.MILLILITERS,
        device_class=SensorDeviceClass.VOLUME,
        state_class=SensorStateClass.TOTAL,
        icon="mdi:flag-checkered",
    ),
    WaterioSensorDescription(
        key="extra_goal_ml",
        field=FIELD_EXTRA_GOAL_ML,
        name="Extra Daily Goal",
        native_unit_of_measurement=UnitOfVolume.MILLILITERS,
        device_class=SensorDeviceClass.VOLUME,
        state_class=SensorStateClass.TOTAL,
        icon="mdi:flag-plus",
        enabled_default=False,
    ),
    WaterioSensorDescription(
        key="last_drink_ml",
        field=FIELD_LAST_DRINK_ML,
        name="Last Drink Amount",
        native_unit_of_measurement=UnitOfVolume.MILLILITERS,
        # No device_class: keeps state_class=MEASUREMENT valid (VOLUME device_class
        # rejects MEASUREMENT per HA validation rules).
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:water-plus",
    ),
    WaterioSensorDescription(
        key="water_remaining_ml",
        field=FIELD_WATER_REMAINING_ML,
        name="Water Remaining in Bottle",
        native_unit_of_measurement=UnitOfVolume.MILLILITERS,
        # No device_class: level goes both up (refill) and down (drink) so HA's
        # VOLUME device_class + TOTAL/TOTAL_INCREASING state_classes don't apply.
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:cup-water",
    ),
    WaterioSensorDescription(
        key="last_drink_ts",
        field=FIELD_LAST_DRINK_TS,
        name="Last Drink Time",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:clock-check-outline",
    ),
    WaterioSensorDescription(
        key="drink_count_today",
        field=FIELD_DRINK_COUNT_TODAY,
        name="Drinks Today",
        native_unit_of_measurement="drinks",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:counter",
    ),
    # ── Device ────────────────────────────────────────────────────────────
    WaterioSensorDescription(
        key="battery",
        field=FIELD_BATTERY,
        name="Battery",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:battery",
        # Source: standard GATT Battery Level characteristic (UUID 0x2A19).
        # This is the charge-circuit percentage (matches the app's value).
    ),

    WaterioSensorDescription(
        key="last_sync",
        field=FIELD_LAST_SYNC,
        name="Last Sync",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:clock-check",
    ),
    WaterioSensorDescription(
        key="battery_cell",
        field=FIELD_BATTERY_CELL,
        name="Battery Cell Life",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:battery-heart-variant",
        entity_cat=EntityCategory.DIAGNOSTIC,
        # Source: opcode 0x1B (GET_BATTERY) and 0x3C byte[13].
        # This is the raw hardware cell reading, NOT the charge-circuit %.
    ),
    WaterioSensorDescription(
        key="device_clock",
        field=FIELD_DEVICE_CLOCK,
        name="Device Clock",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:clock-digital",
        entity_cat=EntityCategory.DIAGNOSTIC,
    ),
    WaterioSensorDescription(
        key="log_count",
        field=FIELD_LOG_COUNT,
        name="Log Entries On Device",
        native_unit_of_measurement="entries",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:counter",
        entity_cat=EntityCategory.DIAGNOSTIC,
    ),
    WaterioSensorDescription(
        key="mac_address",
        field=FIELD_MAC_ADDRESS,
        name="MAC Address",
        icon="mdi:bluetooth",
        entity_cat=EntityCategory.DIAGNOSTIC,
    ),
    # ── Info (disabled by default) ────────────────────────────────────────
    WaterioSensorDescription(
        key="firmware",
        field=FIELD_FIRMWARE,
        name="Firmware Version",
        icon="mdi:chip",
        enabled_default=False,
        entity_cat=EntityCategory.DIAGNOSTIC,
    ),
    WaterioSensorDescription(
        key="hardware",
        field=FIELD_HARDWARE,
        name="Hardware Version",
        icon="mdi:chip",
        enabled_default=False,
        entity_cat=EntityCategory.DIAGNOSTIC,
    ),
    WaterioSensorDescription(
        key="journal_entry_count",
        field=FIELD_JOURNAL_ENTRIES,
        name="Journal Entries",
        native_unit_of_measurement="entries",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:book-clock",
        entity_cat=EntityCategory.DIAGNOSTIC,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Water.io sensors from a config entry."""
    LOGGER.debug("sensor async_setup_entry  entry_id=%s", entry.entry_id)
    coordinator: WaterioCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        WaterioSensor(coordinator, entry, desc)
        for desc in SENSOR_DESCRIPTIONS
    )


class WaterioSensor(CoordinatorEntity[WaterioCoordinator], SensorEntity):
    """A single Water.io sensor entity backed by the coordinator."""

    entity_description: WaterioSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: WaterioCoordinator,
        entry: ConfigEntry,
        description: WaterioSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id  = f"{coordinator.mac}-{description.key}"
        self._attr_entity_registry_enabled_default = description.enabled_default
        if description.entity_cat is not None:
            self._attr_entity_category = description.entity_cat

    @property
    def device_info(self) -> DeviceInfo:
        data: dict[str, Any] = self.coordinator.data or {}
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.mac)},
            name=self.coordinator.device_name,
            manufacturer=data.get(FIELD_MANUFACTURER, "Water.io"),
            model=data.get(FIELD_HARDWARE),
            sw_version=data.get(FIELD_FIRMWARE),
            serial_number=data.get(FIELD_SERIAL),
        )

    @property
    def available(self) -> bool:
        """Mark unavailable only if we have NEVER synced or last sync > 24 h ago.

        We intentionally do NOT check super().available here.  When the bottle
        is out of BLE range the coordinator poll fails and
        ``last_update_success`` goes False, but we still want all sensors to
        show their last known values (and *especially* ``last_sync``) so the
        user can see when the bottle was last reachable.  Sensors only go
        "unavailable" once the data is truly stale (>24 h since last sync)
        or when there has never been a successful sync.
        """
        data = self.coordinator.data
        if not data:
            return False
        last_sync_str = data.get(FIELD_LAST_SYNC)
        if not last_sync_str:
            return False
        try:
            last_sync = datetime.fromisoformat(last_sync_str)
            if last_sync.tzinfo is None:
                last_sync = last_sync.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) - last_sync < timedelta(hours=24)
        except (ValueError, TypeError):
            return False

    @property
    def native_value(self) -> Any:
        if self.coordinator.data is None:
            return None
        value = self.coordinator.data.get(self.entity_description.field)
        # Convert ISO timestamp string fields to timezone-aware datetime
        if self.entity_description.key in ("last_sync", "device_clock", "last_drink_ts") and isinstance(value, str):
            try:
                dt = datetime.fromisoformat(value)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except ValueError:
                return None
        return value

