import logging

LOGGER = logging.getLogger("custom_components.waterio")
DOMAIN = "waterio"

# -------------------------------------------------------------------
# Device discovery
# -------------------------------------------------------------------
DEVICE_NAME_PREFIX = "Water-IO-Cap"

# Poll interval (seconds) – 60 s so HA captures drink log events before
# the Water.io app connects and calls ClearDataCommand.
UPDATE_INTERVAL = 60

# -------------------------------------------------------------------
# Standard GATT UUIDs (Bluetooth SIG)
# -------------------------------------------------------------------
UUID_BATTERY_LEVEL   = "00002a19-0000-1000-8000-00805f9b34fb"  # Battery Level
UUID_FIRMWARE_REV    = "00002a26-0000-1000-8000-00805f9b34fb"  # Firmware Revision String
UUID_HARDWARE_REV    = "00002a27-0000-1000-8000-00805f9b34fb"  # Hardware Revision String
UUID_MANUFACTURER    = "00002a29-0000-1000-8000-00805f9b34fb"  # Manufacturer Name
UUID_SERIAL_NUMBER   = "00002a25-0000-1000-8000-00805f9b34fb"  # Serial Number
UUID_CCCD            = "00002902-0000-1000-8000-00805f9b34fb"  # Client Char Config Descriptor

# -------------------------------------------------------------------
# Water.io Proprietary UUIDs
# Reverse-engineered from APK v3.5.0 / v4.8.6 DEX + libapp.so
#
# Two firmware generations:
#  "new" caps  – base 00805f9b34fb  (same standard base, 0x0000 prefix)
#  "old" caps  – base 00805f9b0131  (Water.io-specific base, 0x0003 prefix)
#
# Naming convention found in SDK class/string names:
#  CAA group – authentication / pairing / main service
#  CAB group – capability / battery query
#  CBB group – configuration / control
#  CDD group – cap data (write commands + data notifications)
# -------------------------------------------------------------------

# CAA group
UUID_CAA1_NEW = "0000caa1-0000-1000-8000-00805f9b34fb"
UUID_CAA2_NEW = "0000caa2-0000-1000-8000-00805f9b34fb"
UUID_CAA3_NEW = "0000caa3-0000-1000-8000-00805f9b34fb"
UUID_CAB5_NEW = "0000cab5-0000-1000-8000-00805f9b34fb"
UUID_CAA1_OLD = "0003caa1-0000-1000-8000-00805f9b0131"
UUID_CAA2_OLD = "0003caa2-0000-1000-8000-00805f9b0131"
UUID_CAA3_OLD = "0003caa3-0000-1000-8000-00805f9b0131"
UUID_CAB5_OLD = "0003cab5-0000-1000-8000-00805f9b0131"

# CBB group
UUID_CBB1_NEW = "0000cbb1-0000-1000-8000-00805f9b34fb"
UUID_CBBB_NEW = "0000cbbb-0000-1000-8000-00805f9b34fb"
UUID_CBB1_OLD = "0003cbb1-0000-1000-8000-00805f9b0131"
UUID_CBBB_OLD = "0003cbbb-0000-1000-8000-00805f9b0131"

# CDD group – confirmed from live GATT dump:
#   CDD1 = notify  (device pushes responses here)
#   CDD2 = write-without-response  (host sends commands here)
UUID_CDD0 = "0003cdd0-0000-1000-8000-00805f9b0131"  # service root
UUID_CDD1 = "0003cdd1-0000-1000-8000-00805f9b0131"  # notify characteristic
UUID_CDD2 = "0003cdd2-0000-1000-8000-00805f9b0131"  # write-without-response characteristic

# Silicon Labs (Cypress PSoC) OTA/DFU service – confirmed by MTU_SIZE_SILAB_OTA string
UUID_SILAB_OTA_SERVICE = "00060000-f8ce-11e4-abf4-0002a5d5c51b"
UUID_SILAB_OTA_CHAR    = "00060001-f8ce-11e4-abf4-0002a5d5c51b"

