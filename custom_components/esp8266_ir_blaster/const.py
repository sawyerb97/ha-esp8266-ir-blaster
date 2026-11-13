"""Constants for the ESP8266 IR Blaster integration."""

DOMAIN = "esp8266_ir_blaster"

CONF_PASSCODE = "passcode"
CONF_SCAN_INTERVAL = "scan_interval"

DEFAULT_PORT = 80
DEFAULT_SCAN_INTERVAL = 10  # seconds
DEFAULT_LEARN_TIMEOUT = 30  # seconds
REQUEST_TIMEOUT = 10  # seconds

NUM_OUTPUTS = 4

EVENT_CODE_RECEIVED = f"{DOMAIN}_code_received"

# Protocols the firmware can transmit by name (irblast() in IRController.ino).
# Anything else (UNKNOWN, SAMSUNG36, ...) is learned and sent as raw timings.
SENDABLE_TYPES = {
    "coolix", "denon", "dish", "gree", "jvc", "lg", "lutron", "nec",
    "panasonic", "rc5", "rc6", "rcmm", "roomba", "samsung", "sharp",
    "sharpraw", "sony", "whynter",
}

MANUFACTURER = "mdhiggins (ESP8266-HTTP-IR-Blaster)"
MODEL = "ESP8266 IR Controller"
