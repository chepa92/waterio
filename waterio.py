"""Water.io BLE coordinator – reverse-engineered from APK v3.5.0 / v4.8.6 (latest)."""
from __future__ import annotations

import asyncio
import struct
from datetime import datetime, timedelta, timezone
from typing import Any

from bleak import BleakClient, BleakError, BleakScanner
from bleak.backends.device import BLEDevice
from bleak_retry_connector import establish_connection, BleakClientWithServiceCache

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    LOGGER,
    DEVICE_NAME_PREFIX,
    UPDATE_INTERVAL,
    # Standard GATT
    UUID_BATTERY_LEVEL, UUID_FIRMWARE_REV, UUID_HARDWARE_REV,
    UUID_MANUFACTURER, UUID_SERIAL_NUMBER,
    # Proprietary – discovery lists
    NOTIFY_CANDIDATES, WRITE_CANDIDATES,
    # Commands (confirmed from EnumCommandDevice.java SDK)
    CMD_RESET_DEVICE, CMD_SET_REAL_TIME,
    CMD_GET_CAP_STATE, CMD_GET_HYDRATIONS,
    CMD_GET_BATTERY, CMD_GET_VERSION,
    CMD_GET_REAL_TIME, CMD_GET_LOG_LENGTH,
    CMD_GET_SYNC_INFO,
    CMD_GET_SILENT_MODE, CMD_SET_SILENT_MODE,
    CMD_GET_MAC_ADDRESS,
    CMD_READ_LOGS, CMD_GET_NEXT_LOGS, CMD_CLEAR_LOGS,
    CMD_GET_SINGLE_MEAS,
    CMD_SET_EXTRA_DAILY_GOAL, CMD_GET_EXTRA_DAILY_GOAL,
    CMD_SET_MULTI_CONFIG, CMD_GET_MULTI_CONFIG,
    # Response opcodes (echo same opcode; generic ACK = 0x5A)
    RESP_STATUS_PUSH, RESP_ACK,
    RESP_GET_CAP_STATE, RESP_GET_HYDRATIONS,
    RESP_GET_BATTERY, RESP_GET_VERSION,
    RESP_GET_REAL_TIME, RESP_GET_LOG_LENGTH,
    RESP_GET_SYNC_INFO, RESP_GET_EXTRA_GOAL,
    RESP_GET_SILENT_MODE, RESP_GET_MAC,
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
    FIELD_BOTTLE_VOLUME,
    # TypeConfig
    TYPECONFIG_FIELDS, FIELD_TO_TYPEBYTE,
    DEFAULT_SETTINGS,
)


# ---------------------------------------------------------------------------
# BLE helpers
# ---------------------------------------------------------------------------

async def discover() -> list[BLEDevice]:
    """Discover Water.io BLE devices nearby."""
    devices = await BleakScanner.discover(timeout=10.0)
    return [
        d for d in devices
        if d.name and d.name.startswith(DEVICE_NAME_PREFIX)
    ]


def _make_cmd(opcode: int, payload: bytes = b"", size: int = 8) -> bytearray:
    """
    Build v4.8.6 BLE command packet:
        [opcode, 0x00, 0x00, payload_len, ...payload...] padded to `size` bytes.
    Confirmed from EnumCommandDevice <clinit> bytecode analysis.
    """
    header = bytearray([opcode, 0x00, 0x00, len(payload)])
    raw = header + bytearray(payload)
    if len(raw) < size:
        raw += bytearray(size - len(raw))
    return raw[:size]


# Keep legacy alias used below
_build_cmd = _make_cmd