# Ordered lists used during characteristic discovery
# Priority order: confirmed live UUIDs first
NOTIFY_CANDIDATES: list[str] = [
    UUID_CDD1,       # confirmed notify on live device
    UUID_CAA3_OLD,
    UUID_CAA3_NEW,
    UUID_CBBB_OLD,
    UUID_CBBB_NEW,
    UUID_CBB1_OLD,
    UUID_CBB1_NEW,
    UUID_CAA2_OLD,
    UUID_CAA2_NEW,
]

WRITE_CANDIDATES: list[str] = [
    UUID_CDD2,       # confirmed write-without-response on live device
    UUID_CAA1_OLD,
    UUID_CAA1_NEW,
    UUID_CAB5_OLD,
    UUID_CAB5_NEW,
    UUID_CBB1_OLD,
    UUID_CBB1_NEW,
]

# -------------------------------------------------------------------
# BLE Protocol opcodes  --  CONFIRMED from v4.8.6 APK
#
# Source: Li3/d (EnumCommandDevice) <clinit> bytecode decoded from
#         classes.dex in io.water.hydration-4.8.6.apk
# Method: source_file_idx -> class mapping + const/4,const/16,const
#         tracing per invoke-direct call in static initializer.
#
# Packet format: [opcode, 0x00, 0x00, payload_len, ...payload...]
#                padded to 20 or 24 bytes.
#
# Advertisement: SVC_DATA uuid=0000180a
#   byte[1] = current water level %  (CONFIRMED: 0x33=51% -> 510mL/1L)
#   byte[0] = previous water level % (or battery %)
#   byte[4] = hardware version
# -------------------------------------------------------------------

# ── Primary poll commands (confirmed from EnumCommandDevice.java v4.8.6 SDK) ─
CMD_RESET_DEVICE       = 0x07   # legacy trigger → auto-pushes 0x58 status notification
CMD_SET_REAL_TIME      = 0x11   # payload: 4B BIG-endian unix timestamp (reversed LE)
CMD_GET_CAP_STATE      = 0x3C   # PRIMARY – 18+ byte response; water + battery + status
CMD_GET_BATTERY        = 0x1B   # response byte[4] = battery %
CMD_GET_HYDRATIONS     = 0x72   # response bytes[4..5]=manual ml, [6..7]=cap ml (LE uint16)
CMD_GET_VERSION        = 0x1D   # returns firmware version string (same as v1.0.5)
CMD_GET_REAL_TIME      = 0x10   # response bytes[4..7] = timestamp LE uint32
CMD_GET_LOG_LENGTH     = 0x0D   # response bytes[4..5] = LE uint16 count
CMD_GET_MAC_ADDRESS    = 0x49   # returns 6-byte BT MAC address
CMD_GET_NEXT_LOGS      = 0x42   # streaming next log chunk
CMD_READ_LOGS          = 0x0E   # read stored logs
CMD_CLEAR_LOGS         = 0x0F   # ACK 0x5A
CMD_SLEEP              = 0x12   # ACK 0x5A
CMD_SET_AWAKE          = 0x13   # ACK 0x5A
CMD_GET_ADVERT_INT     = 0x2C   # GET_ADVERT_INTERVAL_COMMAND
CMD_SET_ADVERT_INT     = 0x2D   # SET_ADVERT_INTERVAL_COMMAND
CMD_GET_REMINDER       = 0x37   # GET_REMINDER_COMMAND
CMD_GET_SILENT_MODE    = 0x67   # GET_SILENT_MODE_COMMAND
CMD_SET_SILENT_MODE    = 0x66   # SET_SILENT_MODE_COMMAND; payload byte = 1/0
CMD_GET_OFFSET_CAL     = 0x62   # GET_OFFSET_CALIBRATION_COMMAND
CMD_ENTER_BOOTLOADER   = 0x99   # ENTER_BOOTLOADER_COMMAND (do not send!)
CMD_GET_SINGLE_MEAS    = 0x01   # GET_SINGLE_MEASUREMENT_ASYNC; ACK 0x5A

