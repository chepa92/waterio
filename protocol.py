"""Water.io BLE protocol helpers — pure functions and data tables.

All symbols here are wire-protocol utilities with no Home Assistant
dependencies (only stdlib + local const.py imports).  See PROTOCOL.md for
the full BLE protocol reference (packet framing, log entry opcode table,
TypeConfig registry, CapState layout, refill detection notes).
"""
from __future__ import annotations

import struct
from datetime import datetime, timezone
from typing import Any

from .const import (
    LOGGER,
    # Commands
    CMD_RESET_DEVICE, CMD_SET_REAL_TIME, CMD_READ_LOGS,
    CMD_SET_MULTI_CONFIG, CMD_GET_MULTI_CONFIG,
    # Response opcodes
    RESP_STATUS_PUSH, RESP_ACK,
    RESP_GET_CAP_STATE, RESP_GET_HYDRATIONS, RESP_GET_SYNC_INFO,
    RESP_GET_BATTERY, RESP_GET_VERSION, RESP_GET_REAL_TIME,
    RESP_GET_LOG_LENGTH, RESP_GET_EXTRA_GOAL,
    RESP_GET_SILENT_MODE, RESP_GET_MAC,
    # Field keys used in parse_notification
    FIELD_BATTERY, FIELD_BATTERY_CELL,
    FIELD_FIRMWARE, FIELD_HARDWARE, FIELD_MANUFACTURER, FIELD_SERIAL,
    FIELD_DAILY_GOAL_ML, FIELD_EXTRA_GOAL_ML,
    FIELD_CAP_CLOSED, FIELD_IS_ACTIVE, FIELD_NEED_DRINK,
    FIELD_IS_CHARGING, FIELD_HYDRATION_STATUS,
    FIELD_CAP_ML, FIELD_MANUAL_ML, FIELD_WATER_ML,
    FIELD_SILENT_MODE, FIELD_MAC_ADDRESS,
    FIELD_DEVICE_CLOCK, FIELD_LOG_COUNT, FIELD_LAST_SYNC,
)


# ---------------------------------------------------------------------------
# Data tables
# ---------------------------------------------------------------------------

# Water.io daily goal lookup table.
# The device stores the goal as a 1-byte index (byte[14] of CapState, byte[4]
# of SyncInfo), NOT as mL.  index 7 → 2200 mL (confirmed from live device).
# Table matches the goal presets in the official Water.io app.
GOAL_INDEX_ML: tuple[int, ...] = (
    500, 750, 1000, 1250, 1500, 1750, 2000, 2200,   # indices 0-7
    2500, 2750, 3000, 3250, 3500, 3750, 4000,        # indices 8-14
)

# Bottle capacity lookup: bottle_volume_type byte → mL.
# From SDK BottleVolumeType.java: 0=500mL, 1=750mL.
BOTTLE_VOLUME_ML: dict[int, int] = {0: 500, 1: 750}

# BLE-read-only fields persisted under "_snap_<field>" in config entry options
# so sensors show last-known values instead of "Unknown" after an HA restart.
SNAP_FIELDS: tuple[str, ...] = (
    FIELD_BATTERY, FIELD_FIRMWARE, FIELD_HARDWARE,
    FIELD_MANUFACTURER, FIELD_SERIAL,
    FIELD_CAP_CLOSED, FIELD_IS_ACTIVE, FIELD_NEED_DRINK,
    FIELD_IS_CHARGING, FIELD_HYDRATION_STATUS, FIELD_BATTERY_CELL,
    FIELD_SILENT_MODE, FIELD_MAC_ADDRESS, FIELD_LAST_SYNC,
)