# ===========================================================================
# WATER.IO BLE PROTOCOL REFERENCE
# Reverse-engineered from SDK source (JADX decompile of io.water.hydration
# v4.8.6 APK).  Source files: ReadLogsCommand.java, EnumCommandDevice.java,
# AutoAddBaseCommands.java, DARType.java, DBRType.java, HydrationRepo.java,
# CapStateResponse.java, TypeConfig.java and subclasses.
# ===========================================================================
#
# ── 0. ADVERTISEMENT DATA (DEVICE STATUS, NOT WATER LEVEL) ─────────────────
#
#   The cap broadcasts BLE advertisement service data on UUID 0x180A
#   (Device Information).  The FULL raw scan record has:
#     bArr[21]   = event type ('O'=Open / 'C'=Close)     (= svc_data payload[0])
#     bArr[22-23]= raw measurement (LE uint16, raw ADC)  (= svc_data payload[1-2])
#     bArr[24]   = extra data                             (= svc_data payload[3])
#     bArr[25-26]= check value (LE uint16)                (= svc_data payload[4-5])
#     bArr[27]   = flag: 0x20 → Level, other → Refill     (= svc_data payload[6])
#     bArr[28]   = connection check (==2?)                 (= svc_data payload[7])
#     bArr[29]   = connection worker type                  (= svc_data payload[8])
#     bArr[30]   = bottle volume type (0=500mL, 1=750mL)  (= svc_data payload[9])
#   See: ReadAdvertisementLogHandler.java, WizardScanningMode.java
#
#   IMPORTANT: bArr[22-23] are RAW ultrasonic sensor values, NOT mL.
#   They require device-specific calibration that only the firmware has.
#   The app does NOT compute water-remaining from advertisement data.
#   Water level in mL comes exclusively from L/U/l log entries (0x0E cmd)
#   which contain FIRMWARE-CONVERTED mL values.
#
# ── 1. PACKET FRAMING ──────────────────────────────────────────────────────
#
#   All host→device commands:          [opcode, 0x00, 0x00, plen, ...payload]
#   All device→host notifications:     [opcode, 0x00, 0x00, plen, ...payload]
#   Exception: 0x3C (GET_CAP_STATE) uses byte[3] as protocolVersion, not plen.
#
# ── 2. LOG ENTRY FRAME (CMD_READ_LOGS 0x0E / CMD_GET_NEXT_LOGS 0x42) ───────
#
#   READ_LOGS command payload:   [offset_LE2, end_LE2]   (4 bytes)
#     – SDK m4141C() requests entries in batches of 50: [offset, offset+50].
#     – Device responds with 1+ notifications, each containing up to 16
#       eight-byte entries.  data[3] = entry count in THIS packet (0–16).
#     – If data[3] == 16 (full page), request next batch from current count.
#     – If data[3] < 16 or == 0, all entries have been sent.
#
#   Response notification (12 bytes total, source: ReadLogsCommand.m4143E()):
#     byte[0]    = 0x0E  (opcode echo)
#     byte[1..2] = 0x00, 0x00
#     byte[3]    = plen  (8 = valid entry; 0 = end-of-log sentinel)
#     byte[4..7] = mTimeStamp  LE uint32  (Unix seconds → ×1000 in SDK for ms)
#     byte[8]    = opCode      ISO-8859-1 char  (see table below)
#     byte[9..10]= mMeasurement LE uint16  (mL remaining after event, OR raw ADC)
#     byte[11]   = mExtraData  uint8  (interpretation depends on opCode)
#
#   LogEntry opCode table (source: ReadLogsCommand.m4145w() switch statement):
#
#   opCode  chr  name                   mMeasurement          mExtraData
#   ─────── ──── ─────────────────────  ────────────────────  ────────────────────────────────────
#   0x41    'A'  CONNECTED              last-log-index (uint16) battery/sync info
#   0x42    'B'  CALIBRATED             –                     –
#   0x43    'C'  CLOSED_CAP             –                     –
#   0x44    'D'  DISCONNECTED           –                     disconnect reason
#   0x45    'E'  MEASURE_NOT_VALID      raw ADC               error code
#   0x46    'F'  TEMPERATURE            °C × 10               –
#   0x47    'G'  BUTTON_PRESSED         button type           0=none 1=short 2=long1 4=long2 8=dbl
#   0x48    'H'  HYDRATION_CHECK        –                     –
#   0x4C    'L'  MEASUREMENT_ACCURATE   mL or RAW ADC (see note¹)    signed std-dev byte
#   0x4D    'M'  MEASURE_EVENT          raw sensor capture    –
#   0x4E    'N'  LAST_VOLUME_EVENT      last known mL         –
#   0x4F    'O'  OPENED_CAP             –                     –
#   0x50    'P'  PAIR                   –                     –
#   0x51    'Q'  REMINDER_SEQUENCE      –                     –
#   0x52    'R'  REMINDER               Voltage mV            –
#   0x53    'S'  START_SHAKE            –                     –
#   0x54    'T'  EXIT_HIGH_TEMP         raw ADC               sensor node id
#   0x55    'U'  MEASUREMENT_ESTIMATED  mL or RAW ADC (see note¹)    signed std-dev byte
#   0x56    'V'  VOLTAGE                Voltage mV            –
#   0x58    'X'  SCHEDULER_EVENT        –                     –
#   0x59    'Y'  LIQUID_TYPE            –                     liquid type ID
#   0x5A    'Z'  RESET_DEVICE           –                     reset reason
#   0x5E    '^'  SIGNAL_RATE            –                     rate value
#   0x5F    '_'  SIGNAL_RATE_MIN        –                     min rate
#   0x61    'a'  (not seen)
#   0x63    'c'  TOP_CLOSE              –                     –
#   0x67    'g'  STOP_TILT              –                     –
#   0x68    'h'  SLEEP_DEVICE           –                     –
#   0x6C    'l'  VOLUME_AS_ML           mL remaining (firmware-converted) ← ALWAYS mL!
#                                        HydrationRepo.m3823q(mL, ts, "hydration", mac)
#
#   ¹ NOTE on L/U vs l:  On pv=12 firmware (RCH04.05.03.41), 'L' and 'U' entries
#     carry RAW ultrasonic sensor readings (e.g. 11140, 16580) — NOT mL.  Only
#     'l' (VOLUME_AS_ML) and 'Ð' (0xD0) reliably carry firmware-converted mL.
#     Our code filters out entries with level > 2× bottle_capacity as raw ADC.
#   0x6F    'o'  TOP_OPEN               –                     –
#   0x72    'r'  PRE_REMINDER           –                     –
#   0x73    's'  STOP_SHAKE             –                     –
#   0x74    't'  ENTER_HIGH_TEMP        raw ADC               sensor node id
#   0xC8    'È'  MEASUREMENT_COUNTERS   –                     counter value
#   0xC9    'É'  UPDATE_MANUAL_HYDRATION–                     –
#   0xCA    'Ê'  UPDATE_MEAS_HYDRATION  –                     –
#   0xCB    'Ë'  UPDATE_EXTRA_GOAL      –                     extra goal mL
#   0xD0    'Ð'  FIRMWARE_REFILL        mL remaining (post-ref) sub-type  ← KEY for refill!
#                                        sub-type 1 = DAR (Drink After Refill)
#                                        sub-type 2 = DBR (Drink Before Refill)
#                                        sub-type 3 = BOTH
#                                        HydrationRepo.m3823q(mL, ts, "refill-N", mac)
#   0xD1    'Ñ'  SEQUENCE_PATTERN       –                     –
#   0xD3    'Ó'  HYDRATION_STATE        state bitmask (uint16) (byte 11 unused)
#                                        mMeasurement = state machine bitmask:
#                                          0=IDLE  1=DAY_STARTED  2=HYD_START_DAILY_GOAL
#                                          4=DAY_ENDED  8=MEASURE_READY
#                                          16=HYD_PARAMETERS_CHANGE  32=HYD_TIME_CHANGE
#                                          64=PRACTICE_STARTED  128=PRACTICE_ENDED
#                                        mExtraData = copy of mMeasurement (not raw byte 11)
#                                        SDK: setExtraData(measurement); no HydrationRepo call
#                                        EventDesc: "Hydration (DAY_STARTED)" etc.
#   0xD5    'Õ'  BOTTLE_NOT_STABLE      –                     source: 0=App 1=Cap35s 2=Cap150s
#   0xD6    'Ö'  RE_MEASURE             –                     –
#   0xD7    '×'  EXIT_LOW_POWER         –                     –
#   0xD8    'Ø'  EXIT_VERY_LOW_POWER    –                     –
#   0xD9    'Ù'  CAP_OVERFLOW_MEMORY    –                     –
#   0xDA    'Ú'  LED_STATUS_COUNTER     –                     counter
#   0xDB    'Û'  BACKUP_HEAP            (ignored)             (ignored)
#                                        Firmware diagnostic: cap performed heap backup
#                                        (flash memory preservation).  App sets only
#                                        eventDesc="Backup heap"; no fields consumed.
#   0xDC    'Ü'  HYDRATION_V2           byte[9]*5=mL_lo  byte[10]*5=mL_hi  byte[11]*5=extra
#                                        HydrationRepo.m3822p(ts, lo, hi, extra, "hydrationV2", mac)
#   0xDD    'Ý'  RESTORE_DAILY_SUM      –                     –
#   0xDE    'Þ'  RESTORE_EXTRA_GOAL     –                     –
#   0xE6    'æ'  SHABBAT_MODE           –                     –
#   0xE7    'ç'  INIT_ENTER_SHABBAT     –                     –
#   0xE8    'è'  INIT_EXIT_SHABBAT      –                     –
#   0xE9    'é'  BOTTLE_NOT_STABLE_OLD  –                     source byte
#   0xEB    'ë'  SILENT_MODE            (ignored)             1=active 0=inactive
#                                        mMeasurement computed but NOT stored.
#                                        mExtraData = raw byte 11: 1=muted, 0=normal.
#                                        Logs when cap silent-mode state changes.
#                                        Related cmds: GET(0x67) / SET(0x66) SILENT_MODE.
#   0xEE    'î'  VIBRATION_TEST         –                     test id
#   0x31..  '1'..'5'  CHARGE_START/STOP/FULL/LOW/VERYLOW  charge event
#   0x36..  '6'..'8'  CONFIG_CHANGE / DARK_MODE_ENTER / DARK_MODE_EXIT
#
#   NOTE ON 'L' AND 'U': on older firmware the ADC→mL conversion happens
#   internally and L/U carry mL directly.  On pv=12 firmware (RCH04.05.03.41),
#   L/U carry RAW ultrasonic readings (10000–30000 range) while ONLY 'l' and
#   'Ð' (0xD0) carry firmware-converted mL.  Our code uses _MAX_SANE_ML guard
#   (2× bottle_capacity) to skip raw-ADC L/U entries automatically.
#   Drink detection: level DOWN ≥ 30 mL = drink.  Level UP = refill.
#
#   NOTE ON 'Ü' (0xDC) vs 'l':  On pv=12 firmware, each cap open/close cycle
#   produces BOTH a 'l' entry (absolute water level in mL) AND a 'Ü' entry
#   (drink amount = extra×5 mL).  These are REDUNDANT — processing both would
#   double-count drinks.  We use 'l' as primary (via prev_level delta) and
#   only fall back to 'Ü' if no 'l' entries were in the batch.
#
# ── 3. REFILL DETECTION — HOW IT WORKS ────────────────────────────────────
#
#   The cap firmware generates 0xD0 (Ð) log entries when it detects a refill.
#   This happens ONLY when the DAR or DBR TypeConfig settings are enabled
#   (type bytes 38 and 39 respectively).  By default both are False = disabled,
#   which is why we see no 0xD0 events unless you turn on the switches in HA.
#
#   DAR = Drink-After-Refill  (TypeConfig type 0x26 = 38)
#     → firmware emits 0xD0 sub-type 1 when: level UP then level DOWN ≥ MAR
#   DBR = Drink-Before-Refill  (TypeConfig type 0x27 = 39)
#     → firmware emits 0xD0 sub-type 2 when: level DOWN ≥ MBR then level UP
#   BOTH: 0xD0 sub-type 3 when both transitions happen in same cycle.
#
#   MAR (type 40): minimum mL after refill to qualify as DAR  (default 200 mL)
#   MBR (type 41): minimum mL before refill to qualify as DBR (default 200 mL)
#   IGR (type 42): ignore-gap threshold – refill below this mL is discarded
#
#   Enable DAR/DBR via the HA switch entities or TypeConfig SET (0x4D):
#     _build_set_typeconfig(38, b'\x01')   # DAR on
#     _build_set_typeconfig(39, b'\x01')   # DBR on
#
#   Once enabled, every refill generates a 0xD0 entry in the log with:
#     mMeasurement = post-refill water level in mL  (exact, firmware-measured)
#     mExtraData   = sub-type (1=DAR, 2=DBR, 3=BOTH)
#
# ── 4. TYPECONFIG PROTOCOL (SET 0x4D / GET 0x4E) ─────────────────────────
#
#   SET_MULTI_PARAM_CONFIG (0x4D) — write one TypeConfig setting:
#     [0x4D, 0x00, 0x00, total_len, val_len+1, type_byte, ...value_bytes]
#     where total_len = val_len + 3  (plen header + type + value)
#
#   GET_MULTI_PARAM_CONFIG (0x4E) — read settings (DEAD on RCH04.05.03.41):
#     [0x4E, 0x00, 0x00, total_len, 0x00, 0xFF, 0x01, type_byte, ...]
#     Response format mirrors SET: type_byte + value_bytes per each item.
#     Firmware RCH04.05.03.41 returns no data for GET — all settings are
#     write-only in practice; read-back from the device is not possible.
#
#   TypeConfig type byte registry (source: p141X2/*.java, AutoAddBaseCommands):
#     0  = TimeZone         (int16 LE, minutes from UTC)  ← NOTE: SDK sends hours, we send minutes
#     1  = WorkStart        (uint8, hour)
#     2  = WorkEnd          (uint8, hour)
#     3  = DailyGoal        (uint16 LE, mL)
#     5  = ReminderLogic    (bool, 0/1)
#     6  = ReminderInterval (uint16 LE, minutes)
#     7  = ReminderRound    (uint16 LE, minutes)
#     9  = ReminderCycles   (uint8)
#     12 = ReminderPattern  (uint8: 1=bounce, 2=pulse, 3=snake)
#     13 = ReminderUI       (uint8 bitmask: bit0=blink, bit1=sound, bit2=vibrate)
#     30 = ReminderLedsColor(N*3 bytes RGB)
#     36 = EnableOpenCloseLED (bool)
#     38 = DAR              (bool)  ← MUST be True for firmware to emit 0xD0 refill events
#     39 = DBR              (bool)  ← MUST be True for firmware to emit 0xD0 refill events
#     40 = MAR              (uint16 LE, mL — min mL after refill for DAR, default 200)
#     41 = MBR              (uint16 LE, mL — min mL before refill for DBR, default 200)
#     42 = IGR              (uint8, mL  — ignore-refill gap threshold, default 50)
#     44 = EnableReminderLED(bool)
#     45 = EnableStatusLED  (bool)
#     46 = EnableDemoMode   (bool)
#     49 = LEDOffOutsideHours(bool)
#     50 = EnableShabbatMode(bool)
#     52 = LEDOffInCharger  (bool)
#     53 = BottleVolume     (uint8: 0=500mL, 1=750mL)
#
# ── 5. RESP_GET_CAP_STATE (0x3C) LAYOUT ───────────────────────────────────
#
#   byte[3]    = protocolVersion  (NOT plen; e.g. 12 for RCH04 firmware)
#   byte[4]    = isClosed         (1 = cap is closed)
#   byte[5]    = mode flags       (bit0=active, bit1=coach/need_drink)
#   byte[6]    = hydrationStatus  (0=none, 1=low, 2=ok, 3=good)
#   byte[7]    = hydrationLevel % (device-side, unreliable after counter reset)
#   byte[10]   = isCharging       (pv==12)
#   byte[11]   = isCharging       (pv!=12)
#   byte[13]   = batteryCell %    (raw hardware reading)
#   byte[14..15]= for pv==12: accumulated daily hydration mL  (LE uint16)
#   byte[17..18]= for pv>=15: today's consumed mL             (LE uint16)
#   byte[19..20]= for pv>=17: today's manual mL               (LE uint16)
#
# ===========================================================================