# ── Hydration / goal control ───────────────────────────────────────────────
CMD_SET_MANUALLY_HYDRATION = 0x71   # SET_MANUALLY_HYDRATION_COMMAND; payload LE uint16 ml
CMD_SET_HYDRATION_LEVEL    = 0x75   # SET_HYDRATION_LEVEL_COMMAND; payload LE uint16 ml
CMD_SET_EXTRA_DAILY_GOAL   = 0x73   # SET_EXTRA_DAILY_GOAL_COMMAND; payload LE uint16 ml
CMD_GET_EXTRA_DAILY_GOAL   = 0x74   # GET_EXTRA_DAILY_GOAL_COMMAND
CMD_GET_SYNC_INFO          = 0x78   # GET_SYNC_INFO_COMMAND (base_goal, extra_goal, totals)
CMD_SET_DAILY_USAGE_TIME   = 0x22   # SET_DAILY_USAGE_TIME_COMMAND; payload 4B
CMD_SET_MULTI_CONFIG       = 0x4D   # SET_MULTI_PARAM_CONFIG — TLV TypeConfig write
CMD_GET_MULTI_CONFIG       = 0x4E   # GET_MULTI_PARAM_CONFIG — TLV TypeConfig read (dead on RCH04.05.03.41)
# ── Response opcodes ───────────────────────────────────────────────────────
# Responses echo the command opcode in byte[0].
# Generic ACK (0x5A) is returned by all SET_* / action commands.
RESP_STATUS_PUSH       = 0x58   # autonomous push on connect / after CMD_RESET_DEVICE
RESP_ACK               = 0x5A   # generic success ACK (0x5A = 'Z')
RESP_GET_REAL_TIME     = 0x10   # echoes CMD_GET_REAL_TIME
RESP_GET_BATTERY       = 0x1B   # echoes CMD_GET_BATTERY; byte[4] = battery %
RESP_GET_VERSION       = 0x1D   # echoes CMD_GET_VERSION
RESP_SET_REAL_TIME     = 0x5A   # generic ACK
RESP_GET_CAP_STATE     = 0x3C   # echoes CMD_GET_CAP_STATE; 18+ bytes (THE KEY RESPONSE)
RESP_GET_LOG_LENGTH    = 0x0D   # echoes CMD_GET_LOG_LENGTH
RESP_GET_HYDRATIONS    = 0x72   # echoes CMD_GET_HYDRATIONS
RESP_GET_MAC           = 0x49   # echoes CMD_GET_MAC_ADDRESS
RESP_GET_SYNC_INFO     = 0x78   # echoes CMD_GET_SYNC_INFO
RESP_GET_EXTRA_GOAL    = 0x74   # echoes CMD_GET_EXTRA_DAILY_GOAL
RESP_GET_SILENT_MODE   = 0x67   # echoes CMD_GET_SILENT_MODE; byte[4]=0/1
RESP_GET_MAC           = 0x49   # echoes CMD_GET_MAC_ADDRESS; bytes[4..9]=MAC

# -------------------------------------------------------------------
# Data field keys (used in coordinator data dict and sensor keys)
# -------------------------------------------------------------------
FIELD_BATTERY           = "battery"
FIELD_WATER_ML          = "water_ml"         # daily cap-measured hydration (ml)
FIELD_MANUAL_ML         = "manual_ml"         # daily manually-logged hydration (ml)
FIELD_CAP_ML            = "cap_ml"            # cumulative cap sensor total (ml)
FIELD_HYDRATION_STATUS  = "hydration_status"  # % of daily goal achieved (0-100)
FIELD_DAILY_GOAL_ML     = "daily_goal_ml"     # daily base goal from sync info (ml)
FIELD_EXTRA_GOAL_ML     = "extra_goal_ml"     # extra daily goal add-on (ml)
FIELD_TEMPERATURE       = "temperature"
FIELD_FIRMWARE          = "firmware"
FIELD_HARDWARE          = "hardware"
FIELD_MANUFACTURER      = "manufacturer"
FIELD_SERIAL            = "serial"
FIELD_DAILY_GOAL        = "daily_goal"        # legacy alias kept for compat
FIELD_LAST_SYNC         = "last_sync"
FIELD_CAP_CLOSED        = "cap_closed"        # bool: physical cap is closed
FIELD_IS_CHARGING       = "is_charging"       # bool: cap is charging
FIELD_IS_ACTIVE         = "is_active"         # bool: cap in active mode (not sleeping)
FIELD_NEED_DRINK        = "need_to_drink"     # bool: coach says drink now
FIELD_SILENT_MODE       = "silent_mode"       # bool: reminder silence on/off
FIELD_BATTERY_CELL      = "battery_cell"      # int: real cell battery % (from 0x1B)
FIELD_MAC_ADDRESS       = "mac_address"       # str: device BT MAC from 0x49
FIELD_DEVICE_CLOCK      = "device_clock"      # str: device RTC ISO string from 0x10
FIELD_LOG_COUNT         = "log_count"         # int: total log entries on device (0x0D)
FIELD_HYDRATION_LEVEL   = "hydration_level"   # int: % of daily goal achieved (byte[7] of 0x3C)