# Human-readable names for every log entry opCode (ReadLogsCommand.java m4145w switch)
OPCODE_NAMES: dict[str, str] = {
    'A': 'CONNECTED',            'B': 'CALIBRATED',           'C': 'CLOSED_CAP',
    'D': 'DISCONNECTED',         'E': 'MEASURE_NOT_VALID',    'F': 'TEMPERATURE',
    'G': 'BUTTON_PRESSED',       'H': 'HYDRATION_CHECK',
    'L': 'MEASUREMENT_ACCURATE', 'M': 'MEASURE_EVENT',        'N': 'LAST_VOLUME_EVENT',
    'O': 'OPENED_CAP',           'P': 'PAIR',                 'Q': 'REMINDER_SEQUENCE',
    'R': 'REMINDER',             'S': 'START_SHAKE',          'T': 'EXIT_HIGH_TEMP',
    'U': 'MEASUREMENT_ESTIMATED','V': 'VOLTAGE',              'X': 'SCHEDULER_EVENT',
    'Y': 'LIQUID_TYPE',          'Z': 'RESET_DEVICE',         '^': 'SIGNAL_RATE',
    '_': 'SIGNAL_RATE_MIN',      'c': 'TOP_CLOSE',            'g': 'STOP_TILT',
    'h': 'SLEEP_DEVICE',         'l': 'VOLUME_AS_ML',         'o': 'TOP_OPEN',
    'r': 'PRE_REMINDER',         's': 'STOP_SHAKE',           't': 'ENTER_HIGH_TEMP',
    '\xc8': 'MEASUREMENT_COUNTERS',    '\xc9': 'UPDATE_MANUAL_HYDRATION',
    '\xca': 'UPDATE_MEAS_HYDRATION',   '\xcb': 'UPDATE_EXTRA_GOAL',
    '\xd0': 'FIRMWARE_REFILL',         '\xd1': 'SEQUENCE_PATTERN',
    '\xd3': 'HYDRATION_STATE',         '\xd5': 'BOTTLE_NOT_STABLE',
    '\xd6': 'RE_MEASURE',              '\xd7': 'EXIT_LOW_POWER',
    '\xd8': 'EXIT_VERY_LOW_POWER',     '\xd9': 'CAP_OVERFLOW_MEMORY',
    '\xda': 'LED_STATUS_COUNTER',      '\xdb': 'BACKUP_HEAP',
    '\xdc': 'HYDRATION_V2',
}


# ---------------------------------------------------------------------------
# Goal-index helpers
# ---------------------------------------------------------------------------

def goal_index_to_ml(index: int) -> int | None:
    """Map a device goal-index byte to mL, or None if index is out of range."""
    if 0 <= index < len(GOAL_INDEX_ML):
        return GOAL_INDEX_ML[index]
    return None


def ml_to_goal_index(ml: int) -> int:
    """Map a goal in mL to the nearest device goal index."""
    best_idx = 0
    best_diff = abs(GOAL_INDEX_ML[0] - ml)
    for i, g in enumerate(GOAL_INDEX_ML):
        d = abs(g - ml)
        if d < best_diff:
            best_diff = d
            best_idx = i
    return best_idx


# ---------------------------------------------------------------------------
# Packet builders
# ---------------------------------------------------------------------------

def make_cmd(opcode: int, payload: bytes = b"", size: int = 8) -> bytearray:
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


def build_time_cmd() -> bytearray:
    """
    SET_REAL_TIME_COMMAND (0x11).
    Wire: [0x11, 0x00, 0x00, 0x04, ts_LE4]
    Payload is the Unix timestamp as little-endian uint32.
    SDK buildTimestampPayload() takes secBytes[7,6,5,4] from 8-byte BE long
    which equals LE uint32.  Sending BE caused device RTC to read year 2043
    (byte-swapped); fix: send LE directly.
    """
    ts = int(datetime.now().timestamp())
    payload = struct.pack("<I", ts)
    return make_cmd(CMD_SET_REAL_TIME, payload, size=8)


def build_reset_device_cmd() -> bytearray:
    """
    RESET_DEVICE_COMMAND (0x07): bare opcode + 4B LE timestamp.
    CONFIRMED on live device -> 0x58 push response.
    This is the v3.x format: [0x07, ts0, ts1, ts2, ts3, 0...]
    """
    ts = int(datetime.now().timestamp())
    raw = bytearray([CMD_RESET_DEVICE]) + struct.pack("<I", ts)
    raw += bytearray(19 - len(raw))   # pad to 20 bytes
    return raw


def build_read_logs_cmd(offset: int, count: int = 1) -> bytearray:
    """READ_LOGS (0x0E) command.
    Payload = [offset_LE2, count_LE2] — matches SDK ReadLogsCommand.java and
    the test script build_read_logs(offset, count).
    Strategy B (one command per entry, count=1) is used in _fetch_log_entries
    because live capture shows exactly one notification per command.
    """
    payload = struct.pack("<HH", offset, count)
    return make_cmd(CMD_READ_LOGS, payload, size=8)