# ---------------------------------------------------------------------------
# TypeConfig helpers  (SET 0x4D / GET 0x4E)
# ---------------------------------------------------------------------------

def _typeconfig_encode(fmt: str, value: object) -> bytes:
    """Encode a Python value to TypeConfig wire bytes according to format."""
    if fmt == "bool":
        return bytes([1 if value else 0])
    if fmt == "uint8":
        return bytes([int(value) & 0xFF])
    if fmt == "uint16":
        return struct.pack("<H", int(value) & 0xFFFF)
    if fmt == "uint16s":
        # signed int16 LE (e.g. timezone_offset_min can be negative)
        return struct.pack("<h", int(value))
    if fmt == "ui_elem":
        # value is int bitmask or dict with blink/sound/vibrate keys
        if isinstance(value, dict):
            bitmask = (int(value.get("blink", 0)) |
                       (int(value.get("sound", 0)) << 1) |
                       (int(value.get("vibrate", 0)) << 2))
        else:
            bitmask = int(value) & 0xFF
        return bytes([bitmask])
    if fmt == "rgb":
        # value is "#RRGGBB" string or list of such strings (multi-color)
        colors = value if isinstance(value, (list, tuple)) else [str(value)]
        result = bytearray()
        for c in colors:
            c = c.strip().lstrip("#")
            if len(c) == 6:
                result += bytes.fromhex(c)
        return bytes(result) or bytes([0xFF, 0x00, 0x00])
    raise ValueError(f"Unknown TypeConfig format: {fmt}")


def _typeconfig_decode(fmt: str, value_bytes: bytes) -> object:
    """Decode TypeConfig wire bytes to a Python value."""
    if not value_bytes:
        return None
    if fmt == "bool":
        return bool(value_bytes[0])
    if fmt == "uint8":
        return value_bytes[0]
    if fmt == "uint16":
        return struct.unpack_from("<H", value_bytes)[0] if len(value_bytes) >= 2 else None
    if fmt == "uint16s":
        return struct.unpack_from("<h", value_bytes)[0] if len(value_bytes) >= 2 else None
    if fmt == "ui_elem":
        b = value_bytes[0]
        return {"blink": bool(b & 1), "sound": bool(b & 2), "vibrate": bool(b & 4), "raw": b}
    if fmt == "rgb":
        colors = []
        for i in range(0, len(value_bytes) - 2, 3):
            colors.append(f"#{value_bytes[i]:02X}{value_bytes[i+1]:02X}{value_bytes[i+2]:02X}")
        return colors
    return value_bytes.hex()


def _build_set_typeconfig(type_byte: int, value_bytes: bytes) -> bytearray:
    """
    Build SET_MULTI_PARAM_CONFIG (0x4D) packet for one TypeConfig setting.

    Wire format — inferred from BaseMultiConfigCommand.java + response TLV structure:
      [0x4D, 0x00, 0x00, total_len, val_len+1, type_byte, *value_bytes]

    Each TLV entry = [val_len+1, type_byte, *value_bytes]
      val_len+1 = len(value_bytes) + 1 (type_byte counts as 1 payload byte)
    """
    entry_head = bytes([len(value_bytes) + 1, type_byte])
    payload = entry_head + value_bytes
    return _make_cmd(CMD_SET_MULTI_CONFIG, payload, size=len(payload) + 4)


def _build_get_multi_cfg(type_bytes: list[int]) -> bytearray:
    """
    Build GET_MULTI_PARAM_CONFIG (0x4E) TLV request.
    Dead on RCH04.05.03.41 — included for future firmware support.
    Packet: [0x4E, 0x00, 0x00, total_len, 0x00, 0xFF, [0x01, type_byte]...]
    """
    tlv = bytearray()
    for tb in type_bytes:
        tlv += bytes([0x01, tb])
    total_len = 1 + len(tlv)
    pkt = bytearray([CMD_GET_MULTI_CONFIG, 0x00, 0x00, total_len & 0xFF, 0x00, 0xFF])
    pkt += tlv
    return pkt


# Water.io daily goal lookup table.
# The device stores the goal as a 1-byte index (byte[14] of CapState, byte[4]
# of SyncInfo), NOT as mL.  Confirmed: index 7 → 2200 mL (user's actual goal).
# Table matches the goal presets in the official Water.io app.
_GOAL_INDEX_ML: tuple[int, ...] = (
    500, 750, 1000, 1250, 1500, 1750, 2000, 2200,   # indices 0-7
    2500, 2750, 3000, 3250, 3500, 3750, 4000,        # indices 8-14
)


# Bottle capacity lookup: bottle_volume_type byte → mL.
# From SDK BottleVolumeType.java: 0=500mL, 1=750mL.
_BOTTLE_VOLUME_ML: dict[int, int] = {0: 500, 1: 750}

def _goal_index_to_ml(index: int) -> int | None:
    """Map a device goal-index byte to mL, or None if index is out of range."""
    if 0 <= index < len(_GOAL_INDEX_ML):
        return _GOAL_INDEX_ML[index]
    return None


def _ml_to_goal_index(ml: int) -> int:
    """Map a goal in mL to the nearest device goal index."""
    best_idx = 0
    best_diff = abs(_GOAL_INDEX_ML[0] - ml)
    for i, g in enumerate(_GOAL_INDEX_ML):
        d = abs(g - ml)
        if d < best_diff:
            best_diff = d
            best_idx = i
    return best_idx


def _build_read_logs_cmd(offset: int, count: int = 1) -> bytearray:
    """READ_LOGS (0x0E) command.
    Payload = [offset_LE2, count_LE2] — matches SDK ReadLogsCommand.java and
    the test script build_read_logs(offset, count).
    Strategy B (one command per entry, count=1) is used in _read_logs_async
    because live capture shows exactly one notification per command.
    """
    payload = struct.pack("<HH", offset, count)
    return _make_cmd(CMD_READ_LOGS, payload, size=8)


def _parse_log_entry(raw8: bytes) -> dict:
    """
    Parse one 8-byte log entry payload from a READ_LOGS (0x0E) notification.

    Called with raw8 = data[4:12]  (the 8 payload bytes after the 4-byte header).
    Source: ReadLogsCommand.java m4145w(int i6, byte[] bArr) — entry loop at i6=4.

    Full notification frame (12 bytes total):
      data[0]    = 0x0E  (opcode echo)
      data[1..2] = 0x00, 0x00
      data[3]    = plen  (8 = valid entry; 0 = end-of-log sentinel)
      data[4..7] = mTimeStamp   LE uint32  Unix seconds  (SDK ×1000 for ms)
      data[8]    = opCode       ISO-8859-1 char  (see protocol reference block)
      data[9..10]= mMeasurement LE uint16
      data[11]   = mExtraData   uint8

    raw8 = data[4:12]:
      raw8[0..3] = mTimeStamp
      raw8[4]    = opCode
      raw8[5..6] = mMeasurement
                   'L','U','l' → mL remaining (firmware-converted uint16 LE).
                   'Ð' (0xD0) → post-refill mL remaining. Firmware-confirmed refill.
                   ALL carry mL — firmware does ADC→mL conversion internally.
      raw8[7]    = mExtraData  (uint8, bArr[i9] & 0xFF)
                   'Ð' (0xD0) → refill sub-type: 1=DAR, 2=DBR, 3=BOTH
                   'G'        → button: 1=short 2=long1 4=long2 8=double
                   'A'        → last log index
                   others     → 0 or event-specific

    CONFIRMED: The firmware does ADC→mL conversion internally.
    SDK m4147y()→m20709h() is just uint16 LE read — NO calibration in app.
    L, U, l, and 0xD0 ALL carry water level in mL directly.
    Level DOWN ≥30 = drink.  Level UP = refill.

    For 'Ð' (0xD0) refill events to appear in logs, DAR and/or DBR TypeConfig
    settings (type bytes 38 and 39) must be enabled via the HA switch entities.
    """
    ts    = struct.unpack_from("<I", raw8, 0)[0]           # timestamp bytes 0-3
    ev    = chr(raw8[4])                                     # opCode byte 4
    level = struct.unpack_from("<H", raw8, 5)[0] if len(raw8) >= 7 else 0  # level bytes 5-6
    extra = raw8[7] if len(raw8) >= 8 else 0               # extra byte 7
    return {"ts": ts, "type": ev, "level": level, "extra": extra,
            "raw": raw8.hex()}


