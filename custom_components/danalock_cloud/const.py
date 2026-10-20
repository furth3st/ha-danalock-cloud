"""Constants for the Danalock Cloud integration."""
from datetime import timedelta
from typing import Final

from homeassistant.const import Platform

DOMAIN: Final = "danalock_cloud"
PLATFORMS: Final = [Platform.LOCK, Platform.SENSOR, Platform.UPDATE]

# Configuration
CONF_USERNAME: Final = "username"
CONF_PASSWORD: Final = "password"
CONF_TOTP: Final = "totp"  # optional 2FA code, used once at login, never stored
CONF_UPDATE_INTERVAL: Final = "update_interval"
CONF_OPTIMISTIC_MODE: Final = "optimistic_mode"

# API endpoints
API_BASE_URL: Final = "https://api.danalock.com"
BRIDGE_API_BASE_URL: Final = "https://bridge.danalockservices.com/bridge/v1"
TOKEN_URL: Final = f"{API_BASE_URL}/oauth2/token"
LOCKS_URL: Final = f"{API_BASE_URL}/locks/v1"
EXECUTE_URL: Final = f"{BRIDGE_API_BASE_URL}/execute"
POLL_URL: Final = f"{BRIDGE_API_BASE_URL}/poll"

# API behaviour
CLIENT_ID: Final = "danalock-web"
DEFAULT_TIMEOUT: Final = 20
DEFAULT_TOKEN_LIFETIME: Final = 3600.0  # used when Danalock omits/garbles expires_in
TOKEN_REFRESH_MARGIN: Final = 60  # refresh this many seconds before expiry
# Job polling: the official web app polls every 0.5 s. Poll fast while a job is
# fresh (where nearly all jobs finish), then back off for slow Bluetooth hops.
JOB_POLL_FAST_INTERVAL: Final = 0.5
JOB_POLL_FAST_WINDOW: Final = 20
JOB_POLL_INTERVAL: Final = 2
JOB_POLL_TIMEOUT: Final = 70
BRIDGE_BUSY_RETRIES: Final = 2
BRIDGE_BUSY_RETRY_DELAY: Final = 3  # seconds, multiplied by attempt number
LOCK_LOADING_RETRY_DELAY: Final = 10  # bridge status 4: "lock is loading, wait 10 seconds"
COMMAND_TIMEOUT_RETRIES: Final = 1  # lock/unlock is idempotent ("nothing changed" = done)

# Polling
UPDATE_INTERVAL: Final = timedelta(minutes=5)
BATTERY_REFRESH_INTERVAL: Final = timedelta(hours=1)  # battery changes slowly; spare the bridge
COMMAND_UPDATE_DELAY: Final = timedelta(seconds=15)
AUTO_LOCK_CHECK_MARGIN: Final = timedelta(seconds=8)  # re-read this long after auto-lock should fire
SETTINGS_REFRESH_INTERVAL: Final = timedelta(hours=12)  # settings/firmware change rarely
DEFAULT_COMMAND_SECONDS: Final = 8.0  # initial estimate shown by the card's progress bar

# Data keys
ACCESS_TOKEN: Final = "access_token"
REFRESH_TOKEN: Final = "refresh_token"
EXPIRES_IN: Final = "expires_in"
TOKEN_EXPIRES_AT: Final = "token_expires_at"
LOCK_SERIAL: Final = "serial_number"
LOCK_NAME: Final = "name"
LOCK_STATE: Final = "state"
LOCK_BATTERY: Final = "battery_level"

# Lock states from API
API_STATE_LOCKED: Final = "Locked"
API_STATE_UNLOCKED: Final = "Unlocked"

# Bridge operations (names as used by the official my.danalock.com web app)
OP_GET_STATE: Final = "afi.lock.get-state"
OP_GET_BATTERY: Final = "afi.power-source.get-information2"
OP_GET_SETTINGS: Final = "afi.lock.get-settings"
OP_GET_DEVICE_INFO: Final = "afi.device.get-information"
OP_BATCH: Final = "batch"  # several operations in one bridge -> lock Bluetooth session
OP_LOCK: Final = "afi.lock.operate"
OP_UNLOCK: Final = "afi.lock.operate"
ARG_LOCK: Final = "lock"
ARG_UNLOCK: Final = "unlock"

# Bridge job status
JOB_STATUS_SUCCEEDED: Final = "Succeeded"
JOB_STATUS_FAILED: Final = "Failed"

# Status codes in a job result (meanings from the official web app's error texts
# and the lock's AFI protocol)
BRIDGE_STATUS_LOCK_LOADING: Final = 4  # "The lock is currently loading. Please wait 10 seconds"
BRIDGE_STATUS_UNREACHABLE: Final = 5  # bridge has no Wi-Fi/power or cannot reach the lock
AFI_STATUS_BUSY: Final = 0x05
AFI_STATUS_NOTHING_CHANGED: Final = 0x07  # already in the requested position
AFI_STATUS_CRITICAL_BATTERY: Final = 0x0B
AFI_STATUS_BLOCKED: Final = 0x17  # jammed
AFI_TEXT_JOB_TIMEOUT: Final = "JobTimeout"

# Firmware
FIRMWARE_LATEST_URL: Final = "https://firmware-upgrade.danalockservices.com/firmware/v1/by-serial-number/{serial}/latest"

# Data keys added in 1.4
LOCK_JAMMED: Final = "jammed"
LOCK_SETTINGS: Final = "settings"
LOCK_FIRMWARE: Final = "firmware_version"
LOCK_LATEST_FIRMWARE: Final = "latest_firmware_version"
LOCK_STATE_UPDATED: Final = "state_updated"

# Services
SERVICE_REFRESH_DEVICES: Final = "refresh_devices"

# Events
EVENT_LOCK_COMMAND_SUCCESS: Final = f"{DOMAIN}_command_success"
EVENT_LOCK_COMMAND_FAILURE: Final = f"{DOMAIN}_command_failure"
