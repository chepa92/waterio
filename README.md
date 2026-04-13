# Water.io

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/chepa92/waterio)

Home Assistant custom integration for **Water.io** BLE smart bottles.
Reverse-engineered from APK v3.5.0 / v4.8.6 — fully local, no cloud required.

## Installation

> **Note:** A restart is always required after installation.

### [HACS](https://hacs.xyz/) (recommended)

Install via [HACS custom repository](https://hacs.xyz/docs/faq/custom_repositories).

### Manual

```bash
cd /config/custom_components
git clone https://github.com/chepa/waterio waterio
```

## Setup

1. Go to **Settings → Devices & Services → Add Integration** and search for **Water.io**.
2. The setup wizard discovers all nearby `Water-IO-Cap*` BLE devices automatically.
3. Select your bottle – the wizard validates the connection by reading battery status.
4. Repeat for each additional bottle.

> **Tip:** Make sure the bottle is within BLE range and the cap is awake (open & close the cap once).

## Entities

### Sensors

| Entity | Description |
|---|---|
| **Water Intake Today** | Daily cumulative mL consumed (cap sensor + log deltas). Resets at midnight. |
| **Water Consumed (Total)** | Lifetime cumulative mL (`TOTAL_INCREASING`). Useful for HA Energy-style dashboards. |
| **Manual Hydration Today** | mL logged manually via the Water.io app *(disabled by default)*. |
| **Hydration Status Tier** | Raw tier: 0 = None, 1 = Low, 2 = Good, 3 = Excellent *(disabled by default)*. |
| **Hydration Goal Progress** | % of daily goal achieved (0–100+). |
| **Daily Hydration Goal** | Current base daily goal in mL. |
| **Extra Daily Goal** | Additional goal add-on in mL *(disabled by default)*. |
| **Last Drink Amount** | mL consumed in the most recent drink event. |
| **Last Drink Time** | Timestamp of the most recent drink event. |
| **Drinks Today** | Number of individual drink events detected today. |
| **Water Remaining in Bottle** | Current water level in mL (from latest `'l'` / `'L'` / `'U'` log entry). |
| **Battery** | Charge-circuit battery % (standard GATT `0x2A19`). |
| **Battery Cell Life** | Raw hardware cell reading % *(diagnostic)*. |
| **Last Sync** | Timestamp of last successful BLE poll. |
| **Device Clock** | Cap's internal RTC as ISO timestamp *(diagnostic)*. |
| **Log Entries On Device** | Number of stored log entries on the cap *(diagnostic)*. |
| **MAC Address** | Device Bluetooth MAC *(diagnostic)*. |
| **Firmware Version** | Cap firmware string *(diagnostic, disabled by default)*. |
| **Hardware Version** | Cap hardware revision *(diagnostic, disabled by default)*. |

### Binary Sensors

| Entity | Description |
|---|---|
| **Cap Closed** | Whether the physical bottle cap is closed. |
| **Charging** | Whether the cap is currently charging. |
| **Active Mode** | Whether the cap is in active mode (not sleeping). |
| **Drink Reminder** | The cap's hydration coach says "drink now". |

### Switches (Settings)

| Entity | Description |
|---|---|
| **Silent Mode** | Mute all cap vibration/LED reminders (dedicated BLE command `0x66`). |
| **Persistent Reminders** | Keep repeating reminders when hydration goal is not met. |
| **Status LED** | Enable/disable the status LED. |
| **Open/Close LED** | LED flash when cap is opened/closed. |
| **LED Off Outside Active Hours** | Turn LEDs off outside configured working hours. |
| **LED Off While Charging** | Turn LEDs off when cap is on the charger. |
| **Drink-After-Refill (DAR)** | Enable firmware refill detection (drink after refill events). |
| **Drink-Before-Refill (DBR)** | Enable firmware refill detection (drink before refill events). |
| **Demo Mode** | Cap demo/showroom mode. |
| **Shabbat Mode** | Disable all electronics activity for Shabbat observance. |

### Number Controls (Settings)

| Entity | Description |
|---|---|
| **Daily Hydration Goal** | Base daily goal (500–5000 mL, step 50). |
| **Extra Daily Goal** | Additional daily goal add-on (0–2000 mL, step 50). |
| **Active Hours Start / End** | Working hours window (0–23 h). Reminders only fire within this window. |
| **Timezone Offset** | UTC offset in minutes (−720 to +840, step 30). |
| **Reminder Interval** | Minutes between reminders (15–480, step 15). |
| **Reminder Round Duration** | How long each reminder round lasts (1–120 min). |
| **Reminder Cycles** | Number of reminder repetitions per round (1–20). |
| **MAR Threshold** | Minimum mL after refill to qualify as DAR event (0–2000). |
| **MBR Threshold** | Minimum mL before refill to qualify as DBR event (0–2000). |
| **IGR Refill Gap** | Refills below this mL are discarded as noise (0–250). |

### Select Controls (Settings)

| Entity | Description |
|---|---|
| **Reminder LED Pattern** | Vibration/LED animation: `bounce`, `pulse`, `snake`. |
| **Bottle Volume** | Bottle capacity type: `500 mL` or `750 mL`. |

### Light

| Entity | Description |
|---|---|
| **Reminder LED** | On/off toggle + RGB color picker for the reminder LED ring. |

### Buttons

| Entity | Description |
|---|---|
| **Find My Bottle** | Trigger the cap's "find me" vibration/LED sequence. |
| **Force Sync** | Immediately trigger a full BLE poll (outside the normal 60 s interval). |

## How It Works

### Polling

The integration connects over BLE every **60 seconds** and issues a sequence of commands:

1. `GET_CAP_STATE` (0x3C) — water level, battery, cap status flags
2. `GET_HYDRATIONS` (0x72) — daily manual + cap mL counters
3. `GET_BATTERY` (0x1B) — cell-level battery reading
4. `GET_REAL_TIME` (0x10) — cap's internal RTC
5. `GET_LOG_LENGTH` (0x0D) — how many log entries exist
6. `READ_LOGS` (0x0E) — download new log entries (batches of 50)
7. `GET_SYNC_INFO` (0x78) — base goal, extra goal, totals
8. `GET_SILENT_MODE` (0x67) — current silent mode state
9. Standard GATT reads — battery level, firmware, hardware, serial

### Drink Detection

Drinks are detected by analyzing log entries from the cap's flash memory:

- Log entry type `'l'` (0x6C) = water level in mL (firmware-converted)
- Log entry types `'L'` (0x4C) / `'U'` (0x55) = accurate/estimated measurements
- **Minimum detectable drink: 30 mL** (hardcoded threshold from `HydrationRepo.m3820m()`)
- Level decrease ≥ 30 mL between consecutive readings = **drink event**
- Level increase = **refill event**
- Log entry `'Ü'` (0xDC) = HydrationV2 (drink amount in 5 mL units) — used as fallback only

### Settings Persistence

All writable settings use the **TypeConfig** protocol (opcode `0x4D` SET_MULTI_CONFIG).
Settings are TLV-encoded: `[type_byte, length, value...]`. Changes are written to the cap
firmware and also persisted in the HA config entry options so they survive restarts.

## Debugging

Add the following to `configuration.yaml`:

```yaml
logger:
  default: warn
  logs:
    custom_components.waterio: debug
```

See the [Logger integration docs](https://www.home-assistant.io/integrations/logger/) for more detail.

## Known Limitations

- The Water.io app's **ClearDataCommand** erases log entries on the cap after syncing.
  If the app syncs before HA polls, those entries are lost. The 60 s poll interval
  minimizes this window.
- `GET_MULTI_CONFIG` (0x4E) is **dead on firmware RCH04.05.03.41** — the cap returns
  no data. Settings are written blind via `SET_MULTI_CONFIG` (0x4D) and tracked locally.
- On **pv=12 firmware**, `'L'` and `'U'` log entries carry raw ultrasonic ADC values
  (10000–30000 range) instead of mL. Only `'l'` and `'Ð'` (0xD0) carry firmware-converted
  mL. The integration automatically guards against this with a 2× bottle capacity filter.

## Credits

Entire BLE protocol reverse-engineered from the Water.io Android APK (v3.5.0 / v4.8.6)
using JADX decompilation. No official documentation exists — Water.io does not publish
their protocol.

## Legal Notice

This is an **independent, non-commercial project** for interoperability purposes only.

- Reverse engineering was performed under interoperability exemptions (EU Directive 91/250/EEC Article 6, US fair use)
- **No Water.io source code was copied** — this is a clean-room Python implementation
- Protocol observation does not constitute trade secret misappropriation
- Water.io is a trademark of Water.io Ltd. — this project is not affiliated with or endorsed by them

See [NOTICE](NOTICE) file for full reverse engineering disclosure.

If Water.io Ltd. provides an official Home Assistant integration or API, this project will be deprecated in its favor.
