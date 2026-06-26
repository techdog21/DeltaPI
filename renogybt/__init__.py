# Vendored from https://github.com/cyrils/renogy-bt (MIT), trimmed to the battery
# path only and patched in BLEManager.py to handle the Pro batteries' duplicate
# GATT characteristic UUIDs (notify subscription). See README / DeltaPI notes.
from .BatteryClient import BatteryClient
from .Utils import *
