"""Constants for Nightscout Extended."""

DOMAIN = "nightscout_extended"

CONF_USE_API_SECRET = "use_api_secret"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_SGV_HISTORY_DAYS = "sgv_history_days"
SERVICE_GET_GLUCOSE_HISTORY = "get_glucose_history"
CONF_RESERVOIR_UPLOADER = "reservoir_uploader"

DEFAULT_SCAN_INTERVAL = 60  # seconds; CGM readings arrive every 5 minutes (1 for some sensors)
MIN_SCAN_INTERVAL = 30

# Nightscout's own defaults for BG_LOW, BG_TARGET_BOTTOM, BG_TARGET_TOP and BG_HIGH.
DEFAULT_THRESHOLDS_MGDL = {"urgent_low": 55.0, "low": 80.0, "high": 180.0, "urgent_high": 260.0}

UNIT_MGDL = "mg/dL"
UNIT_MMOL = "mmol/L"