def _build_reset_device_cmd() -> bytearray:
    """
    RESET_DEVICE_COMMAND (0x07): bare opcode + 4B LE timestamp.
    CONFIRMED on live device -> 0x58 push response.
    This is the v3.x format: [0x07, ts0, ts1, ts2, ts3, 0...]
    """
    ts = int(datetime.now().timestamp())
    raw = bytearray([CMD_RESET_DEVICE]) + struct.pack("<I", ts)
    raw += bytearray(19 - len(raw))   # pad to 20 bytes
    return raw


def _build_time_cmd() -> bytearray:
    """
    SET_REAL_TIME_COMMAND (0x11).
    Wire: [0x11, 0x00, 0x00, 0x04, ts_LE4]
    Payload is the Unix timestamp as little-endian uint32.
    SDK buildTimestampPayload() takes secBytes[7,6,5,4] from 8-byte BE long
    which equals LE uint32. Confirmed: sending BE caused device RTC to read
    year 2043 (byte-swapped). Fix: send LE directly.
    """
    ts = int(datetime.now().timestamp())
    payload = struct.pack("<I", ts)   # little-endian uint32
    return _make_cmd(CMD_SET_REAL_TIME, payload, size=8)


def _parse_notification(data: bytearray) -> dict[str, Any]:
    """
    Parse a BLE notification from the Water.io cap.

    All response frames share this header:
        byte[0] = response opcode (echoes the command opcode)
        byte[1] = 0x00
        byte[2] = 0x00
        byte[3] = payload_length  (bytes that follow)
        byte[4..] = payload

    Exception: GET_CAP_STATE (0x3C) response uses byte[3] as protocolVersion
    and the payload extends directly from byte[4] without a length prefix.

    Generic ACK (0x5A) is returned by all SET_* / action commands.
    Autonomous push (0x58) arrives on connect and after CMD_RESET_DEVICE.

    Key response opcodes (from EnumCommandDevice.java + CapStateResponse.java):
        0x58 RESP_STATUS_PUSH   – autonomous: [bat%, temp°C, ...]
        0x3C RESP_GET_CAP_STATE – 18+ bytes; full cap state (PRIMARY)
        0x72 RESP_GET_HYDRATIONS– [4..5]=manual ml, [6..7]=cap ml
        0x1B RESP_GET_BATTERY   – [4]=battery%
        0x1D RESP_GET_VERSION   – [4..] ASCII firmware string
        0x10 RESP_GET_REAL_TIME – [4..7] LE uint32 timestamp
        0x0D RESP_GET_LOG_LENGTH– [4..5] LE uint16 count
        0x5A RESP_ACK           – generic success ACK
    """
    if not data:
        return {}

    result: dict[str, Any] = {}
    opcode = data[0]

    try:
        if opcode == RESP_STATUS_PUSH:
            # 0x58: device state-change broadcast, emitted after SET_* commands
            # and on connect. Confirmed layout from live capture (protocolVersion=12):
            #   byte[4] = state_flags  (0x53='S' = stopped/idle, stays constant)
            #   byte[5] = event_counter (increments per BLE command processed)
            #   byte[6] = reserved / 0x00
            # Battery and temperature are NOT in this push for protocolVersion=12.
            # Battery comes from RESP_GET_BATTERY (0x1B) and RESP_GET_CAP_STATE (0x3C).
            pass

        elif opcode == RESP_GET_CAP_STATE:
            # 0x3C: full cap state snapshot (CapStateResponse.java)
            # byte[3]  = protocolVersion  (NOT payload length; e.g. 12 for our cap)
            # byte[4]  = isClosed
            # byte[5]  = isActiveMode   (bit flags: 0x01=active, 0x02=coach/need_drink)
            # byte[6]  = hydrationStatus  (tier: 0=none,1=low,2=ok,3=good)
            # byte[7]  = hydrationLevel   (= cumulative_cap_ml / daily_goal * 100)
            # byte[10] = isCharging (if protocolVersion==12)
            # byte[11] = isCharging (if protocolVersion!=12)
            # byte[13] = batteryLevel %
            # byte[14..15] = daily GOAL ml LE uint16 for pv<15 (CONFIRMED: equals
            #               base_goal from GET_SYNC_INFO — NOT water consumed!)
            # byte[17..18] = dailyHydrationMl LE uint16  (pv >= 15 only — actual ml)
            # byte[19..20] = dailyManualHydrationMl LE uint16  (pv >= 17 only)
            if len(data) < 14:
                LOGGER.warning("GET_CAP_STATE response too short: %d bytes", len(data))
            else:
                pv = data[3] & 0xFF
                result[FIELD_CAP_CLOSED]       = bool(data[4] == 1)
                # byte[5]: bit0 = active mode, bit1 = coach/need-to-drink
                if len(data) >= 6:
                    flags5 = data[5] & 0xFF
                    result[FIELD_IS_ACTIVE] = bool(flags5 & 0x01)
                    result[FIELD_NEED_DRINK] = bool(flags5 & 0x02)
                result[FIELD_HYDRATION_STATUS] = data[6] & 0xFF   # tier 0-3
                # byte[7] = device-computed hydration level % — unreliable after
                # counter resets.  We compute our own in _compute_daily_water,
                # so skip storing the device value to avoid overwriting ours.
                # if len(data) >= 8:
                #     result[FIELD_HYDRATION_LEVEL] = data[7] & 0xFF
                is_charging = (data[10] == 1) if pv == 12 else (data[11] == 1)
                result[FIELD_IS_CHARGING]      = bool(is_charging)
                # byte[13] = raw cell/hardware battery reading (same source as 0x1B)
                # This does NOT overwrite FIELD_BATTERY (charge %) which comes from
                # the standard GATT Battery Level characteristic (UUID 0x2A19).
                result[FIELD_BATTERY_CELL]     = data[13] & 0xFF
                # bytes[14..15] for pv==12 (V2 connection): dailyHydration
                # accumulated mL today (per CapStateResponse.java SDK).
                # Only update FIELD_CAP_ML if larger than what GET_HYDRATIONS
                # already stored (responses can arrive in any order).
                if pv == 12 and len(data) >= 16:
                    daily_hydration = struct.unpack_from("<H", data, 14)[0]
                    if 0 < daily_hydration < 0xFFFF:
                        if daily_hydration > (self._data.get(FIELD_CAP_ML, 0) or 0):
                            result[FIELD_CAP_ML] = daily_hydration
                # pv >= 15: byte[17..18] is actual today's consumed ml from device
                if pv >= 15 and len(data) >= 19:
                    measured = struct.unpack_from("<H", data, 17)[0]
                    if 0 < measured < 0xFFFF:
                        result[FIELD_WATER_ML] = measured
                # pv >= 17: byte[19..20] is today's manual ml from device
                if pv >= 17 and len(data) >= 21:
                    manual = struct.unpack_from("<H", data, 19)[0]
                    if 0 < manual < 0xFFFF:
                        result[FIELD_MANUAL_ML] = manual
                LOGGER.info(
                    "CapState pv=%d: goal=%smL  bat=%s%%  hydStatus=%s  closed=%s  "
                    "charging=%s  active=%s  need_drink=%s",
                    pv, result.get(FIELD_DAILY_GOAL_ML), result.get(FIELD_BATTERY),
                    result.get(FIELD_HYDRATION_STATUS), result.get(FIELD_CAP_CLOSED),
                    result.get(FIELD_IS_CHARGING), result.get(FIELD_IS_ACTIVE),
                    result.get(FIELD_NEED_DRINK),
                )

        elif opcode == RESP_GET_HYDRATIONS:
            # 0x72: CUMULATIVE hydration totals since last CLEAR_LOGS (not daily)
            # [4..5] = totalManuallyHydrations LE uint16  (ml, cumulative)
            # [6..7] = totalCapHydrations       LE uint16  (ml, cumulative)
            # Do NOT set FIELD_WATER_ML here — coordinator._compute_daily_water()
            # computes today's delta from FIELD_CAP_ML baseline.
            if len(data) >= 8:
                manual_ml = struct.unpack_from("<H", data, 4)[0]
                cap_ml    = struct.unpack_from("<H", data, 6)[0]
                result[FIELD_MANUAL_ML] = manual_ml if manual_ml < 0xFFFF else 0
                result[FIELD_CAP_ML]    = cap_ml    if cap_ml    < 0xFFFF else 0
                LOGGER.info("Hydrations (cumulative): manual=%dmL  cap=%dmL", manual_ml, cap_ml)

        elif opcode == RESP_GET_SYNC_INFO:
            # 0x78: GetSyncInfoCommand.java — plen=8 for pv=12, layout CONFIRMED:
            #   bytes[4]    = baseGoal INDEX (same index as CapState byte[14])
            #   bytes[5]    = unknown / echo byte
            #   bytes[6..7] = extraGoal (LE uint16, mL or index — usually 0)
            #   bytes[8..9] = manuallyHydrationMl (LE uint16, CUMULATIVE mL)
            #   bytes[10..11]= hydrationMeasurementMl (LE uint16, CUMULATIVE mL)
            # CONFIRMED from raw=78000008 073c 0000 0000 fa00:
            #   byte[4]=0x07(goal_idx=7→2200mL), byte[5]=0x3c(echo)
            #   bytes[8..9]=0=manual, bytes[10..11]=250=cap  ✓
            # Do NOT read bytes[4..5] as manual_ml — it's the goal index!
            # Do NOT set FIELD_WATER_ML here — delta computed in _compute_daily_water.
            if len(data) >= 6:
                # Device goal index — logged for diagnostics only.
                # Goal is managed entirely in HA, not overwritten from device.
                goal_idx = data[4]
                goal_ml = _goal_index_to_ml(goal_idx)
            if len(data) >= 12:
                manual_ml = struct.unpack_from("<H", data, 8)[0]
                cap_ml    = struct.unpack_from("<H", data, 10)[0]
                result[FIELD_MANUAL_ML] = manual_ml if manual_ml < 0xFFFF else 0
                result[FIELD_CAP_ML]    = cap_ml    if cap_ml    < 0xFFFF else 0
                LOGGER.info(
                    "SyncInfo: goal_idx=%d→%smL  manual=%dmL (cum)  cap=%dmL (cum)  [raw=%s]",
                    data[4] if len(data) >= 5 else -1,
                    result.get(FIELD_DAILY_GOAL_ML, '?'),
                    manual_ml, cap_ml, data.hex(),
                )

        elif opcode == RESP_GET_BATTERY:
            # 0x1B: [0..3] header, [4] = raw cell battery level (0-100)
            # This is the hardware cell reading, NOT the charge-circuit % that
            # the app shows.  Store only to FIELD_BATTERY_CELL; FIELD_BATTERY
            # is populated from the standard GATT Battery Level characteristic
            # (UUID 0x2A19) which matches the value the app displays.
            if len(data) >= 5:
                result[FIELD_BATTERY_CELL] = data[4] & 0xFF

        elif opcode == RESP_GET_EXTRA_GOAL:
            # 0x74: [4..5] = extraDailyGoalMl LE uint16
            if len(data) >= 6:
                extra = struct.unpack_from("<H", data, 4)[0]
                result[FIELD_EXTRA_GOAL_ML] = extra if extra < 0xFFFF else 0

        elif opcode == RESP_GET_SILENT_MODE:
            # 0x67: [4] = 0=off, 1=on
            if len(data) >= 5:
                result[FIELD_SILENT_MODE] = bool(data[4])

        elif opcode == RESP_GET_MAC:
            # 0x49: [4..9] = 6-byte BT MAC in order (AA:BB:CC:DD:EE:FF)
            if len(data) >= 10:
                mac_bytes = data[4:10]
                result[FIELD_MAC_ADDRESS] = ":".join(f"{b:02X}" for b in mac_bytes)

        elif opcode == RESP_GET_VERSION:
            # 0x1D: [3]=plen, [4..4+plen] = ASCII version string
            if len(data) >= 5:
                plen = data[3] & 0xFF
                raw_str = bytes(data[4:4 + plen]).rstrip(b"\x00")
                result[FIELD_FIRMWARE] = raw_str.decode("ascii", "replace")

        elif opcode == RESP_GET_REAL_TIME:
            # 0x10: [4..7] = LE uint32 timestamp
            if len(data) >= 8:
                ts = struct.unpack_from("<I", data, 4)[0]
                if ts > 0:
                    try:
                        from datetime import timezone as _tz
                        dt = datetime.fromtimestamp(ts, tz=_tz.utc)
                        result[FIELD_DEVICE_CLOCK] = dt.isoformat(timespec="seconds")
                    except Exception:
                        result[FIELD_DEVICE_CLOCK] = str(ts)

        elif opcode == RESP_GET_LOG_LENGTH:
            # 0x0D: [4..5] = LE uint16 log count
            if len(data) >= 6:
                count = struct.unpack_from("<H", data, 4)[0]
                result[FIELD_LOG_COUNT] = count

        elif opcode == RESP_ACK:
            pass  # generic success ACK from SET_* commands

        else:
            pass  # unhandled opcode

    except Exception as exc:
        LOGGER.warning(
            "Notification parse error  opcode=0x%02x  %s  raw=%s",
            opcode, exc, data.hex(),
        )

    return result


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
        # Settings dict: stores TypeConfig values persisted between polls.
        # Priority: DEFAULT_SETTINGS < config entry options < device-reported values.
        self._settings: dict[str, Any] = {**DEFAULT_SETTINGS}
        if entry.options:
            self._settings.update(entry.options)
        # Pre-populate _data so every settings entity has a value from first boot
        self._data.update(self._settings)

        # Sanity-guard: daily_goal_ml must be a plausible value (100–5000 mL).
        # Older firmware/parse bugs could write garbage (e.g. 15367) into config
        # entry options, which then poisons every subsequent startup.  Reset and
        # purge any out-of-range value immediately so the device-reported value
        # (from CapState bytes[14..15]) wins on the next poll.
        _goal = self._data.get(FIELD_DAILY_GOAL_ML)
        if not isinstance(_goal, (int, float)) or not (100 <= int(_goal) <= 5000):
            self._data[FIELD_DAILY_GOAL_ML]     = DEFAULT_SETTINGS[FIELD_DAILY_GOAL_ML]
            self._settings[FIELD_DAILY_GOAL_ML] = DEFAULT_SETTINGS[FIELD_DAILY_GOAL_ML]
            # Purge the corrupted value from stored options so it never comes back
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
        # ── Daily hydration tracking (persisted in config entry options) ───
        # Drink data from cap_ml deltas (GET_HYDRATIONS 0x72) and/or L/U log
        # entries (READ_LOGS 0x0E).  Refill detection matches original app:
        # water level going up = refill, going down = drink.
        opts = entry.options or {}
        self._daily_baseline_cap_ml: int    = int(opts.get("_baseline_cap_ml", 0))
        self._daily_baseline_manual: int    = int(opts.get("_baseline_manual", 0))
        self._daily_accumulated_ml: int     = int(opts.get("_accumulated_ml", 0))
        self._daily_accumulated_drinks: int = int(opts.get("_accumulated_drinks", 0))
        self._daily_last_drink_ml: int      = int(opts.get("_last_drink_ml", 0))
        self._daily_last_drink_ts: str      = str(opts.get("_last_drink_ts", ""))
        self._daily_water_remaining: int    = int(opts.get("_water_remaining", 0))
        self._prev_cap_ml: int              = int(opts.get("_prev_cap_ml", self._daily_baseline_cap_ml))
        saved_date_str: str | None          = opts.get("_baseline_date")
        try:
            from datetime import date as _date
            self._daily_today_date = _date.fromisoformat(saved_date_str) if saved_date_str else None
        except (ValueError, TypeError):
            self._daily_today_date = None
        # Initialize water_remaining to bottle capacity if not yet set
        # (first boot or migration from old version without remaining tracking)
        if self._daily_water_remaining <= 0 and self._daily_today_date is not None:
            _bv = int(self._settings.get(FIELD_BOTTLE_VOLUME, 0))
            self._daily_water_remaining = _BOTTLE_VOLUME_ML.get(_bv, 500)
        saved_date_str: str | None          = opts.get("_baseline_date")
        try:
            from datetime import date as _date
            self._daily_today_date = _date.fromisoformat(saved_date_str) if saved_date_str else None
        except (ValueError, TypeError):
            self._daily_today_date = None
        # Queue for READ_LOGS (0x0E) streaming responses
        self._log_queue: asyncio.Queue | None = None
        # Set to True when _read_logs_async successfully extracted a water level
        # from a real log entry – prevents _compute_daily_water from overwriting
        # it with the modular-arithmetic estimate.
        self._water_level_from_log: bool = False
        # Mutex to serialise all BLE connect/disconnect sessions.
        self._ble_lock: asyncio.Lock = asyncio.Lock()

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
                if ev in ("L", "U", "l", "N"):
                    level = struct.unpack_from("<H", raw, 9)[0]
                    if 0 < level < 0xFFFF:
                        self._daily_water_remaining = level
                        self._data[FIELD_WATER_REMAINING_ML] = level
                        LOGGER.info("Real-time measurement: type=%s  remaining=%dmL", ev, level)
                        self.async_set_updated_data(dict(self._data))
            return
        parsed = _parse_notification(raw)
        if parsed:
            # Don't let intermediate notifications overwrite computed fields
            # that _compute_daily_water manages (prevents 0% flicker).
            for key in (FIELD_HYDRATION_LEVEL, FIELD_WATER_ML,
                        FIELD_DRINK_COUNT_TODAY, FIELD_LAST_DRINK_ML,
                        FIELD_LAST_DRINK_TS, FIELD_WATER_REMAINING_ML,
                        FIELD_DAILY_GOAL_ML):
                parsed.pop(key, None)
            self._data.update(parsed)
            # Push update to HA without waiting for the next poll cycle
            self.async_set_updated_data(dict(self._data))

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
            ble_device.name if ble_device else "not in registry – using raw MAC",
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
        raw data appears in the HA logs – useful for further protocol study
        when testing against real hardware.
        """
        if not self._client:
            return

        self._notify_char = None
        self._write_char  = None

        notify_lc = [u.lower() for u in NOTIFY_CANDIDATES]
        write_lc  = [u.lower() for u in WRITE_CANDIDATES]

        # Dump the full GATT table at WARNING so it always appears in the log
        # without needing debug mode.  Lets us identify the real UUIDs on this
        # firmware revision and update NOTIFY_CANDIDATES / WRITE_CANDIDATES.
        for svc in self._client.services:
            LOGGER.warning("GATT svc  %s", svc.uuid)
            for ch in svc.characteristics:
                props = ch.properties
                ulc   = ch.uuid.lower()
                LOGGER.warning("  char  %s  props=%s", ch.uuid, list(props))

                if self._notify_char is None and "notify" in props and ulc in notify_lc:
                    self._notify_char = ch.uuid
                    LOGGER.warning("  >>> MATCHED notify: %s", ch.uuid)

                if self._write_char is None and (
                    "write" in props or "write-without-response" in props
                ) and ulc in write_lc:
                    self._write_char = ch.uuid
                    LOGGER.warning("  >>> MATCHED write:  %s", ch.uuid)

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
                "No known notify char found on %s – attempting subscribe on all "
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
          1. SET_REAL_TIME (0x11)  – sync RTC; BE timestamp
          2. GET_CAP_STATE (0x3C)  – full state: water, battery, status, flags
          3. GET_HYDRATIONS (0x72) – cumulative manual + cap-measured ml
          4. GET_SYNC_INFO (0x78)  – base goal, extra goal, running totals
          5. GET_BATTERY (0x1B)    – backup battery %
          6. GET_VERSION (0x1D)    – firmware version string
        """
        if not self._write_char:
            LOGGER.warning("No write char – cannot fetch protocol data")
            return

        # 1. SET_REAL_TIME – sync RTC (SDK step 1 of HydrationDefaultCapCmds)
        pkt_time = _build_time_cmd()
        try:
            await self._client.write_gatt_char(self._write_char, pkt_time, response=False)
            LOGGER.warning("SET_REAL_TIME sent: %s", pkt_time.hex())
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
                pkt_tz = _build_set_typeconfig(0, tz_bytes)
                try:
                    await self._client.write_gatt_char(self._write_char, pkt_tz, response=False)
                    self._data["timezone_offset_min"] = utc_offset_min
                    LOGGER.info("Synced timezone offset to device: %+d min", utc_offset_min)
                except BleakError as exc:
                    LOGGER.warning("Timezone sync failed: %s", exc)
                await asyncio.sleep(0.3)

        # 2. GET_CAP_STATE (0x3C) – full state snapshot (water + battery + flags)
        if await self._write_cmd_v4(CMD_GET_CAP_STATE):
            await asyncio.sleep(1.5)

        # 3. GET_HYDRATIONS (0x72) – cumulative manual + cap-measured ml
        if await self._write_cmd_v4(CMD_GET_HYDRATIONS):
            await asyncio.sleep(1.2)

        # 4. GET_SYNC_INFO (0x78) – base goal, extra goal, running totals
        if await self._write_cmd_v4(CMD_GET_SYNC_INFO):
            await asyncio.sleep(1.0)

        # 5. GET_BATTERY (0x1B) – battery level backup
        if await self._write_cmd_v4(CMD_GET_BATTERY):
            await asyncio.sleep(0.5)

        # 6. GET_VERSION (0x1D) – firmware version string
        if await self._write_cmd_v4(CMD_GET_VERSION):
            await asyncio.sleep(0.5)

        # 7. GET_REAL_TIME (0x10) – device RTC
        if await self._write_cmd_v4(CMD_GET_REAL_TIME):
            await asyncio.sleep(0.5)

        # 8. GET_EXTRA_DAILY_GOAL (0x74) – extra goal ml
        if await self._write_cmd_v4(CMD_GET_EXTRA_DAILY_GOAL):
            await asyncio.sleep(0.5)

        # 9. GET_SILENT_MODE (0x67) – reminder silence flag
        if await self._write_cmd_v4(CMD_GET_SILENT_MODE):
            await asyncio.sleep(0.5)

        # 10. GET_MAC_ADDRESS (0x49)
        if await self._write_cmd_v4(CMD_GET_MAC_ADDRESS):
            await asyncio.sleep(0.5)

        # 11. GET_LOG_LENGTH (0x0D) – total log entries on device
        if await self._write_cmd_v4(CMD_GET_LOG_LENGTH):
            await asyncio.sleep(0.5)

        # 12. READ_LOGS (0x0E) – read recent log entries for L/U/N water level.
        await self._read_logs_async()

        # 13. CLEAR_LOGS (0x0F) – clear device log after reading, same as the
        #     original app's HydrationDefaultCapCmds final step.  Keeps the buffer
        #     small so the next sync only sees new entries (no need to seek to tail).
        #     IMPORTANT: CLEAR_LOGS also resets the cap_ml counter from GET_HYDRATIONS
        #     back to 0.  _compute_daily_water detects the drop and carries the daily
        #     segment forward automatically.
        if await self._write_cmd_v4(CMD_CLEAR_LOGS):
            await asyncio.sleep(0.5)
            LOGGER.info("CLEAR_LOGS sent – device log buffer cleared")
        self._data[FIELD_LOG_COUNT] = 0

        # Note: CMD_GET_SINGLE_MEAS (0x01) was tested but pv=12 firmware
        # ACKs without producing a measurement notification.
        # Water remaining comes from L/U/l log entries (firmware-converted mL).
        # If no log entries are available, cap_ml delta heuristic is the fallback.

    async def _write_cmd_v4(self, opcode: int, payload: bytes = b"") -> bool:
        """Write a v4.8.6 format command: [opcode, 0x00, 0x00, len, payload...]

        Uses compact sizing — GET commands (empty payload) are sent as 4 bytes.
        The original app and waterio_fetch.py both use 4-byte packets for GET;
        the device firmware ignores trailing zeros but some firmware revisions
        stop responding to GET commands when they arrive padded to 8 bytes.
        """
        if not self._client or not self._client.is_connected:
            return False
        if not self._write_char:
            return False
        # Compact: exactly 4 + len(payload) bytes, no extra zero padding
        pkt = _make_cmd(opcode, payload, size=4 + len(payload))
        try:
            await self._client.write_gatt_char(self._write_char, pkt, response=False)
            return True
        except BleakError as exc:
            LOGGER.warning("Write failed 0x%02x: %s", opcode, exc)
            return False

    # ------------------------------------------------------------------
    # DataUpdateCoordinator interface
    # ------------------------------------------------------------------

    async def _read_logs_async(self) -> None:
        """Read ALL device log entries, process hydration events, then clear.

        SDK strategy (ReadLogsCommand.java m4141C / m4143E / mo3870o):
          1. Send batch request: [0x0E, 0x00, 0x00, 0x04, offset_LE2, end_LE2]
             where end = offset + BATCH_SIZE (SDK uses 50).
          2. Device responds with 1+ notifications, each containing up to 16
             eight-byte log entries.  data[3] = number of entries in THIS packet.
          3. If data[3] == 16 (full page), request next batch from current count.
             If data[3] < 16 or == 0, all entries have been read.
          4. Each 8-byte record: [ts_LE4, opCode, mMeasurement_LE2, extraData]

        CRITICAL FINDING (confirmed from SDK source):
          The firmware does ALL ADC→mL conversion INTERNALLY.  mMeasurement
          in L/U/l/0xD0 entries is ALREADY in mL — the app just reads uint16 LE.
          There is NO calibration table or polynomial in the app code.
          m4147y() → m20709h() = plain unsigned 16-bit little-endian read.

        Hydration processing (HydrationRepo.m3823q):
          - L/U/l/0xD0: mMeasurement = current water level in mL
          - level went DOWN ≥ 30 mL from prev → DRINK
          - level went UP from prev → REFILL (silent update in app)
          - 0xD0 with extraData 1/2/3 → explicit DAR/DBR/BOTH refill
        """
        if not self._write_char:
            return

        log_count = self._data.get(FIELD_LOG_COUNT, 0) or 0
        if log_count == 0:
            return

        _BATCH_SIZE = 50  # SDK default: request 50 entries at a time
        _MAX_ENTRIES = 2003  # SDK hard limit: f3009m.size() > 2002

        self._log_queue = asyncio.Queue()
        all_entries: list[dict] = []
        try:
            offset = 0
            while offset < log_count and len(all_entries) < _MAX_ENTRIES:
                end = min(offset + _BATCH_SIZE, log_count)
                pkt = _build_read_logs_cmd(offset, count=end - offset)
                try:
                    await self._client.write_gatt_char(
                        self._write_char, pkt, response=False
                    )
                except BleakError as exc:
                    LOGGER.warning("READ_LOGS batch @%d failed: %s", offset, exc)
                    break

                # Drain all notification packets for this batch.
                # Device sends 1+ packets, each with up to 16 entries (8 bytes each).
                # data[3] = entry count in THIS packet; 0 = no more data.
                batch_done = False
                while not batch_done:
                    try:
                        data = await asyncio.wait_for(
                            self._log_queue.get(), timeout=3.0
                        )
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
                            all_entries.append(
                                _parse_log_entry(bytes(data[start:start + 8]))
                            )

                    # data[3] < 16 means final packet for this batch
                    if entry_count < 16:
                        batch_done = True

                offset = len(all_entries)

            # Log what we found (diagnostic)
            from collections import Counter
            types = Counter(e["type"] for e in all_entries)
            LOGGER.info(
                "READ_LOGS: %d / %d entries read, types=%s",
                len(all_entries), log_count, dict(types),
            )

            # ── Process log entries ─────────────────────────────────────────
            #
            # Hydration-relevant opCodes (ReadLogsCommand switch → HydrationRepo):
            #
            #   'L' (0x4C) MEASUREMENT_ACCURATE  — mMeasurement = mL (firmware-converted)
            #   'U' (0x55) MEASUREMENT_ESTIMATED — mMeasurement = mL (firmware-converted)
            #   'l' (0x6C) VOLUME_AS_ML          — mMeasurement = mL
            #   'Ð' (0xD0) FIRMWARE_REFILL       — mMeasurement = post-refill mL
            #   'Ü' (0xDC) HYDRATION_V2          — bytes each ×5 for mL
            #
            # ALL of these carry mL values directly from the firmware.
            # The app (m4147y → m20709h) just reads uint16 LE — no conversion.
            #
            # Refill detection (HydrationRepo.m3823q):
            #   if new_level > prev_level → REFILL (water went up)
            #   if prev_level - new_level >= 30 → DRINK
            #   if 0xD0 event → explicit firmware refill with DAR/DBR sub-type

            _REFILL_OPCODE = chr(0xD0)  # "Ð" firmware refill event
            # All opCodes that carry water level in mL
            _LEVEL_OPCODES = {"L", "U", "l", _REFILL_OPCODE}

            bottle_cap = _BOTTLE_VOLUME_ML.get(
                self._data.get("bottle_volume_type", 0), 500
            )

            processable = [
                e for e in all_entries if e["type"] in _LEVEL_OPCODES
            ]

            LOGGER.info(
                "READ_LOGS: %d processable (L/U/l/0xD0) of %d total entries",
                len(processable), len(all_entries),
            )

            if processable:
                processable.sort(key=lambda e: e["ts"])
                today_start = int(dt_util.now().replace(
                    hour=0, minute=0, second=0, microsecond=0
                ).timestamp())
                # prev_level: seed from the persisted water_remaining so the
                # FIRST l entry in this batch can detect a drink or refill
                # versus the level we had at the end of the last sync.
                prev_level: int | None = (
                    self._daily_water_remaining
                    if self._daily_water_remaining > 0
                    else None
                )
                last_level: int | None = None

                # Sanity threshold: levels beyond 2× bottle capacity are raw
                # ADC values, NOT mL.  On pv=12 firmware, 'L'/'U' entries carry
                # raw ultrasonic readings (e.g. 11140, 16580) while 'l' carries
                # firmware-converted mL.  Skip raw-ADC entries so they don't
                # pollute prev_level and produce bogus drink deltas.
                _MAX_SANE_ML = bottle_cap * 2

                for entry in processable:
                    ev    = entry["type"]
                    level = entry["level"]
                    extra = entry["extra"]
                    if level <= 0 or level >= 0xFFFF:
                        continue
                    if level > _MAX_SANE_ML:
                        LOGGER.info(
                            "Skipping entry type=%s level=%d (> %d, likely raw ADC)  raw=%s",
                            ev, level, _MAX_SANE_ML, entry.get("raw", ""),
                        )
                        continue
                    is_today = entry["ts"] >= today_start
                    ts_iso = datetime.fromtimestamp(
                        entry["ts"], tz=timezone.utc
                    ).isoformat(timespec="seconds")

                    if ev == _REFILL_OPCODE:
                        # Firmware-confirmed refill — sub-type in mExtraData
                        _subtype_name = {1: "DAR", 2: "DBR", 3: "BOTH"}.get(extra, f"?{extra}")
                        LOGGER.info(
                            "Refill event 0xD0 (%s): post-refill=%dmL  ts=%s",
                            _subtype_name, level, ts_iso,
                        )
                        prev_level = level

                    else:
                        # L/U/l — all carry mL from firmware
                        if prev_level is not None:
                            delta = prev_level - level
                            if delta >= 30:
                                # Drink detected (≥30 mL threshold — HydrationRepo.m3820m)
                                if is_today:
                                    self._daily_accumulated_drinks += 1
                                    self._daily_last_drink_ml = delta
                                    try:
                                        self._daily_last_drink_ts = ts_iso
                                    except Exception:
                                        pass
                                LOGGER.info(
                                    "Drink: %dmL → %dmL  consumed=%dmL  type=%s  today=%s  ts=%s",
                                    prev_level, level, delta, ev, is_today, ts_iso,
                                )
                            elif delta < 0:
                                # Level went UP → refill (same as HydrationRepo:
                                # "refill to <level>" — silent update, no DB entry)
                                LOGGER.info(
                                    "Refill detected: %dmL → %dmL  type=%s  ts=%s",
                                    prev_level, level, ev, ts_iso,
                                )
                        prev_level = level

                    last_level = level

                if last_level is not None and 0 < last_level <= bottle_cap * 4:
                    self._daily_water_remaining = last_level
                    self._data[FIELD_WATER_REMAINING_ML] = last_level
                    self._water_level_from_log = True
                    LOGGER.info(
                        "Water remaining (from log): %dmL  (last L/U/l/0xD0 entry)",
                        last_level,
                    )

            # ── 0xDC (Ü) HydrationV2 events (FALLBACK only) ───────────────────
            # On pv=12 firmware, both 'l' entries AND 'Ü' entries appear for
            # the same sip.  'l' deltas already count drinks above, so we only
            # use 'Ü' when NO 'l' entries were processed (to avoid double-count).
            # HydrationRepo.m3822p(ts, lo*5, hi*5, extra*5, "hydrationV2", mac)
            if not self._water_level_from_log:
                hv2 = [e for e in all_entries if e["type"] == chr(0xDC)]
                for entry in hv2:
                    drink_ml = entry["extra"] * 5
                    if drink_ml >= 30:
                        ts_iso = datetime.fromtimestamp(
                            entry["ts"], tz=timezone.utc
                        ).isoformat(timespec="seconds")
                        today_start = int(dt_util.now().replace(
                            hour=0, minute=0, second=0, microsecond=0
                        ).timestamp())
                        if entry["ts"] >= today_start:
                            self._daily_accumulated_drinks += 1
                            self._daily_last_drink_ml = drink_ml
                            self._daily_last_drink_ts = ts_iso
                        LOGGER.info(
                            "HydrationV2 (fallback): drink=%dmL  ts=%s", drink_ml, ts_iso,
                        )
        finally:
            self._log_queue = None

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch all data from the cap (called by the coordinator scheduler).

        Connect → read everything → disconnect.  We never hold the connection
        open between polls — that would drain the bottle's battery.
        """
        async with self._ble_lock:
            if not await self._ensure_connected():
                raise UpdateFailed(f"Cannot connect to Water.io cap {self._mac}")

            try:
                await self._read_standard_gatt()      # battery, firmware, manufacturer …
                await self._fetch_protocol_data()     # proprietary hydration + cap-state
                self._compute_daily_water()           # cap-delta fallback (no-log path only)

                self._data[FIELD_LAST_SYNC] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                # Strip private accumulator fields (prefixed with '_') from the
                # dict returned to the coordinator — they're internal state only.
                public_data = {k: v for k, v in self._data.items() if not k.startswith("_")}
                LOGGER.info("Water.io update complete  device=%s  data=%s", self._name, public_data)
                return public_data
            finally:
                # Always disconnect after the poll cycle — do NOT hold the BLE link
                # open between polls.  Saves bottle battery and avoids Android app
                # being locked out of its own device.
                await self.disconnect()

    def _compute_daily_water(self) -> None:
        """Single source of truth for ALL daily hydration entities.

        Refill detection algorithm (matching original Water.io app logic):

          Original app (HydrationRepo / MeasurementEntity):
            if current_level > last_level → REFILL (water went UP)
            if current_level < last_level → DRINK  (water went DOWN, min 30 mL)
            After refill → assume bottle is full (bottle_capacity).

          On pv=12 firmware (RCH04) L/U log entries may be absent.  In that
          case we detect refills from cap_ml (cumulative consumption) deltas:
            if delta > current_remaining → user consumed more water than
            was left → at least one refill must have happened.

          For refills with NO subsequent drink (cap_ml unchanged), a
          "Refill Bottle" button is available for the user to signal the
          refill manually.

        Data flow:
          GET_HYDRATIONS (0x72) → cap_ml (cumulative since last CLEAR_LOGS)
          READ_LOGS (0x0E)     → L/U/l/Ð measurements (actual water level)
          cap_ml deltas        → drink detection + refill detection fallback

        Entities driven:
          FIELD_WATER_ML           – total mL consumed today
          FIELD_DRINK_COUNT_TODAY  – number of drinks today
          FIELD_LAST_DRINK_ML      – mL consumed in last drink
          FIELD_LAST_DRINK_TS      – ISO timestamp of last drink
          FIELD_WATER_REMAINING_ML – water remaining in bottle
        """
        cap_ml    = self._data.get(FIELD_CAP_ML,    0) or 0
        manual_ml = self._data.get(FIELD_MANUAL_ML, 0) or 0
        today     = dt_util.now().date()

        bottle_cap = _BOTTLE_VOLUME_ML.get(
            self._data.get("bottle_volume_type", 0), 500
        )

        # ── New calendar day ────────────────────────────────────────────────
        if self._daily_today_date != today:
            LOGGER.info(
                "New day %s — resetting daily water tracking  cap_ml=%d",
                today, cap_ml,
            )
            self._daily_today_date          = today
            self._daily_accumulated_ml      = 0
            self._daily_accumulated_drinks  = 0
            self._daily_baseline_cap_ml     = cap_ml
            self._daily_baseline_manual     = manual_ml
            self._daily_last_drink_ml       = 0
            self._daily_last_drink_ts       = ""
            # Preserve water_remaining — bottle still has water from yesterday
            if self._daily_water_remaining <= 0:
                self._daily_water_remaining = bottle_cap
            self._prev_cap_ml               = cap_ml
            self._data[FIELD_WATER_ML]          = 0
            self._data[FIELD_DRINK_COUNT_TODAY]  = 0
            self._data[FIELD_LAST_DRINK_ML]      = 0
            self._data[FIELD_LAST_DRINK_TS]      = None
            self._persist_baseline()
            return

        # ── CLEAR_LOGS detection: cap_ml dropped vs previous poll ───────────
        if cap_ml < self._prev_cap_ml - 20:
            segment_ml = max(0, self._prev_cap_ml - self._daily_baseline_cap_ml)
            self._daily_accumulated_ml  += segment_ml
            self._daily_baseline_cap_ml  = cap_ml
            self._daily_baseline_manual  = manual_ml
            LOGGER.info(
                "Counter reset detected: +%dmL segment → accumulated=%dmL",
                segment_ml, self._daily_accumulated_ml,
            )
            self._persist_baseline()

        # ── Drink / refill detection ─────────────────────────────────────────
        # When the log gave us real L/U/Ð entries: _read_logs_async already
        # processed every drink and refill event in order, updated
        # _daily_accumulated_drinks / _daily_last_drink_* / _daily_water_remaining,
        # and set _water_level_from_log = True.  We trust that and skip the
        # cap_ml-delta heuristic to avoid double-counting.
        #
        # When no log entries were available fall back to cap_ml deltas:
        delta_since_last = cap_ml - self._prev_cap_ml
        if not self._water_level_from_log and delta_since_last > 5:
            remaining_before = self._daily_water_remaining

            if delta_since_last > remaining_before + 10:
                # Consumed more than what was in the bottle → refill(s) happened.
                available = remaining_before
                n_refills = 0
                while available < delta_since_last:
                    available += bottle_cap
                    n_refills += 1
                new_remaining = available - delta_since_last
                LOGGER.info(
                    "Refill detected (cap_ml): consumed %dmL > remaining %dmL → "
                    "%d refill(s)  new_remaining=%dmL",
                    delta_since_last, remaining_before, n_refills, new_remaining,
                )
                self._daily_water_remaining = max(0, new_remaining)
            else:
                self._daily_water_remaining = max(0, remaining_before - delta_since_last)

            self._daily_accumulated_drinks += 1
            self._daily_last_drink_ml = delta_since_last
            self._daily_last_drink_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
            LOGGER.info(
                "New drink detected (cap_ml): +%dmL (cap %d→%d)  drink #%d today  remaining=%dmL",
                delta_since_last, self._prev_cap_ml, cap_ml,
                self._daily_accumulated_drinks, self._daily_water_remaining,
            )

        self._prev_cap_ml = cap_ml
        self._water_level_from_log = False  # reset for next poll

        # ── Compute totals ──────────────────────────────────────────────────
        delta_cap    = max(0, cap_ml    - self._daily_baseline_cap_ml)
        delta_manual = max(0, manual_ml - self._daily_baseline_manual)
        water_ml     = self._daily_accumulated_ml + delta_cap + delta_manual

        # ── Write all entities ──────────────────────────────────────────────
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

        # Persist after every poll so HA restart recovers everything
        self._persist_baseline()

    def _persist_baseline(self) -> None:
        """Save the current daily baseline into config entry options so it
        survives an HA restart within the same calendar day."""
        try:
            new_options = dict(self.config_entry.options)
            new_options["_baseline_cap_ml"]     = self._daily_baseline_cap_ml
            new_options["_baseline_manual"]     = self._daily_baseline_manual
            new_options["_accumulated_ml"]      = self._daily_accumulated_ml
            new_options["_accumulated_drinks"]  = self._daily_accumulated_drinks
            new_options["_last_drink_ml"]       = self._daily_last_drink_ml
            new_options["_last_drink_ts"]       = self._daily_last_drink_ts
            new_options["_water_remaining"]     = self._daily_water_remaining
            new_options["_prev_cap_ml"]         = self._prev_cap_ml
            new_options["_baseline_date"]       = self._daily_today_date.isoformat() if self._daily_today_date else ""
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

    async def async_set_typeconfig(self, type_byte: int, value: object) -> None:
        """
        Write a single TypeConfig setting via SET_MULTI_PARAM_CONFIG (0x4D).

        Encodes `value` according to TYPECONFIG_FIELDS[type_byte][1] and sends
        the TLV packet to the device. Also persists the new value in config
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
            idx = _ml_to_goal_index(int(value))
            actual_ml = _GOAL_INDEX_ML[idx]
            wire_value = idx
            LOGGER.info(
                "Goal: user=%dmL → index=%d → actual=%dmL",
                int(value), idx, actual_ml,
            )
        else:
            wire_value = value
            actual_ml = None

        async with self._ble_lock:
            if not await self._ensure_connected():
                from homeassistant.exceptions import HomeAssistantError
                raise HomeAssistantError(f"Water.io {self._mac} not connected")

            value_bytes = _typeconfig_encode(fmt, wire_value)
            pkt = _build_set_typeconfig(type_byte, value_bytes)

            try:
                await self._client.write_gatt_char(self._write_char, pkt, response=False)
                LOGGER.info(
                    "TypeConfig SET type=0x%02x (%s) value=%s raw=%s",
                    type_byte, field_key, value, pkt.hex(),
                )
            except Exception as exc:
                LOGGER.error("TypeConfig SET failed: %s", exc)
                raise
            finally:
                await self.disconnect()

        # Optimistic update
        decoded = _typeconfig_decode(fmt, value_bytes)
        # For daily goal, persist the actual mL (not the index)
        if actual_ml is not None:
            decoded = actual_ml
        self._settings[field_key] = decoded
        self._data[field_key] = decoded
        self.async_set_updated_data(dict(self._data))

        # Persist to config entry options
        new_options = dict(self.config_entry.options)
        new_options[field_key] = decoded
        self.hass.config_entries.async_update_entry(self.config_entry, options=new_options)

    async def async_set_typeconfig_by_key(self, field_key: str, value: object) -> None:
        """Convenience wrapper: look up type_byte from field key and call async_set_typeconfig."""
        if field_key not in FIELD_TO_TYPEBYTE:
            raise ValueError(f"No TypeConfig mapping for field: {field_key}")
        await self.async_set_typeconfig(FIELD_TO_TYPEBYTE[field_key], value)

    async def async_set_silent_mode(self, enabled: bool) -> None:
        """Set silent mode (reminder silence) via dedicated 0x66 command."""
        async with self._ble_lock:
            if not await self._ensure_connected():
                from homeassistant.exceptions import HomeAssistantError
                raise HomeAssistantError(f"Water.io {self._mac} not connected")
            try:
                pkt = _make_cmd(CMD_SET_SILENT_MODE, bytes([1 if enabled else 0]), size=5)
                await self._client.write_gatt_char(self._write_char, pkt, response=False)
            finally:
                await self.disconnect()
        LOGGER.info("Silent mode SET: %s", enabled)
        self._settings[FIELD_SILENT_MODE] = enabled
        self._data[FIELD_SILENT_MODE] = enabled
        self.async_set_updated_data(dict(self._data))
        new_options = dict(self.config_entry.options)
        new_options[FIELD_SILENT_MODE] = enabled
        self.hass.config_entries.async_update_entry(self.config_entry, options=new_options)

    async def async_find_bottle(self) -> None:
        """Flash the bottle LED to locate it (GET_SINGLE_MEAS triggers a brief LED cycle)."""
        async with self._ble_lock:
            if not await self._ensure_connected():
                LOGGER.warning("Find bottle: cannot connect to %s", self._mac)
                return
            try:
                await self._write_cmd_v4(CMD_GET_SINGLE_MEAS)
            finally:
                await self.disconnect()
        LOGGER.info("Find bottle command sent to %s", self._mac)

    async def async_set_extra_goal(self, ml: int) -> None:
        """Set extra daily hydration goal via dedicated 0x73 command."""
        async with self._ble_lock:
            if not await self._ensure_connected():
                from homeassistant.exceptions import HomeAssistantError
                raise HomeAssistantError(f"Water.io {self._mac} not connected")
            try:
                pkt = _make_cmd(CMD_SET_EXTRA_DAILY_GOAL, struct.pack("<H", ml), size=6)
                await self._client.write_gatt_char(self._write_char, pkt, response=False)
            finally:
                await self.disconnect()
        LOGGER.info("Extra daily goal SET: %dmL", ml)
        self._data[FIELD_EXTRA_GOAL_ML] = ml
        self.async_set_updated_data(dict(self._data))
        new_options = dict(self.config_entry.options)
        new_options[FIELD_EXTRA_GOAL_ML] = ml
        self.hass.config_entries.async_update_entry(self.config_entry, options=new_options)


# Backward-compatibility alias (old code references WaterioInstance)
WaterioInstance = WaterioCoordinator
