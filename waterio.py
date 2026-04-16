"""Water.io BLE coordinator â€“ reverse-engineered from APK v3.5.0 / v4.8.6 (latest)."""
from __future__ import annotations

import asyncio
import json
import os
import struct
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from bleak import BleakClient, BleakError, BleakScanner
from bleak.backends.device import BLEDevice
from bleak_retry_connector import establish_connection, BleakClientWithServiceCache

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    LOGGER,
    DEVICE_NAME_PREFIX,
    UPDATE_INTERVAL,
    # Standard GATT
    UUID_BATTERY_LEVEL, UUID_FIRMWARE_REV, UUID_HARDWARE_REV,
    UUID_MANUFACTURER, UUID_SERIAL_NUMBER,
    # Proprietary â€“ discovery lists
    NOTIFY_CANDIDATES, WRITE_CANDIDATES,
    # Commands used directly in the coordinator
    CMD_GET_CAP_STATE, CMD_GET_HYDRATIONS,
    CMD_GET_BATTERY, CMD_GET_VERSION,
    CMD_GET_REAL_TIME, CMD_GET_LOG_LENGTH,
    CMD_GET_SYNC_INFO,
    CMD_GET_SILENT_MODE, CMD_SET_SILENT_MODE,
    CMD_GET_MAC_ADDRESS,
    CMD_CLEAR_LOGS,
    CMD_GET_SINGLE_MEAS,
    CMD_START_BLINK, CMD_START_VIBRATION,
    CMD_SET_EXTRA_DAILY_GOAL, CMD_GET_EXTRA_DAILY_GOAL,
    # Field keys
    FIELD_BATTERY, FIELD_WATER_ML,
    FIELD_FIRMWARE, FIELD_HARDWARE, FIELD_MANUFACTURER,
    FIELD_SERIAL, FIELD_DAILY_GOAL, FIELD_LAST_SYNC,
    FIELD_MANUAL_ML, FIELD_CAP_ML, FIELD_HYDRATION_STATUS,
    FIELD_DAILY_GOAL_ML, FIELD_EXTRA_GOAL_ML,
    FIELD_CAP_CLOSED, FIELD_IS_CHARGING,
    FIELD_IS_ACTIVE, FIELD_NEED_DRINK, FIELD_SILENT_MODE,
    FIELD_BATTERY_CELL, FIELD_MAC_ADDRESS, FIELD_DEVICE_CLOCK,
    FIELD_LOG_COUNT,
    FIELD_LAST_DRINK_ML, FIELD_LAST_DRINK_TS, FIELD_DRINK_COUNT_TODAY,
    FIELD_WATER_REMAINING_ML,
    FIELD_HYDRATION_LEVEL, FIELD_REMINDER_COLOR,
    FIELD_BOTTLE_VOLUME, FIELD_JOURNAL_ENTRIES,
    # TypeConfig
    TYPECONFIG_FIELDS, FIELD_TO_TYPEBYTE,
    DEFAULT_SETTINGS,
)
from .protocol import (
    make_cmd,
    build_time_cmd, build_set_typeconfig,
    typeconfig_encode, typeconfig_decode,
    parse_notification, parse_log_entry, build_read_logs_cmd,
    GOAL_INDEX_ML, BOTTLE_VOLUME_ML, SNAP_FIELDS, OPCODE_NAMES,
    goal_index_to_ml, ml_to_goal_index,
)


# ---------------------------------------------------------------------------
# BLE helpers
# ---------------------------------------------------------------------------

async def discover() -> list[BLEDevice]:
    """Discover Water.io BLE devices nearby (legacy — config_flow uses HA bluetooth now)."""
    devices = await BleakScanner.discover(timeout=10.0)
    return [
        d for d in devices
        if d.name and d.name.startswith(DEVICE_NAME_PREFIX)
    ]


# ===========================================================================
# ARCHITECTURE & DESIGN NOTES
# ===========================================================================
#
# â”€â”€ ENTITY AVAILABILITY STRATEGY â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
#
#   When the bottle cap is out of BLE range (user away from home), the
#   coordinator poll fails.  HA's default CoordinatorEntity marks ALL entities
#   "unavailable" immediately, which hides last_sync, water_remaining, etc.
#
#   Our strategy:
#     â€¢ _async_update_data returns LAST KNOWN DATA on BLE failure (instead of
#       raising UpdateFailed).  This keeps coordinator.last_update_success True
#       and all entities showing stale values.
#     â€¢ A `ble_reachable` flag in the coordinator data dict indicates whether
#       the device was actually reached on the last poll.
#     â€¢ Read-only entities (sensor, binary_sensor) override `available` to
#       return True as long as last_sync is < 24 hours ago.  After 24 hours
#       without a successful sync, they go "unavailable".
#     â€¢ Write-capable entities (switch, number, select, light, button minus
#       sync) check `ble_reachable` â€” they go unavailable immediately when
#       the device is unreachable, since writing settings requires BLE.
#     â€¢ The "Force Sync" button is ALWAYS available (can trigger reconnect).
#
# â”€â”€ MULTI-DAY RETRO-SYNC â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
#
#   The device accumulates log entries in flash.  CLEAR_LOGS is sent after
#   each successful sync, so normally only new entries appear.  But if the
#   bottle has not been synced for days (user travel, HA offline, etc.),
#   entries spanning multiple calendar days accumulate.
#
#   _read_logs_async processes ALL entries chronologically.  prev_level and
#   water_remaining are tracked across day boundaries.  However, only
#   TODAY's drinks increment _daily_accumulated_drinks.  Past-day drinks are
#   logged for diagnostics but not added to today's count.
#
#   This ensures water_remaining always reflects the physical bottle state
#   regardless of sync gap, while the "drinks today" counter stays accurate.
#
# -- CAP_ML COUNTER & "Water Consumed (Total)" SENSOR -----------------------
#
#   IMPORTANT: The original Water.io app does NOT use GET_HYDRATIONS (0x72)
#   or GET_SYNC_INFO (0x78) counters for the daily water display.  It relies
#   ENTIRELY on summing drink deltas from log entries ('l' and U+00DC).
#   GET_SYNC_INFO is used only for post-sync diagnostic verification
#   (checkCapSync).  GET_HYDRATIONS is an on-demand Flutter query, not part
#   of the automatic sync flow.
#
#   Our "Water Consumed (Total)" sensor (FIELD_CAP_ML) is an HA-specific
#   addition that exposes the raw device counter for HA's statistics/energy
#   dashboard.  It uses SensorStateClass.TOTAL_INCREASING so HA computes
#   daily/weekly bar charts automatically.
#
#   FIELD_CAP_ML sourcing:
#     - GET_SYNC_INFO (0x78) bytes[10..11] = daily cumulative cap mL
#       This is the SOLE authoritative source for FIELD_CAP_ML.
#     - GET_HYDRATIONS (0x72) bytes[6..7] = interval mL since CLEAR_LOGS
#       This resets to 0 after every sync.  It is stored as _interval_cap_ml
#       (internal only) and NEVER written to FIELD_CAP_ML.
#     - GET_CAP_STATE (0x3C) bytes[14..15] on pv<15 = daily GOAL (not consumption!)
#       NOT used for FIELD_CAP_ML.
#
#   Intermediate notification pushes (async_set_updated_data during sync)
#   suppress FIELD_CAP_ML and FIELD_MANUAL_ML to prevent the TOTAL_INCREASING
#   sensor from seeing transient drops (interval value < daily cumulative)
#   which corrupt HA's reset-detection statistics.  The final stable values
#   are delivered in the coordinator return dict at end of sync.
#
#   For "Water Intake Today" (FIELD_WATER_ML), see DRINK COUNTING below.
#
# â”€â”€ PERSISTENCE & VERSION MIGRATION â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
#
#   Daily hydration state is persisted into config_entry.options so it
#   survives HA restarts within the same calendar day.  Keys:
#     _baseline_cap_ml  â€“ last seen cap_ml (= _prev_cap_ml, for delta)
#     _baseline_manual  â€“ last seen manual_ml (for delta)
#     _accumulated_ml   â€“ running water total up to the last poll
#     _accumulated_drinks, _last_drink_ml, _last_drink_ts
#     _water_remaining, _prev_cap_ml, _baseline_date
#     _persist_version  â€“ currently 4
#
#   _persist_version is bumped when drink-counting logic changes materially.
#   On startup, if the saved version < current version, stale drink
#   accumulators are reset to 0 (water_remaining is preserved since it
#   reflects physical state).  This prevents carryover of inflated counts
#   from older buggy code.
#
# -- DRINK COUNTING (SDK-VERIFIED) ------------------------------------------
#
#   Original SDK behavior (HydrationRepo.m3823q / ReadLogsCommand.java):
#     - ONLY log entries are used for the daily water total
#     - GET_HYDRATIONS / GET_SYNC_INFO counters are NOT used for display
#     - Daily total = SUM(amount) from hydration_table Room DB
#     - Each 'amount' = prev_level - current_level from consecutive entries
#
#   Data sources:
#
#   1. 'l' (VOLUME_AS_ML) log entry deltas    <- PRIMARY
#      prev_level - current_level >= 30 mL = 1 drink.
#      SDK: ReadLogsCommand case 'l' -> HydrationRepo.m3823q().
#      level=0 is valid (empty bottle) - original SDK has NO level<=0 guard.
#      'L'/'U' entries only update prev_level/water_remaining, never drinks.
#
#   2. delta_cap (cap_ml - prev_cap_ml) LAST-RESORT fallback
#      NOT in the original SDK.  Used ONLY when _fetch_log_entries returned
#      0 entries (log read failure).  If log entries were read (even
#      housekeeping-only), the absence of 'l' entries is treated as
#      authoritative: no drinks occurred.  See BUG FIX note in
#      _compute_daily_water for details on the phantom accumulation bug
#      that this guard prevents.
#
#   3. 'U+00DC' (0xDC HYDRATION_V2) via m3822p()  <- PARTIALLY USED BY SDK
#      The original SDK DOES process 'U+00DC' entries (extraData * 5 = mL).
#      Our code currently ignores them because m3822p() appeared to be gated
#      on a premium/cloud-sync flag (m3818k).  This may cause undercounting
#      if the device sometimes sends 'U+00DC' without a matching 'l' entry.
#      TODO: investigate whether pv=12 firmware always pairs 'U+00DC' with 'l'.
#
#   _water_level_from_log flag prevents double-counting between log-based
#   and cap_ml-delta paths: set True after processing log entries, checked
#   in _compute_daily_water to skip delta_cap when logs were available.
#
# â”€â”€ L/U RAW ADC NOTE â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
#
#   On pv=12 firmware, 'L'/'U' entries carry raw ultrasonic ADC (10kâ€“30k
#   range), not mL.  'l' and 'Ã' reliably carry firmware-converted mL.
#   L/U are included in _LEVEL_OPCODES only to update prev_level/water_
#   remaining if they happen to fall within 2Ã— bottle_capacity (sane guard).
#   They NEVER increment the drink counter (_DRINK_OPCODES = {'l'} only).
#
# ===========================================================================