# ---------------------------------------------------------------------------
# TypeConfig helpers  (SET 0x4D / GET 0x4E)
# ---------------------------------------------------------------------------

def typeconfig_encode(fmt: str, value: object) -> bytes:
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


def typeconfig_decode(fmt: str, value_bytes: bytes) -> object:
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


def build_set_typeconfig(type_byte: int, value_bytes: bytes) -> bytearray:
    """
    Build SET_MULTI_PARAM_CONFIG (0x4D) packet for one TypeConfig setting.

    Wire format — inferred from BaseMultiConfigCommand.java + response TLV structure:
      [0x4D, 0x00, 0x00, total_len, val_len+1, type_byte, *value_bytes]

    Each TLV entry = [val_len+1, type_byte, *value_bytes]
      val_len+1 = len(value_bytes) + 1 (type_byte counts as 1 payload byte)
    """
    entry_head = bytes([len(value_bytes) + 1, type_byte])
    payload = entry_head + value_bytes
    return make_cmd(CMD_SET_MULTI_CONFIG, payload, size=len(payload) + 4)


def build_get_multi_cfg(type_bytes: list[int]) -> bytearray:
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


# ---------------------------------------------------------------------------
# Log entry parser
# ---------------------------------------------------------------------------

def parse_log_entry(raw8: bytes) -> dict:
    """
    Parse one 8-byte log entry payload from a READ_LOGS (0x0E) notification.

    Called with raw8 = data[4:12]  (the 8 payload bytes after the 4-byte header).
    Source: ReadLogsCommand.java m4145w(int i6, byte[] bArr) — entry loop at i6=4.

    Full notification frame (12 bytes total):
      data[0]    = 0x0E  (opcode echo)
      data[1..2] = 0x00, 0x00
      data[3]    = plen  (8 = valid entry; 0 = end-of-log sentinel)
      data[4..7] = mTimeStamp   LE uint32  Unix seconds  (SDK ×1000 for ms)
      data[8]    = opCode       ISO-8859-1 char
      data[9..10]= mMeasurement LE uint16  (mL remaining for L/U/l/0xD0 entries)
      data[11]   = mExtraData   uint8

    raw8 = data[4:12]:
      raw8[0..3] = mTimeStamp
      raw8[4]    = opCode
      raw8[5..6] = mMeasurement
      raw8[7]    = mExtraData
    """
    ts    = struct.unpack_from("<I", raw8, 0)[0]
    ev    = chr(raw8[4])
    level = struct.unpack_from("<H", raw8, 5)[0] if len(raw8) >= 7 else 0
    extra = raw8[7] if len(raw8) >= 8 else 0
    return {"ts": ts, "type": ev, "level": level, "extra": extra,
            "raw": raw8.hex()}


# ---------------------------------------------------------------------------
# Notification parser
# ---------------------------------------------------------------------------