# Log-derived live fields (computed from READ_LOGS 0x0E every poll)
FIELD_LAST_DRINK_ML      = "last_drink_ml"      # int: mL consumed in last drink event
FIELD_LAST_DRINK_TS      = "last_drink_ts"      # str: ISO timestamp of last drink
FIELD_DRINK_COUNT_TODAY  = "drink_count_today"  # int: number of drink events today
FIELD_WATER_REMAINING_ML = "water_remaining_ml" # int: mL remaining in bottle (latest level reading)

# -------------------------------------------------------------------
# TypeConfig settings field keys
# Each corresponds to one TypeConfig type_byte (SET via 0x4D / GET via 0x4E).
# Firmware RCH04.05.03.41: 0x4E GET returns no data; 0x4D SET works (app-verified).
# Settings are stored in coordinator and persisted via config entry options.
# -------------------------------------------------------------------
FIELD_TZ_OFFSET         = "timezone_offset_min"     # int16: UTC offset in minutes (type 0)
FIELD_WORK_START        = "working_hours_start"     # uint8: hour 0-23 (type 1)
FIELD_WORK_END          = "working_hours_end"       # uint8: hour 0-23 (type 2)
# FIELD_DAILY_GOAL_ML already defined above        # uint16: mL (type 3)
FIELD_REMINDER_LOGIC    = "reminder_logic"          # bool: persistent reminders (type 5)
FIELD_REMINDER_INTERVAL = "reminder_interval_min"   # uint16: minutes (type 6)
FIELD_REMINDER_ROUND    = "reminder_round_min"      # uint16: minutes (type 7)
FIELD_REMINDER_CYCLES   = "reminder_cycles"         # uint8: cycle count (type 9)
FIELD_REMINDER_PATTERN  = "reminder_pattern"        # uint8: 1=bounce,2=pulse,3=snake (type 12)
FIELD_REMINDER_UI       = "reminder_ui_elements"    # uint8 bitmask (type 13)
FIELD_OPEN_CLOSE_LED    = "enable_open_close_led"   # bool (type 36)
FIELD_DAR               = "dar_drink_after_refill"  # bool: DAR logic (type 38)
FIELD_DBR               = "dbr_drink_before_refill" # bool: DBR logic (type 39)
FIELD_MAR               = "mar_after_refill_ml"     # uint16: mL threshold (type 40)
FIELD_MBR               = "mbr_before_refill_ml"    # uint16: mL threshold (type 41)
FIELD_IGR               = "igr_refill_gap_ml"       # uint8: threshold mL (type 42)
FIELD_REMINDER_LED      = "enable_reminder_led"     # bool (type 44)
FIELD_STATUS_LED        = "enable_status_led"       # bool (type 45)
FIELD_DEMO_MODE         = "enable_demo_mode"        # bool (type 46)
FIELD_LED_OFF_HOURS     = "led_off_outside_hours"   # bool (type 49)
FIELD_SHABBAT_MODE      = "enable_shabbat_mode"     # bool (type 50)
FIELD_LED_OFF_CHARGER   = "led_off_in_charger"      # bool (type 52)
FIELD_BOTTLE_VOLUME     = "bottle_volume_type"      # uint8: 0=500mL,1=750mL (type 53)
FIELD_REMINDER_COLOR    = "reminder_color_rgb"      # str: "#RRGGBB" LED color (type 30)