# See protocol.py for BLE packet builders, parsers, and data tables.
# See the inline comments in protocol.py for the full BLE wire-protocol
# reference (packet framing, opcode table, TypeConfig registry, etc.).
# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------

class WaterioCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """
    Home Assistant DataUpdateCoordinator for a Water.io Smart Bottle cap.

    Connects over BLE, reads standard GATT characteristics (battery,
    firmware, manufacturer) and sends proprietary protocol commands
    (GET_HYDRATIONS, GET_CAP_STATE) via the CDD1/CDD2 write+notify channel
    discovered through reverse engineering the APK.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, mac: str, name: str) -> None:
        # Poll interval: read from options (set via UI options flow), fall back
        # to UPDATE_INTERVAL constant (default 60 s).
        poll_seconds = int((entry.options or {}).get("poll_interval", UPDATE_INTERVAL))
        super().__init__(
            hass,
            LOGGER,
            name=f"Water.io {name}",
            update_interval=timedelta(seconds=poll_seconds),
            config_entry=entry,
        )
        self._mac   = mac
        self._name  = name
        self._client: BleakClient | None = None
        self._notify_char: str | None    = None
        self._write_char:  str | None    = None
        self._data: dict[str, Any]       = {}

        # Settings dict: TypeConfig values persisted between polls.
        # Priority: DEFAULT_SETTINGS < config entry options < device-reported values.
        self._settings: dict[str, Any] = {**DEFAULT_SETTINGS}
        if entry.options:
            self._settings.update(entry.options)
        # Pre-populate _data so every settings entity has a value from first boot
        self._data.update(self._settings)

        # Sanity-guard: daily_goal_ml must be a plausible value (100–5000 mL).
        # Older firmware/parse bugs could write garbage into config entry options.
        _goal = self._data.get(FIELD_DAILY_GOAL_ML)
        if not isinstance(_goal, (int, float)) or not (100 <= int(_goal) <= 5000):
            self._data[FIELD_DAILY_GOAL_ML]     = DEFAULT_SETTINGS[FIELD_DAILY_GOAL_ML]
            self._settings[FIELD_DAILY_GOAL_ML] = DEFAULT_SETTINGS[FIELD_DAILY_GOAL_ML]
            if entry.options and FIELD_DAILY_GOAL_ML in entry.options:
                try:
                    cleaned = {k: v for k, v in entry.options.items()
                               if k != FIELD_DAILY_GOAL_ML}
                    hass.config_entries.async_update_entry(entry, options=cleaned)
                    LOGGER.warning(
                        "Purged corrupted daily_goal_ml=%s from config entry options", _goal
                    )
                except Exception:
                    pass

        # Runtime-only BLE + poll state
        self._log_queue: asyncio.Queue | None = None
        self._water_level_from_log: bool = False
        self._had_any_log_entries: bool = False
        self._pre_fetch_drinks: int = 0
        self._ble_lock: asyncio.Lock = asyncio.Lock()

        # Restore persisted daily hydration state + last-known sensor values
        self._restore_state(entry.options or {})

    # ------------------------------------------------------------------
    # Public properties (used by sensor entities)
    # ------------------------------------------------------------------

    @property
    def mac(self) -> str:
        return self._mac

    @property
    def device_name(self) -> str:
        return self._name

    # kept for backward compatibility with old sensor.py
    @property
    def battery(self) -> int | None:
        return self._data.get(FIELD_BATTERY)

    # ------------------------------------------------------------------
    # State initialization helpers
    # ------------------------------------------------------------------

    def _restore_state(self, opts: dict) -> None:
        """Restore daily hydration accumulators and last-known BLE field values.

        Called once from __init__ after all instance attributes are set up.
        Reads persisted data from config entry options and pre-populates
        self._data so sensors have values immediately on startup (before the
        first BLE sync).
        """
        _PERSIST_VERSION = 4
        saved_version = int(opts.get("_persist_version", 0))

        if saved_version < _PERSIST_VERSION:
            LOGGER.info(
                "Persistence version %d → %d: resetting stale daily drink "
                "accumulators (old values: drinks=%s, water_ml=%s, remaining=%s)",
                saved_version, _PERSIST_VERSION,
                opts.get("_accumulated_drinks", 0),
                opts.get("_accumulated_ml", 0),
                opts.get("_water_remaining", 0),
            )
            # Preserve water_remaining and baseline_cap_ml (physical bottle state).
            # Reset only drink counts inflated by old buggy code.
            self._daily_baseline_cap_ml     = int(opts.get("_baseline_cap_ml", 0))
            self._daily_baseline_manual     = int(opts.get("_baseline_manual", 0))
            self._daily_accumulated_ml      = 0
            self._daily_accumulated_drinks  = 0
            self._daily_last_drink_ml       = 0
            self._daily_last_drink_ts       = ""
            self._daily_water_remaining     = int(opts.get("_water_remaining", 0))
            self._prev_cap_ml               = int(opts.get("_prev_cap_ml", self._daily_baseline_cap_ml))
        else:
            self._daily_baseline_cap_ml     = int(opts.get("_baseline_cap_ml", 0))
            self._daily_baseline_manual     = int(opts.get("_baseline_manual", 0))
            self._daily_accumulated_ml      = int(opts.get("_accumulated_ml", 0))
            self._daily_accumulated_drinks  = int(opts.get("_accumulated_drinks", 0))
            self._daily_last_drink_ml       = int(opts.get("_last_drink_ml", 0))
            self._daily_last_drink_ts       = str(opts.get("_last_drink_ts", ""))
            self._daily_water_remaining     = int(opts.get("_water_remaining", 0))
            self._prev_cap_ml               = int(opts.get("_prev_cap_ml", self._daily_baseline_cap_ml))

        saved_date_str: str | None = opts.get("_baseline_date")
        try:
            from datetime import date as _date
            self._daily_today_date = _date.fromisoformat(saved_date_str) if saved_date_str else None
        except (ValueError, TypeError):
            self._daily_today_date = None

        # Initialize water_remaining to bottle capacity if not yet set
        # (first boot or migration from older version without remaining tracking).
        if self._daily_water_remaining <= 0 and self._daily_today_date is not None:
            _bv = int(self._settings.get(FIELD_BOTTLE_VOLUME, 0))
            self._daily_water_remaining = BOTTLE_VOLUME_ML.get(_bv, 500)

        # Reconstruct computed hydration fields from accumulators.
        # No BLE needed — everything is already in memory from opts above.
        if self._daily_today_date is not None:  # skip on very first boot
            _goal_ml = int(self._settings.get(FIELD_DAILY_GOAL_ML, 2000)) or 2000
            self._data[FIELD_WATER_ML]           = self._daily_accumulated_ml
            self._data[FIELD_DRINK_COUNT_TODAY]   = self._daily_accumulated_drinks
            self._data[FIELD_LAST_DRINK_ML]       = self._daily_last_drink_ml
            self._data[FIELD_LAST_DRINK_TS]       = self._daily_last_drink_ts or None
            self._data[FIELD_WATER_REMAINING_ML]  = self._daily_water_remaining
            self._data[FIELD_HYDRATION_LEVEL]     = min(
                100, round(self._daily_accumulated_ml / _goal_ml * 100)
            )

        # Restore BLE-read-only fields (battery, firmware, cap state, …) from
        # "_snap_<field>" keys so sensors show stale data instead of "Unknown"
        # until the first successful BLE sync.
        for _f in SNAP_FIELDS:
            _v = opts.get(f"_snap_{_f}")
            if _v is not None:
                self._data[_f] = _v

        # Pre-populate journal entry count from the existing journal file.
        try:
            _jp = self._journal_path
            if os.path.exists(_jp):
                with open(_jp, "r", encoding="utf-8") as _jf:
                    _j = json.load(_jf)
                self._data[FIELD_JOURNAL_ENTRIES] = sum(s.get("log_count", 0) for s in _j)
            else:
                self._data[FIELD_JOURNAL_ENTRIES] = 0
        except Exception:
            self._data[FIELD_JOURNAL_ENTRIES] = 0

        # Restore incremental log-read watermark: number of entries that were
        # on the device at the end of the last successful sync.  Next sync
        # starts reading from this offset so we never re-read old entries.
        self._log_device_count: int = int(opts.get("_log_device_count", 0))

    # BLE notification handler
    # ------------------------------------------------------------------

    def _on_notification(self, _sender: int, raw: bytearray) -> None:
        """Handle incoming notifications from the cap."""
        # READ_LOGS (0x0E) responses: during an active log-reading session they
        # go to the dedicated streaming queue.  Outside a session, update
        # water_remaining_ml in real-time from any L/U measurement push.
        if raw and raw[0] == 0x0E:
            if self._log_queue is not None:
                self._log_queue.put_nowait(bytes(raw))
                return
            # Autonomous 0x0E push: data[4..7]=ts, data[8]=opCode, data[9..10]=level
            if len(raw) >= 12:
                ev = chr(raw[8]) if 32 <= raw[8] < 127 else ""
                if ev in ("L", "U", "l"):
                    level = struct.unpack_from("<H", raw, 9)[0]
                    # On pv=12, L/U carry raw ultrasonic ADC (10k-30k), not mL.
                    # Only trust values within 2Ã— bottle capacity.
                    _bottle = BOTTLE_VOLUME_ML.get(
                        self._data.get("bottle_volume_type", 0), 500
                    )
                    if 0 <= level <= _bottle * 2:
                        self._daily_water_remaining = level
                        self._data[FIELD_WATER_REMAINING_ML] = level
                        LOGGER.info("Real-time measurement: type=%s  remaining=%dmL", ev, level)
                        self.async_set_updated_data(dict(self._data))
                    elif level > _bottle * 2:
                        LOGGER.info(
                            "Skipping real-time push type=%s level=%d (> %d, likely raw ADC)",
                            ev, level, _bottle * 2,
                        )
            return
        parsed = parse_notification(raw)
        if parsed:
            # Don't let intermediate notifications overwrite computed fields
            # that _compute_daily_water manages (prevents 0% hydration_level
            # flicker and stale water_ml from raw device bytes).
            # NOTE: FIELD_LOG_COUNT is NOT suppressed - _read_logs_async needs
            # it in self._data to know how many log entries to read.
            #
            # FIELD_CAP_ML and FIELD_MANUAL_ML are also suppressed from
            # intermediate pushes.  GET_HYDRATIONS (interval) and GET_SYNC_INFO
            # (daily cumulative) both try to set these fields, and pushing a
            # transient interval value (e.g. 490) < the previous daily cumulative
            # (e.g. 2370) during the sync causes the TOTAL_INCREASING sensor's
            # statistics to detect a false reset and inflate the displayed total.
            # The fields STILL get stored in self._data (update happens before
            # the push filtering), so _compute_daily_water sees the latest values.
            _SUPPRESS_FROM_PUSH = {
                FIELD_HYDRATION_LEVEL, FIELD_WATER_ML,
                FIELD_DRINK_COUNT_TODAY, FIELD_LAST_DRINK_ML,
                FIELD_LAST_DRINK_TS, FIELD_WATER_REMAINING_ML,
                FIELD_DAILY_GOAL_ML,
                FIELD_CAP_ML, FIELD_MANUAL_ML,
            }
            # Update self._data with ALL parsed fields (including cap_ml)
            # so _compute_daily_water sees the latest values from the device.
            self._data.update(parsed)
            # Build a push dict WITHOUT suppressed fields - entities for those
            # get their final stable values from the coordinator return dict
            # at the end of the sync cycle (_async_update_data return).
            push = {k: v for k, v in self._data.items() if k not in _SUPPRESS_FROM_PUSH}
            self.async_set_updated_data(push)

    # ------------------------------------------------------------------
    # Internal BLE helpers
    # ------------------------------------------------------------------

    async def _ensure_connected(self) -> bool:
        """Connect (or verify already connected) and discover characteristics."""
        if self._client and self._client.is_connected:
            return True

        # Prefer HA's bluetooth registry device so bleak-retry-connector has
        # full advertisement data for reliable connection establishment.
        ble_device = bluetooth.async_ble_device_from_address(
            self.hass, self._mac, connectable=True
        )

        LOGGER.info(
            "Connecting to Water.io cap '%s' (%s)  ble_device=%s",
            self._name, self._mac,
            ble_device.name if ble_device else "not in registry â€“ using raw MAC",
        )
        try:
            if ble_device is not None:
                self._client = await establish_connection(
                    BleakClientWithServiceCache,
                    ble_device,
                    self._name,
                    max_attempts=3,
                )
            else:
                # HA bluetooth registry doesn't know this device yet (e.g. first
                # connection attempt before a scan has seen the advertisement).
                # Fall back to a direct BleakClient connection.
                client = BleakClient(self._mac, timeout=20.0)
                await client.connect()
                self._client = client
            LOGGER.info("Connected to %s", self._mac)
        except BleakError as exc:
            LOGGER.error("BLE connect failed  %s  %s", self._mac, exc)
            self._client = None
            return False
        except Exception as exc:
            LOGGER.error("Unexpected BLE error  %s  %s", self._mac, exc)
            self._client = None
            return False

        await asyncio.sleep(0.5)          # give stack time to settle
        await self._discover_chars()
        return True

    async def _discover_chars(self) -> None:
        """
        Walk GATT services, find the best write + notify characteristics
        from the proprietary UUIDs discovered during APK reverse engineering.

        Falls back to subscribing to ANY notify characteristic so that the
        raw data appears in the HA logs â€“ useful for further protocol study
        when testing against real hardware.
        """
        if not self._client:
            return

        self._notify_char = None
        self._write_char  = None

        notify_lc = [u.lower() for u in NOTIFY_CANDIDATES]
        write_lc  = [u.lower() for u in WRITE_CANDIDATES]

        # Dump the full GATT table at DEBUG level (enable debug logging for
        # custom_components.waterio to see it).  Useful for identifying real
        # UUIDs when updating NOTIFY_CANDIDATES / WRITE_CANDIDATES.
        for svc in self._client.services:
            LOGGER.debug("GATT svc  %s", svc.uuid)
            for ch in svc.characteristics:
                props = ch.properties
                ulc   = ch.uuid.lower()
                LOGGER.debug("  char  %s  props=%s", ch.uuid, list(props))

                if self._notify_char is None and "notify" in props and ulc in notify_lc:
                    self._notify_char = ch.uuid
                    LOGGER.debug("  >>> MATCHED notify: %s", ch.uuid)

                if self._write_char is None and (
                    "write" in props or "write-without-response" in props
                ) and ulc in write_lc:
                    self._write_char = ch.uuid
                    LOGGER.debug("  >>> MATCHED write:  %s", ch.uuid)

        # Subscribe to the chosen notify characteristic
        if self._notify_char:
            try:
                await self._client.start_notify(self._notify_char, self._on_notification)
                LOGGER.info("Subscribed to notifications on %s", self._notify_char)
                # Extra settle time: HA bluetooth proxy needs longer than direct BleakClient
                # before the device starts routing notification callbacks reliably.
                await asyncio.sleep(0.8)
            except BleakError as exc:
                LOGGER.warning("start_notify failed for %s: %s", self._notify_char, exc)
        else:
            # Diagnostic fallback: subscribe to everything that supports notify.
            # Errors are logged so we can see which chars the device rejects.
            LOGGER.warning(
                "No known notify char found on %s â€“ attempting subscribe on all "
                "notify characteristics listed above.",
                self._mac,
            )
            for svc in self._client.services:
                for ch in svc.characteristics:
                    if "notify" in ch.properties:
                        try:
                            await self._client.start_notify(ch.uuid, self._on_notification)
                            LOGGER.warning("Diagnostic subscribe OK:   %s", ch.uuid)
                        except Exception as exc:
                            LOGGER.warning("Diagnostic subscribe FAIL: %s  %s", ch.uuid, exc)

    async def _read_standard_gatt(self) -> None:
        """Read Bluetooth SIG standardised characteristics."""
        if not self._client or not self._client.is_connected:
            return

        reads: list[tuple[str, str, Any]] = [
            (UUID_BATTERY_LEVEL, FIELD_BATTERY,      lambda d: d[0]),
            (UUID_FIRMWARE_REV,  FIELD_FIRMWARE,     lambda d: d.decode("utf-8", errors="replace").strip()),
            (UUID_HARDWARE_REV,  FIELD_HARDWARE,     lambda d: d.decode("utf-8", errors="replace").strip()),
            (UUID_MANUFACTURER,  FIELD_MANUFACTURER, lambda d: d.decode("utf-8", errors="replace").strip()),
            (UUID_SERIAL_NUMBER, FIELD_SERIAL,       lambda d: d.decode("utf-8", errors="replace").strip()),
        ]

        for uuid, field, parser in reads:
            try:
                raw = await self._client.read_gatt_char(uuid)
                self._data[field] = parser(bytearray(raw))
            except (BleakError, KeyError):
                pass  # not all firmware versions expose every characteristic
            except Exception:
                pass  # GATT read error

    async def _write_cmd(self, opcode: int, payload: bytes = b"") -> bool:
        """Write a command byte to the write characteristic (v4.8.6 packet format)."""
        return await self._write_cmd_v4(opcode, payload)

    async def _fetch_protocol_data(self) -> None:
        """
        Send the proprietary v4.8.6 commands and wait for notification
        callbacks to populate self._data.

        Command sequence (SDK HydrationDefaultCapCmds, no reset):
          1. SET_REAL_TIME (0x11)  â€“ sync RTC; BE timestamp
          2. GET_CAP_STATE (0x3C)  â€“ full state: water, battery, status, flags
          3. GET_HYDRATIONS (0x72) â€“ cumulative manual + cap-measured ml
          4. GET_SYNC_INFO (0x78)  â€“ base goal, extra goal, running totals
          5. GET_BATTERY (0x1B)    â€“ backup battery %
          6. GET_VERSION (0x1D)    â€“ firmware version string
        """
        if not self._write_char:
            LOGGER.warning("No write char â€“ cannot fetch protocol data")
            return

        # 1. SET_REAL_TIME â€“ sync RTC (SDK step 1 of HydrationDefaultCapCmds)
        pkt_time = build_time_cmd()
        try:
            await self._client.write_gatt_char(self._write_char, pkt_time, response=False)
            LOGGER.debug("SET_REAL_TIME sent: %s", pkt_time.hex())
        except BleakError as exc:
            LOGGER.warning("SET_REAL_TIME failed: %s", exc)
        await asyncio.sleep(0.5)

        # 1b. Sync timezone offset from HA to device (TypeConfig type 0).
        #     The cap uses this for drink-reminder scheduling (working hours).
        ha_tz = dt_util.get_time_zone(self.hass.config.time_zone)
        if ha_tz is not None:
            utc_offset_min = int(datetime.now(ha_tz).utcoffset().total_seconds() // 60)
            if utc_offset_min != self._data.get("timezone_offset_min", 0):
                tz_bytes = struct.pack("<h", utc_offset_min)
                pkt_tz = build_set_typeconfig(0, tz_bytes)
                try:
                    await self._client.write_gatt_char(self._write_char, pkt_tz, response=False)
                    self._data["timezone_offset_min"] = utc_offset_min
                    LOGGER.info("Synced timezone offset to device: %+d min", utc_offset_min)
                except BleakError as exc:
                    LOGGER.warning("Timezone sync failed: %s", exc)
                await asyncio.sleep(0.3)

        # 2. GET_CAP_STATE (0x3C) â€“ full state snapshot (water + battery + flags)
        if await self._write_cmd_v4(CMD_GET_CAP_STATE):
            await asyncio.sleep(1.5)

        # 3. GET_HYDRATIONS (0x72) â€“ cumulative manual + cap-measured ml
        if await self._write_cmd_v4(CMD_GET_HYDRATIONS):
            await asyncio.sleep(1.2)

        # 4. GET_SYNC_INFO (0x78) â€“ base goal, extra goal, running totals
        if await self._write_cmd_v4(CMD_GET_SYNC_INFO):
            await asyncio.sleep(1.0)

        # 5. GET_BATTERY (0x1B) â€“ battery level backup
        if await self._write_cmd_v4(CMD_GET_BATTERY):
            await asyncio.sleep(0.5)

        # 6. GET_VERSION (0x1D) â€“ firmware version string
        if await self._write_cmd_v4(CMD_GET_VERSION):
            await asyncio.sleep(0.5)

        # 7. GET_REAL_TIME (0x10) â€“ device RTC
        if await self._write_cmd_v4(CMD_GET_REAL_TIME):
            await asyncio.sleep(0.5)

        # 8. GET_EXTRA_DAILY_GOAL (0x74) â€“ extra goal ml
        if await self._write_cmd_v4(CMD_GET_EXTRA_DAILY_GOAL):
            await asyncio.sleep(0.5)

        # 9. GET_SILENT_MODE (0x67) â€“ reminder silence flag
        if await self._write_cmd_v4(CMD_GET_SILENT_MODE):
            await asyncio.sleep(0.5)

        # 10. GET_MAC_ADDRESS (0x49)
        if await self._write_cmd_v4(CMD_GET_MAC_ADDRESS):
            await asyncio.sleep(0.5)

        # 11. GET_LOG_LENGTH (0x0D) â€“ total log entries on device
        if await self._write_cmd_v4(CMD_GET_LOG_LENGTH):
            await asyncio.sleep(0.5)

        # 12. READ_LOGS (0x0E) â€“ read recent log entries for L/U/N water level.
        await self._read_logs_async()

        # 13. CLEAR_LOGS (0x0F) â€“ clear device log after reading, same as the
        #     original app's HydrationDefaultCapCmds final step.  Keeps the buffer
        #     small so the next sync only sees new entries (no need to seek to tail).
        #     NOTE: CLEAR_LOGS MAY or MAY NOT reset cap_ml (GET_HYDRATIONS /
        #     GET_SYNC_INFO / GET_CAP_STATE each report cumulative values that
        #     behave differently).  _compute_daily_water uses a delta-based
        #     approach (cap_ml âˆ’ prev_cap_ml) that works either way.
        if await self._write_cmd_v4(CMD_CLEAR_LOGS):
            await asyncio.sleep(0.5)
            LOGGER.info("CLEAR_LOGS sent â€“ device log buffer cleared")
        self._data[FIELD_LOG_COUNT] = 0
        # Reset the incremental-read watermark: CLEAR_LOGS just reset the
        # device log counter to 0, so the next sync must read from offset 0.
        # Without this reset: start_offset = min(old_watermark, new_count)
        # which always equals new_count, so the skip-guard
        # (start_offset >= log_count) fires and returns [] forever —
        # every drink logged after a sync is silently discarded.
        self._log_device_count = 0
        LOGGER.debug("CLEAR_LOGS: watermark reset to 0")

        # Note: CMD_GET_SINGLE_MEAS (0x01) was tested but pv=12 firmware
        # ACKs without producing a measurement notification.
        # Water remaining comes from L/U/l log entries (firmware-converted mL).
        # If no log entries are available, cap_ml delta heuristic is the fallback.

    async def _write_cmd_v4(self, opcode: int, payload: bytes = b"") -> bool:
        """Write a v4.8.6 format command: [opcode, 0x00, 0x00, len, payload...]

        Uses compact sizing â€” GET commands (empty payload) are sent as 4 bytes.
        The original app and waterio_fetch.py both use 4-byte packets for GET;
        the device firmware ignores trailing zeros but some firmware revisions
        stop responding to GET commands when they arrive padded to 8 bytes.
        """
        if not self._client or not self._client.is_connected:
            return False
        if not self._write_char:
            return False
        # Compact: exactly 4 + len(payload) bytes, no extra zero padding
        pkt = make_cmd(opcode, payload, size=4 + len(payload))
        try:
            await self._client.write_gatt_char(self._write_char, pkt, response=False)
            return True
        except BleakError as exc:
            LOGGER.warning("Write failed 0x%02x: %s", opcode, exc)
            return False

    # ------------------------------------------------------------------
    # DataUpdateCoordinator interface
    # ------------------------------------------------------------------

    async def _fetch_log_entries(self) -> list[dict]:
        """Stream all device log entries over BLE; return raw parsed records.

        Manages the self._log_queue lifecycle.  Callers should pass the
        returned list to _process_log_entries after this returns.

        SDK strategy (ReadLogsCommand.java m4141C / m4143E / mo3870o):
          1. Send batch request: [0x0E, 0x00, 0x00, 0x04, offset_LE2, end_LE2]
             where end = offset + BATCH_SIZE (SDK uses 50).
          2. Device responds with 1+ notifications, each containing up to 16
             eight-byte log entries.  data[3] = number of entries in THIS packet.
          3. If data[3] == 16 (full page), request next batch from current count.
             If data[3] < 16 or == 0, all entries have been read.
          4. Each 8-byte record: [ts_LE4, opCode, mMeasurement_LE2, extraData]
        """
        log_count = self._data.get(FIELD_LOG_COUNT, 0) or 0
        if not self._write_char or log_count == 0:
            return []

        _BATCH_SIZE = 50    # SDK default: request 50 entries at a time
        _MAX_ENTRIES = 2003  # SDK hard limit: f3009m.size() > 2002

        # Incremental read: skip entries already seen in previous syncs.
        # CLEAR_LOGS resets the device log counter to 0 after every sync,
        # so the watermark (_log_device_count) should normally be 0 here.
        # If the watermark exceeds log_count, the device was cleared and
        # we MUST read from 0 — NOT from log_count (the old min() logic
        # silently set start_offset = log_count, which hit the skip guard
        # and discarded every entry forever).
        if self._log_device_count > log_count:
            LOGGER.info(
                "READ_LOGS: watermark %d > device log_count %d — "
                "device was cleared, resetting to 0",
                self._log_device_count, log_count,
            )
            self._log_device_count = 0
        start_offset = self._log_device_count
        if start_offset > 0:
            LOGGER.debug(
                "READ_LOGS: incremental — skipping %d already-seen entries, "
                "reading %d new of %d total",
                start_offset, log_count - start_offset, log_count,
            )
        if start_offset >= log_count:
            LOGGER.debug("READ_LOGS: nothing new (device count=%d, last seen=%d)", log_count, start_offset)
            return []

        # Hard wall-clock budget: stop reading before HA's setup watchdog fires.
        # Partial progress is saved so the next sync continues from where we left off.
        _BUDGET_S = 20.0
        _deadline = asyncio.get_event_loop().time() + _BUDGET_S

        self._log_queue = asyncio.Queue()
        all_entries: list[dict] = []
        budget_exhausted = False
        try:
            offset = start_offset
            while offset < log_count and len(all_entries) < _MAX_ENTRIES:
                # Hard time budget — stop before HA's task-cancellation watchdog.
                if asyncio.get_event_loop().time() >= _deadline:
                    LOGGER.warning(
                        "READ_LOGS: %.0fs budget exhausted at offset %d/%d — "
                        "saving partial watermark, will resume next sync",
                        _BUDGET_S, start_offset + len(all_entries), log_count,
                    )
                    budget_exhausted = True
                    break

                # Guard: BLE connection may have dropped mid-sync
                if not self._client or not self._client.is_connected:
                    LOGGER.warning(
                        "READ_LOGS: BLE disconnected at offset %d – aborting log read", offset
                    )
                    break
                end = min(offset + _BATCH_SIZE, log_count)
                pkt = build_read_logs_cmd(offset, count=end - offset)
                try:
                    await self._client.write_gatt_char(self._write_char, pkt, response=False)
                except (BleakError, AttributeError) as exc:
                    LOGGER.warning("READ_LOGS batch @%d failed: %s", offset, exc)
                    break

                # Drain all notification packets for this batch.
                # Device sends 1+ packets, each with up to 16 entries (8 bytes each).
                # data[3] = entry count in THIS packet; 0 = no more data.
                batch_done = False
                while not batch_done:
                    try:
                        data = await asyncio.wait_for(self._log_queue.get(), timeout=3.0)
                    except asyncio.TimeoutError:
                        batch_done = True
                        break

                    entry_count = data[3] if len(data) >= 4 else 0
                    if entry_count == 0:
                        batch_done = True
                        break

                    # Parse ALL 8-byte entries in this packet
                    for i in range(entry_count):
                        start = 4 + i * 8
                        if start + 8 <= len(data):
                            all_entries.append(parse_log_entry(bytes(data[start:start + 8])))

                    # data[3] < 16 means final packet for this batch
                    if entry_count < 16:
                        batch_done = True

                offset = start_offset + len(all_entries)

            from collections import Counter
            types = Counter(e["type"] for e in all_entries)
            LOGGER.info(
                "READ_LOGS: %d new entries read (offset %d→%d of %d total), types=%s",
                len(all_entries), start_offset, start_offset + len(all_entries),
                log_count, dict(types),
            )
            # Advance watermark: partial if budget was exhausted, full otherwise.
            self._log_device_count = (
                start_offset + len(all_entries) if budget_exhausted else log_count
            )
            return all_entries
        except asyncio.CancelledError:
            # HA cancelled the task (e.g. setup watchdog). Save partial progress
            # so the next sync resumes from here rather than starting from 0.
            self._log_device_count = start_offset + len(all_entries)
            LOGGER.warning(
                "READ_LOGS: task cancelled at offset %d/%d — partial watermark saved",
                self._log_device_count, log_count,
            )
            raise
        finally:
            self._log_queue = None

    async def _process_log_entries(self, all_entries: list[dict]) -> None:
        """Save raw log entries to the journal and process drink/refill events.

        Hydration processing (matching original SDK HydrationRepo.m3823q):
          - 'l' (0x6C): level DOWN >= 30 mL from prev → DRINK  ← only source
          - 'L'/'U'    : update prev_level/water_remaining only, NO drink count
          - 'Ð' (0xD0) : explicit firmware refill → update prev_level, no drink
          - level UP from prev → REFILL (silent update, no drink entry in SDK)
          - 'Ü' (0xDC) : IGNORED (SDK m3822p gated on premium flag, see notes)

        Multi-day retro-sync: when the bottle hasn't synced for several days,
        entries spanning multiple calendar days are processed in order.  Only
        entries from TODAY accumulate into the daily drink counters.
        """
        # Save ALL raw entries to the journal BEFORE processing — data is safe
        # even if the processing logic below crashes.
        await self._save_journal(all_entries)

        _REFILL_OPCODE = chr(0xD0)  # 'Ð' firmware refill event
        # Opcodes that carry water level in mL (all update water_remaining).
        _LEVEL_OPCODES = {"L", "U", "l", _REFILL_OPCODE}
        # Only 'l' (VOLUME_AS_ML) triggers drink counting, matching the
        # original SDK: ReadLogsCommand only calls HydrationRepo.m3823q()
        # for case 'l' — NOT for 'L' / 'U'.
        _DRINK_OPCODES = {"l"}

        bottle_cap = BOTTLE_VOLUME_ML.get(self._data.get("bottle_volume_type", 0), 500)
        processable = [e for e in all_entries if e["type"] in _LEVEL_OPCODES]

        LOGGER.info(
            "READ_LOGS: %d processable (L/U/l/0xD0) of %d total entries",
            len(processable), len(all_entries),
        )

        if not processable:
            return

        processable.sort(key=lambda e: e["ts"])

        # Detect multi-day span for logging
        from datetime import date as _date_cls
        first_day = _date_cls.fromtimestamp(processable[0]["ts"])
        last_day  = _date_cls.fromtimestamp(processable[-1]["ts"])
        if first_day != last_day:
            LOGGER.info(
                "Multi-day retro-sync: entries span %s → %s (%d days)",
                first_day.isoformat(), last_day.isoformat(),
                (last_day - first_day).days + 1,
            )

        today_start = int(
            dt_util.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        )
        # Seed prev_level from persisted water_remaining so the first log
        # entry can detect a drink/refill relative to the last sync.
        prev_level: int | None = (
            self._daily_water_remaining if self._daily_water_remaining > 0 else None
        )
        last_level: int | None = None
        # Sanity threshold: levels beyond 2× bottle capacity are raw ADC values
        # (pv=12 firmware emits L/U as raw ultrasonic readings, NOT mL).
        _MAX_SANE_ML = bottle_cap * 2
        past_drinks_total = 0
        past_ml_total = 0

        for entry in processable:
            ev    = entry["type"]
            level = entry["level"]
            extra = entry["extra"]
            if level < 0 or level >= 0xFFFF:
                continue
            # level=0 is valid for 'l' (VOLUME_AS_ML) — means bottle is empty.
            # For L/U (raw ADC), 0 is technically impossible but harmless:
            # L/U are not in _DRINK_OPCODES and the _MAX_SANE_ML guard below
            # handles ADC range filtering.
            if level > _MAX_SANE_ML:
                LOGGER.info(
                    "Skipping entry type=%s level=%d (> %d, likely raw ADC)  raw=%s",
                    ev, level, _MAX_SANE_ML, entry.get("raw", ""),
                )
                continue
            is_today = entry["ts"] >= today_start
            ts_iso = datetime.fromtimestamp(entry["ts"], tz=timezone.utc).isoformat(
                timespec="seconds"
            )

            if ev == _REFILL_OPCODE:
                # Firmware-confirmed refill — sub-type in mExtraData
                subtype_name = {1: "DAR", 2: "DBR", 3: "BOTH"}.get(extra, f"?{extra}")
                LOGGER.info(
                    "Refill event 0xD0 (%s): post-refill=%dmL  ts=%s",
                    subtype_name, level, ts_iso,
                )
                prev_level = level
            else:
                # L/U/l — all carry mL from firmware; update water level.
                # Only 'l' entries count as drinks (matches original SDK).
                if prev_level is not None:
                    delta = prev_level - level
                    if delta >= 30 and ev in _DRINK_OPCODES:
                        # Drink detected (>=30 mL threshold — HydrationRepo.m3820m)
                        if is_today:
                            self._daily_accumulated_drinks += 1
                            self._daily_accumulated_ml += delta
                            self._daily_last_drink_ml = delta
                            self._daily_last_drink_ts = ts_iso
                        else:
                            past_drinks_total += 1
                            past_ml_total += delta
                        LOGGER.info(
                            "Drink: %dmL → %dmL  consumed=%dmL  type=%s  today=%s  ts=%s",
                            prev_level, level, delta, ev, is_today, ts_iso,
                        )
                    elif delta < 0:
                        # Level went UP → refill (silent update, no drink DB entry)
                        LOGGER.info(
                            "Refill detected: %dmL → %dmL  type=%s  ts=%s",
                            prev_level, level, ev, ts_iso,
                        )
                prev_level = level

            last_level = level

        if past_drinks_total:
            LOGGER.info(
                "Retro-sync: %d past-day drinks totalling %dmL (not added to today)",
                past_drinks_total, past_ml_total,
            )

        if last_level is not None and 0 < last_level <= bottle_cap * 4:
            self._daily_water_remaining = last_level
            self._data[FIELD_WATER_REMAINING_ML] = last_level
            self._water_level_from_log = True
            LOGGER.info(
                "Water remaining (from log): %dmL  (last L/U/l/0xD0 entry)", last_level,
            )

    async def _read_logs_async(self) -> None:
        """Fetch log entries from device and process hydration events."""
        entries = await self._fetch_log_entries()
        self._had_any_log_entries = len(entries) > 0
        await self._process_log_entries(entries)

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch all data from the cap (called by the coordinator scheduler).

        Connect â†’ read everything â†’ disconnect.  We never hold the connection
        open between polls â€” that would drain the bottle's battery.

        When the device is unreachable (user away from home, BLE out of range)
        we return the last known data instead of raising UpdateFailed so that
        sensors stay "available" (showing stale values) until the 24-hour
        staleness timeout kicks in.  This lets the user always see when the
        last successful sync was.
        """
        async with self._ble_lock:
            if not await self._ensure_connected():
                # Device not reachable â€” return last known data if we have any
                self._data["ble_reachable"] = False
                if self._data.get(FIELD_LAST_SYNC):
                    LOGGER.info(
                        "BLE connect failed â€“ returning last known data  "
                        "last_sync=%s", self._data.get(FIELD_LAST_SYNC),
                    )
                    public = {k: v for k, v in self._data.items()
                              if not k.startswith("_")}
                    public["ble_reachable"] = False
                    return public
                raise UpdateFailed(f"Cannot connect to Water.io cap {self._mac}")

            try:
                # Mark BLE reachable immediately so every mid-sync
                # async_set_updated_data() call (from _on_notification) carries
                # ble_reachable=True.  Without this, writable entities flicker
                # "unavailable" on every intermediate notification during the sync.
                self._data["ble_reachable"] = True
                await self._read_standard_gatt()      # battery, firmware, manufacturer â€¦
                # Snapshot drink counter BEFORE log reading so new-day reset can
                # distinguish persisted-yesterday count from today's log entries.
                self._pre_fetch_drinks = self._daily_accumulated_drinks
                await self._fetch_protocol_data()     # proprietary hydration + cap-state
                # Set FIELD_LAST_SYNC BEFORE _compute_daily_water so that
                # _persist_baseline (called at the end of _compute_daily_water)
                # saves the current timestamp in _snap_last_sync.  Previously
                # it was set after, so the persisted snap was always one sync
                # behind and the sensor showed the old time after an HA restart.
                self._data[FIELD_LAST_SYNC] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                self._compute_daily_water()           # cap-delta fallback (no-log path only)

                # Strip private accumulator fields (prefixed with '_') from the
                # dict returned to the coordinator â€” they're internal state only.
                public_data = {k: v for k, v in self._data.items() if not k.startswith("_")}
                public_data["ble_reachable"] = True
                LOGGER.info("Water.io update complete  device=%s  data=%s", self._name, public_data)
                return public_data
            finally:
                # Always disconnect after the poll cycle â€” do NOT hold the BLE link
                # open between polls.  Saves bottle battery and avoids Android app
                # being locked out of its own device.
                await self.disconnect()

    def _compute_daily_water(self) -> None:
        """Single source of truth for ALL daily hydration entities.

        Refill detection algorithm (matching original Water.io app logic):

          Original app (HydrationRepo / MeasurementEntity):
            if current_level > last_level â†’ REFILL (water went UP)
            if current_level < last_level â†’ DRINK  (water went DOWN, min 30 mL)
            After refill â†’ assume bottle is full (bottle_capacity).

          On pv=12 firmware (RCH04) L/U log entries may be absent.  In that
          case we detect refills from cap_ml (cumulative consumption) deltas:
            if delta > current_remaining â†’ user consumed more water than
            was left â†’ at least one refill must have happened.

          For refills with NO subsequent drink (cap_ml unchanged), a
          "Refill Bottle" button is available for the user to signal the
          refill manually.

        Data flow:
          GET_HYDRATIONS (0x72) â†’ cap_ml (cumulative â€” may or may not reset on CLEAR_LOGS)
          READ_LOGS (0x0E)     â†’ L/U/l/Ã measurements (actual water level)
          DELTA = cap_ml âˆ’ prev_cap_ml  â†’ new consumption since last poll

        Entities driven:
          FIELD_WATER_ML           â€“ total mL consumed today
          FIELD_DRINK_COUNT_TODAY  â€“ number of drinks today
          FIELD_LAST_DRINK_ML      â€“ mL consumed in last drink
          FIELD_LAST_DRINK_TS      â€“ ISO timestamp of last drink
          FIELD_WATER_REMAINING_ML â€“ water remaining in bottle
        """
        cap_ml    = self._data.get(FIELD_CAP_ML,    0) or 0
        manual_ml = self._data.get(FIELD_MANUAL_ML, 0) or 0
        today     = dt_util.now().date()

        bottle_cap = BOTTLE_VOLUME_ML.get(
            self._data.get("bottle_volume_type", 0), 500
        )

        # â”€â”€ New calendar day â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        if self._daily_today_date != today:
            # _read_logs_async already ran this poll and may have added today's
            # drinks into the accumulators on top of yesterday's persisted count.
            # Isolate today's log contribution using the pre-fetch snapshot.
            # _daily_accumulated_drinks may include yesterday's persisted count +
            # today's drinks just added by _read_logs_async.  Subtract the
            # snapshot to get only what log processing added for today.
            today_drinks = max(0, self._daily_accumulated_drinks - self._pre_fetch_drinks)
            LOGGER.info(
                "New day %s â€” resetting daily water tracking  cap_ml=%d  "
                "log contributed today: %d drinks",
                today, cap_ml, today_drinks,
            )
            self._daily_today_date          = today
            self._daily_accumulated_ml      = 0
            self._daily_accumulated_drinks  = today_drinks
            self._daily_baseline_cap_ml     = cap_ml    # this poll's cap IS the baseline
            self._daily_baseline_manual     = manual_ml
            # Preserve last-drink info only if log produced a drink today
            if not today_drinks:
                self._daily_last_drink_ml   = 0
                self._daily_last_drink_ts   = ""
            # Preserve water_remaining â€” bottle still has water from yesterday
            if self._daily_water_remaining <= 0:
                self._daily_water_remaining = bottle_cap
            self._prev_cap_ml               = cap_ml   # baseline for delta
            # Fall through â€” compute totals correctly below instead of returning early

        # â”€â”€ Delta-based accounting â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        # cap_ml may be daily-cumulative (NOT reset by CLEAR_LOGS) â€” at least
        # one of GET_CAP_STATE / GET_HYDRATIONS / GET_SYNC_INFO reports a
        # value that keeps growing across syncs within the same day.
        #
        # The safe approach: compute DELTA from prev_cap_ml.
        #   - cap_ml >= prev â†’ normal growth, delta = cap_ml - prev
        #   - cap_ml <  prev â†’ genuine counter reset (CLEAR_LOGS did clear
        #     it, or device midnight reset), delta = cap_ml
        #
        # This handles BOTH scenarios (reset and no-reset) correctly and
        # never double-counts.
        delta_cap = cap_ml - self._prev_cap_ml
        if delta_cap < 0:
            # Counter was reset (e.g., CLEAR_LOGS actually cleared it this
            # time, or device midnight reset).  Everything in cap_ml is new.
            delta_cap = cap_ml

        delta_manual = manual_ml - self._daily_baseline_manual
        if delta_manual < 0:
            delta_manual = manual_ml

        LOGGER.info(
            "Delta accounting: cap_ml=%d  prev=%d  delta_cap=%d  "
            "manual=%d  base_manual=%d  delta_manual=%d  accumulated=%d",
            cap_ml, self._prev_cap_ml, delta_cap,
            manual_ml, self._daily_baseline_manual, delta_manual,
            self._daily_accumulated_ml,
        )

        # â”€â”€ Drink / refill detection (cap_ml delta, fallback only) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        # When the log gave us real L/U/Ã entries: _read_logs_async already
        # processed every drink and refill event in order, updated
        # _daily_accumulated_drinks / _daily_last_drink_* / _daily_water_remaining,
        # and set _water_level_from_log = True.  We trust that and skip the
        # cap_ml-delta heuristic to avoid double-counting.
        #
        # When log entries WERE read (even housekeeping-only like CONNECTED,
        # VOLTAGE, HYDRATION_STATE): the absence of 'l' entries is
        # authoritative evidence that no drinking occurred.  The original
        # Water.io SDK ONLY uses log entries for water tracking (never
        # GET_SYNC_INFO), so we trust the log data exclusively.
        #
        # Cap_ml delta is a LAST-RESORT fallback: used ONLY when log reading
        # failed completely (0 entries returned despite device reporting
        # log_count > 0).  In all other cases, log-based data is definitive.
        #
        # BUG FIX: previously, any sync without 'l' entries (including normal
        # housekeeping syncs) would trigger the cap_ml delta path.  GET_SYNC_INFO's
        # cap_ml counter changes between syncs for non-drink reasons (measurement
        # events, HYDRATION_STATE updates, etc.), causing phantom water
        # accumulation of ~800mL per hourly sync cycle.
        if (not self._water_level_from_log
                and not self._had_any_log_entries
                and delta_cap > 5):
            remaining_before = self._daily_water_remaining

            if delta_cap > remaining_before + 10:
                # Consumed more than what was in the bottle â†’ refill(s) happened.
                available = remaining_before
                n_refills = 0
                while available < delta_cap:
                    available += bottle_cap
                    n_refills += 1
                new_remaining = available - delta_cap
                LOGGER.info(
                    "Refill detected (cap_ml): consumed %dmL > remaining %dmL â†’ "
                    "%d refill(s)  new_remaining=%dmL",
                    delta_cap, remaining_before, n_refills, new_remaining,
                )
                self._daily_water_remaining = max(0, new_remaining)
            else:
                self._daily_water_remaining = max(0, remaining_before - delta_cap)

            self._daily_accumulated_drinks += 1
            self._daily_last_drink_ml = delta_cap
            self._daily_last_drink_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
            LOGGER.info(
                "New drink detected (cap_ml fallback): +%dmL  drink #%d today  remaining=%dmL",
                delta_cap, self._daily_accumulated_drinks, self._daily_water_remaining,
            )

        had_log_data = self._water_level_from_log
        had_any_logs = self._had_any_log_entries
        self._water_level_from_log = False  # reset for next poll
        self._had_any_log_entries = False    # reset for next poll

        # Compute totals
        # Three paths:
        #   1. LOG-BASED (primary): _process_log_entries already added each
        #      drink's mL into _daily_accumulated_ml.  Only add delta_manual.
        #   2. LOGS-READ-NO-DRINKS: log entries were read but none were drink
        #      events — authoritative evidence of no consumption.  Only manual.
        #   3. CAP_ML-DELTA (last resort): when log reading FAILED (0 entries
        #      despite device reporting log_count > 0), use delta_cap as a
        #      rough estimate.  Unreliable but better than nothing.
        #
        # This prevents the inflation bug caused by FIELD_CAP_ML being
        # written by 3 different BLE responses (GET_CAP_STATE, GET_HYDRATIONS,
        # GET_SYNC_INFO) with inconsistent reset semantics.
        if had_log_data:
            water_ml = self._daily_accumulated_ml + delta_manual
            LOGGER.info(
                "Totals (LOG-BASED): accumulated=%d + delta_manual=%d = %d",
                self._daily_accumulated_ml, delta_manual, water_ml,
            )
        elif had_any_logs:
            # Log entries were read but no L/U/l/0xD0 entries found.
            # This is normal for housekeeping syncs (CONNECTED, VOLTAGE, etc.).
            # The absence of drink entries is definitive — do NOT use cap_ml delta.
            water_ml = self._daily_accumulated_ml + delta_manual
            LOGGER.info(
                "Totals (LOGS-READ-NO-DRINKS): accumulated=%d + delta_manual=%d = %d  "
                "(cap_ml delta %d suppressed — log had no drink events)",
                self._daily_accumulated_ml, delta_manual, water_ml, delta_cap,
            )
        else:
            water_ml = self._daily_accumulated_ml + delta_cap + delta_manual
            LOGGER.info(
                "Totals (CAP_ML fallback): accumulated=%d + delta_cap=%d + delta_manual=%d = %d",
                self._daily_accumulated_ml, delta_cap, delta_manual, water_ml,
            )

        # â”€â”€ Write all entities â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        self._data[FIELD_WATER_ML]          = water_ml
        self._data[FIELD_DRINK_COUNT_TODAY]  = self._daily_accumulated_drinks
        self._data[FIELD_LAST_DRINK_ML]      = self._daily_last_drink_ml
        self._data[FIELD_LAST_DRINK_TS]      = self._daily_last_drink_ts or None
        self._data[FIELD_WATER_REMAINING_ML] = self._daily_water_remaining

        # Override device-reported hydration_level with our own computation.
        # The device byte[7] resets to 0 after CLEAR_LOGS / counter resets,
        # but our water_ml tracks the real daily total.
        goal = self._data.get(FIELD_DAILY_GOAL_ML) or 2000
        self._data[FIELD_HYDRATION_LEVEL] = min(100, round(water_ml / goal * 100))

        LOGGER.info(
            "Daily: water=%dmL  drinks=%d  last=%dmL@%s  remaining=%dmL",
            water_ml, self._daily_accumulated_drinks,
            self._daily_last_drink_ml, self._daily_last_drink_ts or "?",
            self._daily_water_remaining,
        )

        # â”€â”€ Prepare for next poll â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        # Save accumulated total and current cap/manual positions so the NEXT
        # poll computes only the NEW delta.  This is correct whether or not
        # CLEAR_LOGS resets cap_ml:
        #   - If cap_ml resets to 0: next poll sees cap_ml < prev â†’ delta = cap_ml
        #   - If cap_ml stays cumulative: next poll sees cap_ml >= prev â†’ delta = diff
        self._daily_accumulated_ml  = water_ml
        self._prev_cap_ml           = cap_ml
        self._daily_baseline_manual = manual_ml
        self._daily_baseline_cap_ml = cap_ml  # persisted for restart recovery

        # Persist after every poll so HA restart recovers everything
        self._persist_baseline()

    @property
    def _journal_path(self) -> str:
        """Absolute path to the per-device journal JSON file."""
        mac_clean = self._mac.replace(":", "").upper()
        return self.hass.config.path(f"waterio_journal_{mac_clean}.json")

    def _journal_write_blocking(self, session: dict, path: str) -> int:
        """Blocking file I/O â€” must be called via run_in_executor only."""
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                journal: list = json.load(f)
        else:
            journal = []
        journal.append(session)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(journal, f, ensure_ascii=False, indent=2)
        return sum(s.get("log_count", 0) for s in journal)

    async def _save_journal(self, all_entries: list[dict]) -> None:
        """Append this sync session's raw log entries to the journal JSON file.

        File lives at <HA config>/waterio_journal_<MAC>.json.
        Structure: list of session objects, newest appended last.
        Each session: { sync_ts, device_mac, log_count, entries[] }.
        Each entry:   { ts, ts_iso, type, type_name, level, extra, raw }.
        """
        if not all_entries:
            return
        sync_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        session = {
            "sync_ts": sync_ts,
            "device_mac": self._mac,
            "log_count": len(all_entries),
            "entries": [
                {
                    "ts":        e["ts"],
                    "ts_iso":    datetime.fromtimestamp(
                                     e["ts"], tz=timezone.utc
                                 ).isoformat(timespec="seconds"),
                    "type":      e["type"],
                    "type_name": OPCODE_NAMES.get(
                                     e["type"],
                                     f"0x{ord(e['type']):02X}"
                                 ),
                    "level":     e["level"],
                    "extra":     e["extra"],
                    "raw":       e["raw"],
                }
                for e in all_entries
            ],
        }
        path = self._journal_path
        try:
            total = await self.hass.async_add_executor_job(
                self._journal_write_blocking, session, path
            )
            self._data[FIELD_JOURNAL_ENTRIES] = total
            LOGGER.info(
                "Journal: +%d entries saved  total=%d  path=%s",
                len(all_entries), total, path,
            )
        except Exception as exc:
            LOGGER.warning("Journal save failed: %s", exc)

    async def async_clear_journal(self) -> None:
        """Delete the journal file (called by the Clear Journal button)."""
        path = self._journal_path
        try:
            await self.hass.async_add_executor_job(os.remove, path)
            LOGGER.info("Journal cleared: %s", path)
        except FileNotFoundError:
            pass
        except Exception as exc:
            LOGGER.warning("Journal clear failed: %s", exc)
        self._data[FIELD_JOURNAL_ENTRIES] = 0
        self.async_set_updated_data(dict(self._data))

    def _persist_baseline(self) -> None:
        """Save the current daily baseline into config entry options so it
        survives an HA restart within the same calendar day."""
        try:
            new_options = dict(self.config_entry.options)
            new_options["_persist_version"]     = 4
            new_options["_baseline_cap_ml"]     = self._daily_baseline_cap_ml
            new_options["_baseline_manual"]     = self._daily_baseline_manual
            new_options["_accumulated_ml"]      = self._daily_accumulated_ml
            new_options["_accumulated_drinks"]  = self._daily_accumulated_drinks
            new_options["_last_drink_ml"]       = self._daily_last_drink_ml
            new_options["_last_drink_ts"]       = self._daily_last_drink_ts
            new_options["_water_remaining"]     = self._daily_water_remaining
            new_options["_prev_cap_ml"]         = self._prev_cap_ml
            new_options["_baseline_date"]       = self._daily_today_date.isoformat() if self._daily_today_date else ""
            new_options["_log_device_count"]     = self._log_device_count
            # Snapshot BLE-read-only fields so they survive HA restarts
            for _f in SNAP_FIELDS:
                _v = self._data.get(_f)
                if _v is not None:
                    new_options[f"_snap_{_f}"] = _v
            self.hass.config_entries.async_update_entry(self.config_entry, options=new_options)
        except Exception:
            pass  # Could not persist daily baseline

    async def disconnect(self) -> None:
        """Cleanly disconnect from the BLE device."""
        if self._client and self._client.is_connected:
            try:
                await self._client.disconnect()
            except Exception:
                pass
        self._client = None

    # ------------------------------------------------------------------
    # Settings write API (called by number/switch/select entities)
    # ------------------------------------------------------------------

    async def _ble_write(
        self,
        build_packet: Callable[[], bytearray],
        *,
        raise_on_disconnect: bool = True,
    ) -> bool:
        """Acquire BLE lock, connect, write a single packet, then disconnect.

        Args:
            build_packet: Zero-argument callable that returns the packet bytes.
            raise_on_disconnect: When True (default) raise HomeAssistantError if
                the device cannot be reached; when False, log a warning and
                return False.

        Returns True on success.  Re-raises on write failure.
        """
        async with self._ble_lock:
            if not await self._ensure_connected():
                if raise_on_disconnect:
                    raise HomeAssistantError(f"Water.io {self._mac} not connected")
                LOGGER.warning("BLE not reachable for write: %s", self._mac)
                return False
            try:
                pkt = build_packet()
                await self._client.write_gatt_char(self._write_char, pkt, response=False)
                return True
            except Exception as exc:
                LOGGER.error("BLE write failed: %s", exc)
                raise
            finally:
                await self.disconnect()

    async def async_set_typeconfig(self, type_byte: int, value: object) -> None:
        """Write a single TypeConfig setting via SET_MULTI_PARAM_CONFIG (0x4D).

        Encodes `value` according to TYPECONFIG_FIELDS[type_byte][1] and sends
        the TLV packet to the device, then persists the new value in config
        entry options so it survives HA restarts.

        Special case: type_byte=3 (daily_goal_ml) — device stores a goal INDEX,
        not raw mL.  We convert the mL value to the nearest goal index before
        sending, and persist the actual mL that the index maps to.
        """
        if type_byte not in TYPECONFIG_FIELDS:
            raise ValueError(f"Unknown TypeConfig type_byte: {type_byte}")

        field_key, fmt = TYPECONFIG_FIELDS[type_byte]

        # Daily goal: convert mL → goal index for the device
        if type_byte == 3 and isinstance(value, (int, float)):
            idx = ml_to_goal_index(int(value))
            actual_ml = GOAL_INDEX_ML[idx]
            wire_value = idx
            LOGGER.info("Goal: user=%dmL → index=%d → actual=%dmL", int(value), idx, actual_ml)
        else:
            wire_value = value
            actual_ml = None

        value_bytes = typeconfig_encode(fmt, wire_value)
        pkt = build_set_typeconfig(type_byte, value_bytes)
        await self._ble_write(lambda: pkt)
        LOGGER.info(
            "TypeConfig SET type=0x%02x (%s) value=%s raw=%s",
            type_byte, field_key, value, pkt.hex(),
        )

        # Optimistic update — for daily goal persist the actual mL, not the index
        decoded = typeconfig_decode(fmt, value_bytes)
        if actual_ml is not None:
            decoded = actual_ml
        self._settings[field_key] = decoded
        self._data[field_key] = decoded
        self.async_set_updated_data(dict(self._data))
        new_options = dict(self.config_entry.options)
        new_options[field_key] = decoded
        self.hass.config_entries.async_update_entry(self.config_entry, options=new_options)

    async def async_set_typeconfig_by_key(self, field_key: str, value: object) -> None:
        """Convenience wrapper: look up type_byte from field key and call async_set_typeconfig."""
        if field_key not in FIELD_TO_TYPEBYTE:
            raise ValueError(f"No TypeConfig mapping for field: {field_key}")
        await self.async_set_typeconfig(FIELD_TO_TYPEBYTE[field_key], value)

    async def async_set_silent_mode(self, enabled: bool) -> None:
        """Set silent mode (reminder silence) via dedicated CMD_SET_SILENT_MODE command."""
        pkt = make_cmd(CMD_SET_SILENT_MODE, bytes([1 if enabled else 0]), size=5)
        await self._ble_write(lambda: pkt)
        LOGGER.info("Silent mode SET: %s", enabled)
        self._settings[FIELD_SILENT_MODE] = enabled
        self._data[FIELD_SILENT_MODE] = enabled
        self.async_set_updated_data(dict(self._data))
        new_options = dict(self.config_entry.options)
        new_options[FIELD_SILENT_MODE] = enabled
        self.hass.config_entries.async_update_entry(self.config_entry, options=new_options)

    async def async_find_bottle(self) -> None:
        """Locate the bottle with LED blink + vibration (matches original app behaviour)."""
        # 1. Start LED blink — opcode 0x1A, DEFAULT pattern [0x00, 0x00]
        blink_pkt = make_cmd(CMD_START_BLINK, bytes([0x00, 0x00]), size=5)
        await self._ble_write(lambda: blink_pkt, raise_on_disconnect=False)
        # 2. Start vibration — opcode 0x77, intensity as LE uint16 (500 ≈ medium pulse)
        vib_pkt = make_cmd(CMD_START_VIBRATION, struct.pack("<H", 500), size=6)
        if await self._ble_write(lambda: vib_pkt, raise_on_disconnect=False):
            LOGGER.info("Find bottle (blink + vibrate) sent to %s", self._mac)

    async def async_set_extra_goal(self, ml: int) -> None:
        """Set extra daily hydration goal via dedicated CMD_SET_EXTRA_DAILY_GOAL command."""
        pkt = make_cmd(CMD_SET_EXTRA_DAILY_GOAL, struct.pack("<H", ml), size=6)
        await self._ble_write(lambda: pkt)
        LOGGER.info("Extra daily goal SET: %dmL", ml)
        self._data[FIELD_EXTRA_GOAL_ML] = ml
        self.async_set_updated_data(dict(self._data))
        new_options = dict(self.config_entry.options)
        new_options[FIELD_EXTRA_GOAL_ML] = ml
        self.hass.config_entries.async_update_entry(self.config_entry, options=new_options)


# Backward-compatibility alias (old code references WaterioInstance)
WaterioInstance = WaterioCoordinator