def parse_notification(data: bytearray) -> dict[str, Any]:
    """
    Parse a BLE notification from the Water.io cap.

    All response frames share this header:
        byte[0] = response opcode (echoes the command opcode)
        byte[1] = 0x00
        byte[2] = 0x00
        byte[3] = payload_length  (bytes that follow)
        byte[4..] = payload

    Exception: GET_CAP_STATE (0x3C) response uses byte[3] as protocolVersion.
    Generic ACK (0x5A) is returned by all SET_* / action commands.
    Autonomous push (0x58) arrives on connect and after CMD_RESET_DEVICE.
    """
    if not data:
        return {}

    result: dict[str, Any] = {}
    opcode = data[0]

    try:
        if opcode == RESP_STATUS_PUSH:
            # 0x58: device state-change broadcast, emitted after SET_* commands
            # and on connect.  Battery/temperature not present for pv=12.
            pass

        elif opcode == RESP_GET_CAP_STATE:
            # 0x3C: full cap state snapshot (CapStateResponse.java)
            # byte[3]  = protocolVersion  (NOT payload length; e.g. 12 for our cap)
            # byte[4]  = isClosed
            # byte[5]  = isActiveMode   (bit flags: 0x01=active, 0x02=coach/need_drink)
            # byte[6]  = hydrationStatus  (tier: 0=none,1=low,2=ok,3=good)
            # byte[10] = isCharging (if protocolVersion==12)
            # byte[11] = isCharging (if protocolVersion!=12)
            # byte[13] = batteryLevel %
            # byte[14..15] = daily GOAL ml LE uint16 for pv<15
            # byte[17..18] = dailyHydrationMl LE uint16  (pv >= 15 only)
            # byte[19..20] = dailyManualHydrationMl LE uint16  (pv >= 17 only)
            if len(data) < 14:
                LOGGER.warning("GET_CAP_STATE response too short: %d bytes", len(data))
            else:
                pv = data[3] & 0xFF
                result[FIELD_CAP_CLOSED]       = bool(data[4] == 1)
                if len(data) >= 6:
                    flags5 = data[5] & 0xFF
                    result[FIELD_IS_ACTIVE]  = bool(flags5 & 0x01)
                    result[FIELD_NEED_DRINK] = bool(flags5 & 0x02)
                result[FIELD_HYDRATION_STATUS] = data[6] & 0xFF
                # byte[7] = device-computed hydration level % — unreliable after
                # counter resets.  We compute our own in _compute_daily_water.
                is_charging = (data[10] == 1) if pv == 12 else (data[11] == 1)
                result[FIELD_IS_CHARGING]      = bool(is_charging)
                result[FIELD_BATTERY_CELL]     = data[13] & 0xFF
                if pv == 12 and len(data) >= 16:
                    # bytes[14..15] on pv<15 is the daily GOAL index/ml, NOT
                    # daily hydration.  Do NOT assign to FIELD_CAP_ML — that
                    # contaminates the delta-tracking with the goal value and
                    # causes water_ml inflation.  GET_HYDRATIONS / GET_SYNC_INFO
                    # provide the real consumption counters.
                    _capstate_val = struct.unpack_from("<H", data, 14)[0]
                    LOGGER.debug(
                        "CapState pv=12 bytes[14..15]=%d (daily goal, NOT cap_ml)",
                        _capstate_val,
                    )
                if pv >= 15 and len(data) >= 19:
                    measured = struct.unpack_from("<H", data, 17)[0]
                    if 0 < measured < 0xFFFF:
                        result[FIELD_WATER_ML] = measured
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
            # 0x72: cumulative totals since last CLEAR_LOGS.
            # [4..5] = totalManuallyHydrations LE uint16  (ml, cumulative)
            # [6..7] = totalCapHydrations       LE uint16  (ml, cumulative)
            if len(data) >= 8:
                manual_ml = struct.unpack_from("<H", data, 4)[0]
                cap_ml    = struct.unpack_from("<H", data, 6)[0]
                result[FIELD_MANUAL_ML] = manual_ml if manual_ml < 0xFFFF else 0
                result[FIELD_CAP_ML]    = cap_ml    if cap_ml    < 0xFFFF else 0
                LOGGER.info("Hydrations (cumulative): manual=%dmL  cap=%dmL", manual_ml, cap_ml)

        elif opcode == RESP_GET_SYNC_INFO:
            # 0x78: GetSyncInfoCommand.java — plen=8 for pv=12, layout CONFIRMED:
            #   bytes[4]     = baseGoal INDEX (same index as CapState byte[14])
            #   bytes[8..9]  = manuallyHydrationMl (LE uint16, CUMULATIVE mL)
            #   bytes[10..11]= hydrationMeasurementMl (LE uint16, CUMULATIVE mL)
            # Do NOT read bytes[4..5] as manual_ml — it's the goal index!
            if len(data) >= 6:
                goal_idx = data[4]
                goal_ml  = goal_index_to_ml(goal_idx)  # for log only
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
            # 0x1B: [4] = raw cell battery level (0-100).
            # Stored to FIELD_BATTERY_CELL only; FIELD_BATTERY comes from
            # the standard GATT Battery Level characteristic (UUID 0x2A19).
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
                        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
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

    except Exception as exc:
        LOGGER.warning(
            "Notification parse error  opcode=0x%02x  %s  raw=%s",
            opcode, exc, data.hex(),
        )

    return result