# -------------------------------------------------------------------
# TypeConfig registry
# Maps type_byte -> (field_key, wire_format)
#   "bool"   : 1-byte  (0=False / 1=True)
#   "uint8"  : 1-byte unsigned
#   "uint16" : 2-byte LE unsigned
#   "uint16s": 2-byte LE SIGNED (timezone can be negative)
#   "ui_elem": 1-byte bitmask bit0=blink, bit1=sound, bit2=vibrate
#   "rgb"    : N*3 bytes -> list of "#RRGGBB"
# -------------------------------------------------------------------
TYPECONFIG_FIELDS: dict[int, tuple[str, str]] = {
    0:  (FIELD_TZ_OFFSET,         "uint16s"),
    1:  (FIELD_WORK_START,        "uint8"),
    2:  (FIELD_WORK_END,          "uint8"),
    3:  (FIELD_DAILY_GOAL_ML,     "uint16"),
    5:  (FIELD_REMINDER_LOGIC,    "bool"),
    6:  (FIELD_REMINDER_INTERVAL, "uint16"),
    7:  (FIELD_REMINDER_ROUND,    "uint16"),
    9:  (FIELD_REMINDER_CYCLES,   "uint8"),
    12: (FIELD_REMINDER_PATTERN,  "uint8"),
    13: (FIELD_REMINDER_UI,       "ui_elem"),
    30: (FIELD_REMINDER_COLOR,    "rgb"),
    36: (FIELD_OPEN_CLOSE_LED,    "bool"),
    38: (FIELD_DAR,               "bool"),
    39: (FIELD_DBR,               "bool"),
    40: (FIELD_MAR,               "uint16"),
    41: (FIELD_MBR,               "uint16"),
    42: (FIELD_IGR,               "uint8"),
    44: (FIELD_REMINDER_LED,      "bool"),
    45: (FIELD_STATUS_LED,        "bool"),
    46: (FIELD_DEMO_MODE,         "bool"),
    49: (FIELD_LED_OFF_HOURS,     "bool"),
    50: (FIELD_SHABBAT_MODE,      "bool"),
    52: (FIELD_LED_OFF_CHARGER,   "bool"),
    53: (FIELD_BOTTLE_VOLUME,     "uint8"),
}

# Reverse map: field_key -> type_byte
FIELD_TO_TYPEBYTE: dict[str, int] = {v[0]: k for k, v in TYPECONFIG_FIELDS.items()}

# -------------------------------------------------------------------
# Default setting values — used as fallback when the device hasn't
# reported its TypeConfig (0x4E GET is dead on RCH04.05.03.41).
# Values match the Water.io app factory defaults for a 500 mL bottle.
# Persisted user overrides (config entry options) take priority.
# -------------------------------------------------------------------
DEFAULT_SETTINGS: dict[str, object] = {
    FIELD_DAILY_GOAL_ML:     2000,    # 2 L default daily goal
    FIELD_EXTRA_GOAL_ML:     0,
    FIELD_TZ_OFFSET:         0,       # UTC (user should adjust)
    FIELD_WORK_START:        8,       # 08:00
    FIELD_WORK_END:          22,      # 22:00
    FIELD_REMINDER_LOGIC:    False,   # non-persistent reminders
    FIELD_REMINDER_INTERVAL: 60,      # every 60 min
    FIELD_REMINDER_ROUND:    15,      # 15 min round window
    FIELD_REMINDER_CYCLES:   3,
    FIELD_REMINDER_PATTERN:  1,       # bounce
    FIELD_OPEN_CLOSE_LED:    True,
    FIELD_DAR:               False,
    FIELD_DBR:               False,
    FIELD_MAR:               200,
    FIELD_MBR:               200,
    FIELD_IGR:               50,
    FIELD_REMINDER_LED:      True,
    FIELD_STATUS_LED:        True,
    FIELD_DEMO_MODE:         False,
    FIELD_LED_OFF_HOURS:     False,
    FIELD_SHABBAT_MODE:      False,
    FIELD_LED_OFF_CHARGER:   False,
    FIELD_BOTTLE_VOLUME:     0,       # 500 mL
    FIELD_SILENT_MODE:       False,
    # NOTE: FIELD_REMINDER_COLOR is intentionally NOT in defaults.
    # 0x4E GET_MULTI_CONFIG is dead on RCH04.05.03.41 so we can never
    # read the real color back from the device. Omitting the default lets
    # the HA light entity show "unknown" until the user sets it via HA.
}
