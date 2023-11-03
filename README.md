# waterio
[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/chepa92/waterio)

Home Assistant integration for BLE based Water.io smart bottles.

Supports pulling data from water.io bottles

## Installation

Note: Restart is always required after installation.

### [HACS](https://hacs.xyz/) (recommended)
Installation can be done through [HACS custom repository](https://hacs.xyz/docs/faq/custom_repositories).

### Manual installation
You can manually clone this repository inside `config/custom_components/waterio`.

For  example, from Terminal plugin:
```
cd /config/custom_components
git clone https://github.com/chepa/waterio waterio
```

## Setup
After installation, you should find WaterIo under the Configuration -> Integrations -> Add integration.

The setup step includes discovery which will list out all Water.io bottles discovered. The setup will validate connection by checking battery status. Make sure your bottle near BLE module.

The setup needs to be repeated for each bottle.

## Features
1. Discovery: Automatically discover Water.io bottles without manually hunting for Bluetooth MAC address
3. Battery Status
5. Multiple Bottle support

## Debugging
Add the following to `configuration.yml` to show debugging logs. Please make sure to include debug logs when filing an issue.

See [logger intergration docs](https://www.home-assistant.io/integrations/logger/) for more information to configure logging.

```yml
logger:
  default: warn
  logs:
    custom_components.waterio: debug
```

## Credits
Special thanks to water.io that not wants to tell about their protocol :)
