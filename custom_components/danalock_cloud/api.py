"""API client for Danalock Cloud."""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from time import monotonic, time
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    ACCESS_TOKEN,
    AFI_STATUS_BLOCKED,
    AFI_STATUS_BUSY,
    AFI_STATUS_CRITICAL_BATTERY,
    AFI_STATUS_NOTHING_CHANGED,
    AFI_TEXT_JOB_TIMEOUT,
    ARG_LOCK,
    ARG_UNLOCK,
    BRIDGE_BUSY_RETRIES,
    BRIDGE_BUSY_RETRY_DELAY,
    BRIDGE_STATUS_LOCK_LOADING,
    BRIDGE_STATUS_UNREACHABLE,
    CLIENT_ID,
    COMMAND_TIMEOUT_RETRIES,
    CONF_PASSWORD,
    CONF_USERNAME,
    DEFAULT_TIMEOUT,
    DEFAULT_TOKEN_LIFETIME,
    EXECUTE_URL,
    EXPIRES_IN,
    FIRMWARE_LATEST_URL,
    JOB_POLL_FAST_INTERVAL,
    JOB_POLL_FAST_WINDOW,
    JOB_POLL_INTERVAL,
    JOB_POLL_TIMEOUT,
    JOB_STATUS_FAILED,
    JOB_STATUS_SUCCEEDED,
    LOCK_BATTERY,
    LOCK_FIRMWARE,
    LOCK_LOADING_RETRY_DELAY,
    LOCK_NAME,
    LOCK_SERIAL,
    LOCK_SETTINGS,
    LOCK_STATE,
    LOCKS_URL,
    OP_BATCH,
    OP_GET_BATTERY,
    OP_GET_DEVICE_INFO,
    OP_GET_SETTINGS,
    OP_GET_STATE,
    OP_LOCK,
    OP_UNLOCK,
    POLL_URL,
    REFRESH_TOKEN,
    TOKEN_EXPIRES_AT,
    TOKEN_REFRESH_MARGIN,
    TOKEN_URL,
    API_STATE_LOCKED,
    API_STATE_UNLOCKED,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/90.0.4430.212 Safari/537.36"
)
FORM_HEADERS = {"content-type": "application/x-www-form-urlencoded"}
JSON_HEADERS = {"content-type": "application/json", "Accept": "application/json"}


class DanalockApiClientError(Exception):
    """Transient problem talking to Danalock (network, timeout, 5xx). Never a credential problem."""


class DanalockApiAuthError(DanalockApiClientError):
    """Danalock rejected the credentials or token (HTTP 400 invalid_grant / 401)."""


class DanalockApiError(DanalockApiClientError):
    """Danalock returned an unexpected HTTP status or payload."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class DanalockJobError(DanalockApiClientError):
    """A bridge job failed, was rejected, or timed out.

    Carries the status fields of the job result so callers can tell a busy bridge,
    a jammed lock or an "already in that position" answer apart.
    """

    def __init__(
        self,
        message: str,
        *,
        result: dict[str, Any] | None = None,
        timed_out: bool = False,
    ) -> None:
        super().__init__(message)
        self.result: dict[str, Any] = result or {}
        self.bridge_status = _as_int(self.result.get("bridge_server_status"))
        self.afi_status = _as_int(self.result.get("afi_status"))
        self.text = " ".join(
            str(self.result.get(key) or "") for key in ("bridge_server_status_text", "afi_status_text")
        ).strip()
        self._timed_out = timed_out

    @property
    def busy(self) -> bool:
        """The bridge or lock is handling another operation; retrying shortly helps."""
        return "busy" in self.text.lower() or "busy" in str(self).lower() or self.afi_status == AFI_STATUS_BUSY

    @property
    def lock_loading(self) -> bool:
        """Bridge status 4: the lock needs ~10 s before it accepts another operation."""
        return self.bridge_status == BRIDGE_STATUS_LOCK_LOADING

    @property
    def unreachable(self) -> bool:
        """Bridge status 5 / not attached: bridge offline or no Bluetooth link to the lock."""
        return self.bridge_status == BRIDGE_STATUS_UNREACHABLE or "notattached" in self.text.lower()

    @property
    def timed_out(self) -> bool:
        """The bridge (or our poll loop) gave up waiting for the lock."""
        return self._timed_out or AFI_TEXT_JOB_TIMEOUT.lower() in self.text.lower()

    @property
    def bridge_timed_out(self) -> bool:
        """The bridge itself reported JobTimeout (quick); not our own 70 s poll limit."""
        return AFI_TEXT_JOB_TIMEOUT.lower() in self.text.lower()

    @property
    def nothing_changed(self) -> bool:
        """The lock was already in the requested position."""
        return self.afi_status == AFI_STATUS_NOTHING_CHANGED

    @property
    def jammed(self) -> bool:
        """The bolt is blocked."""
        return self.afi_status == AFI_STATUS_BLOCKED

    @property
    def critical_battery(self) -> bool:
        return self.afi_status == AFI_STATUS_CRITICAL_BATTERY

    @property
    def transient(self) -> bool:
        return self.busy or self.lock_loading or self.unreachable or self.timed_out

    @property
    def user_message(self) -> str:
        """A human explanation (texts adapted from the official Danalock web app)."""
        if self.jammed:
            return "the lock is jammed (the bolt is blocked)"
        if self.critical_battery:
            return "the lock battery is critically low; replace the batteries"
        if self.lock_loading:
            return "the lock is still busy with a previous operation; try again in 10 seconds"
        if self.busy:
            return "the Danabridge is busy with another operation"
        if self.timed_out:
            return "the request timed out (slow or unstable Wi-Fi or Bluetooth between bridge and lock)"
        if self.unreachable:
            return "the Danabridge could not reach the lock; check the bridge's power and Wi-Fi"
        return str(self)


class DanalockJobSkipped(DanalockJobError):
    """A background read was skipped so a lock/unlock command could go first."""


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_state(result: Any) -> str | None:
    state = result.get("state") if isinstance(result, dict) else None
    return state if state in (API_STATE_LOCKED, API_STATE_UNLOCKED) else None


def _parse_battery(result: Any) -> int | None:
    battery = _as_int(result.get("battery_level")) if isinstance(result, dict) else None
    return battery if battery is not None and 0 <= battery <= 100 else None


def _parse_settings(result: Any) -> dict[str, int] | None:
    """afi.lock.get-settings: auto_lock (s, 0 = off), hold_back_latch (s), end_to_end_mode."""
    if not isinstance(result, dict):
        return None
    settings = {
        key: value
        for key in ("auto_lock", "hold_back_latch", "end_to_end_mode")
        if (value := _as_int(result.get(key))) is not None
    }
    return settings or None


def _parse_firmware(result: Any) -> str | None:
    version = result.get("firmware_version") if isinstance(result, dict) else None
    if isinstance(version, str) and version.strip():
        return version.strip()
    if isinstance(version, (list, tuple)) and version:
        return ".".join(str(part) for part in version)
    return None


def _expires_at_from(response: dict[str, Any]) -> float:
    """Turn an OAuth `expires_in` (may be missing, a string, or junk) into an absolute timestamp."""
    try:
        lifetime = float(response.get(EXPIRES_IN, DEFAULT_TOKEN_LIFETIME))
    except (TypeError, ValueError):
        lifetime = DEFAULT_TOKEN_LIFETIME
    if lifetime <= 0:
        lifetime = DEFAULT_TOKEN_LIFETIME
    return time() + lifetime


class DanalockApiClient:
    """Danalock Cloud API client.

    Credential safety rules:
    * Only an explicit rejection from the token endpoint (400/401) counts as an auth
      failure. Outages, timeouts and 5xx responses never discard tokens or the password.
    * After a genuine rejection the client stops retrying (to avoid account lockout)
      until the entry is reloaded by a successful re-authentication.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        username: str,
        password: str | None = None,
        entry: ConfigEntry | None = None,
        access_token: str | None = None,
        refresh_token: str | None = None,
        token_expires_at: float | None = None,
    ) -> None:
        """Initialize the API client."""
        self._hass = hass
        self._entry = entry
        self._username = username.strip()
        self._password = password
        self._access_token = access_token
        self._refresh_token = refresh_token
        try:
            self._token_expires_at = float(token_expires_at or 0.0)
        except (TypeError, ValueError):
            self._token_expires_at = 0.0
        self._pending_refresh_token: str | None = None
        self._credentials_rejected = False
        self._session = async_get_clientsession(hass)
        self._token_lock = asyncio.Lock()
        # A Danabridge can only run one operation at a time; serialize all bridge jobs
        # (polls and commands) for this account so they queue instead of colliding.
        self._bridge_lock = asyncio.Lock()
        # lock/unlock commands waiting for the bridge: queued background reads give way to them
        self._commands_waiting = 0
        # None = not tried yet; False = this bridge/backend rejected "batch", use single reads
        self._batch_supported: bool | None = None
        # recent bridge jobs (no secrets) for diagnostics: where does the time go?
        self.job_history: deque[dict[str, Any]] = deque(maxlen=25)

    # ------------------------------------------------------------------ persistence

    def _persist_updated_tokens(self) -> None:
        """Save current tokens/password back to the config entry (if any)."""
        if not self._entry:
            return
        new_data = {
            **self._entry.data,
            CONF_USERNAME: self._username,
            CONF_PASSWORD: self._password,
            ACCESS_TOKEN: self._access_token,
            REFRESH_TOKEN: self._refresh_token,
            TOKEN_EXPIRES_AT: self._token_expires_at,
        }
        if dict(self._entry.data) != new_data:
            _LOGGER.debug("Persisting updated tokens for %s", self._username)
            self._hass.config_entries.async_update_entry(self._entry, data=new_data)

    def _activate_pending_refresh_token(self) -> None:
        """Promote a rotated refresh token once it has proven to work."""
        if self._pending_refresh_token:
            self._refresh_token = self._pending_refresh_token
            self._pending_refresh_token = None
            self._persist_updated_tokens()

    def _token_is_fresh(self) -> bool:
        return bool(self._access_token) and self._token_expires_at > time() + TOKEN_REFRESH_MARGIN

    # ------------------------------------------------------------------ HTTP

    async def _raw_request(
        self,
        method: str,
        url: str,
        *,
        data: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, Any]:
        """Perform one HTTP request. Returns (status, parsed JSON or text). Raises only on transport errors."""
        final_headers = {"User-Agent": DEFAULT_USER_AGENT, **(headers or {})}
        try:
            async with self._session.request(
                method,
                url,
                headers=final_headers,
                data=data,
                json=json,
                timeout=aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT),
            ) as response:
                text = await response.text()
                try:
                    body: Any = await response.json(content_type=None)
                except (ValueError, aiohttp.ContentTypeError):
                    body = text
                return response.status, body
        except (aiohttp.ClientError, TimeoutError, asyncio.TimeoutError) as err:
            raise DanalockApiClientError(f"Cannot reach Danalock ({method} {url}): {err}") from err

    async def _token_request(self, form: dict[str, str]) -> dict[str, Any]:
        """POST to the OAuth token endpoint and classify the outcome."""
        status, body = await self._raw_request("POST", TOKEN_URL, data=form, headers=FORM_HEADERS)
        if status in (400, 401):
            error = body.get("error") if isinstance(body, dict) else None
            description = body.get("error_description") if isinstance(body, dict) else None
            raise DanalockApiAuthError(
                f"Danalock rejected the login ({status} {error or ''}: {description or body})".strip()
            )
        if not 200 <= status < 300:
            raise DanalockApiError(f"Token endpoint returned HTTP {status}: {body}", status)
        if not isinstance(body, dict) or not body.get(ACCESS_TOKEN):
            raise DanalockApiError(f"Token endpoint returned an unexpected payload: {body!r}", status)
        return body

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """Authenticated request with one transparent retry after a 401."""
        for attempt in (1, 2):
            await self.async_ensure_token_valid()
            status, body = await self._raw_request(
                method,
                url,
                json=json,
                headers={**(headers or {}), "Authorization": f"Bearer {self._access_token}"},
            )
            if status == 401 and attempt == 1:
                _LOGGER.debug("Got 401 from %s; forcing token refresh and retrying once", url)
                self._token_expires_at = 0.0
                continue
            if status == 401:
                raise ConfigEntryAuthFailed(f"Danalock keeps rejecting the access token for {url}")
            if not 200 <= status < 300:
                raise DanalockApiError(f"HTTP {status} from {method} {url}: {body}", status)
            self._activate_pending_refresh_token()
            return body
        raise DanalockApiError(f"Request to {url} failed")  # pragma: no cover - loop always returns/raises

    # ------------------------------------------------------------------ auth

    async def async_ensure_token_valid(self) -> None:
        """Make sure a usable access token exists.

        Raises ConfigEntryAuthFailed only when Danalock explicitly rejected the credentials;
        raises DanalockApiClientError for anything transient (caller should retry later).
        """
        async with self._token_lock:
            if self._token_is_fresh():
                return
            if self._credentials_rejected:
                raise ConfigEntryAuthFailed("Danalock credentials were rejected; re-authentication required")

            if self._refresh_token:
                try:
                    await self._refresh_access_token()
                    return
                except DanalockApiAuthError as err:
                    _LOGGER.info("Refresh token rejected (%s); falling back to password login", err)
                    self._refresh_token = self._pending_refresh_token = None
                    self._access_token = None
                    self._token_expires_at = 0.0
                # DanalockApiClientError (outage) propagates untouched: keep all credentials.

            if self._password:
                try:
                    await self.authenticate(self._username, self._password)
                    return
                except DanalockApiAuthError as err:
                    self._credentials_rejected = True
                    self._persist_updated_tokens()
                    raise ConfigEntryAuthFailed(
                        "Danalock rejected the stored password; please re-authenticate"
                    ) from err

            self._credentials_rejected = True
            raise ConfigEntryAuthFailed("No valid Danalock token or password available")

    # Backwards-compatible name used by older code paths.
    _ensure_token_valid = async_ensure_token_valid

    async def authenticate(self, username: str, password: str, totp: str | None = None) -> dict[str, Any]:
        """Log in with username/password (+ optional 2FA code).

        Returns a dict that always contains access_token, refresh_token and token_expires_at.
        Raises DanalockApiAuthError for bad credentials, DanalockApiClientError otherwise.
        """
        username = username.strip()
        form = {
            "grant_type": "password",
            "username": username,
            "password": password,
            "client_id": CLIENT_ID,
        }
        totp = (totp or "").replace(" ", "").strip()
        if totp:
            # Documented by Danalock: "Time-based one-time password if two factor
            # authentication is required for login."
            form["totp"] = totp
        response = await self._token_request(form)
        self._access_token = response[ACCESS_TOKEN]
        self._refresh_token = response.get(REFRESH_TOKEN) or self._refresh_token
        self._token_expires_at = _expires_at_from(response)
        self._pending_refresh_token = None
        self._username = username
        self._password = password
        self._credentials_rejected = False
        self._persist_updated_tokens()
        return {
            ACCESS_TOKEN: self._access_token,
            REFRESH_TOKEN: self._refresh_token,
            TOKEN_EXPIRES_AT: self._token_expires_at,
        }

    async def _refresh_access_token(self) -> None:
        """Exchange the refresh token for a new access token (and possibly a rotated refresh token)."""
        if not self._refresh_token:
            raise DanalockApiAuthError("No refresh token")
        response = await self._token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
                "client_id": CLIENT_ID,
            }
        )
        self._access_token = response[ACCESS_TOKEN]
        self._token_expires_at = _expires_at_from(response)
        # Use a rotated refresh token right away: once issued, the old one may already be
        # invalid, and saving the old one would force a password login after a restart.
        self._refresh_token = response.get(REFRESH_TOKEN) or self._refresh_token
        self._pending_refresh_token = None
        self._persist_updated_tokens()

    # ------------------------------------------------------------------ locks

    async def get_locks(self) -> list[dict[str, Any]]:
        """Return [{serial_number, name}] for every lock on the account."""
        response = await self._request("GET", LOCKS_URL, headers=JSON_HEADERS)
        if not isinstance(response, list):
            raise DanalockApiError(f"Unexpected locks payload: {response!r}")
        locks: list[dict[str, Any]] = []
        for item in response:
            if not isinstance(item, dict):
                continue
            afi = item.get("afi")
            serial = afi.get("serial_number") if isinstance(afi, dict) else None
            if not serial:
                # e.g. type "mwm" locks: not Danalock V3 (afi) devices, so the bridge can't drive them
                _LOGGER.warning(
                    "Skipping lock %r (type %r): only Danalock V3 locks paired with a Danabridge V3 are supported",
                    item.get("name"),
                    item.get("type"),
                )
                continue
            locks.append({LOCK_SERIAL: serial, LOCK_NAME: item.get("name") or f"Danalock {serial}"})
        _LOGGER.debug("Found %d Danalock lock(s)", len(locks))
        return locks

    # ------------------------------------------------------------------ reads

    async def get_lock_data(
        self,
        serial_number: str,
        *,
        include_battery: bool = True,
        include_details: bool = False,
    ) -> dict[str, Any]:
        """Read state (+ battery, + settings/firmware) for one lock.

        Uses one "batch" bridge job when possible (one Bluetooth session instead of
        several). Never raises except for auth failures: anything that cannot be read
        right now comes back as None so the caller keeps its last known value.
        """
        operations = [OP_GET_STATE]
        if include_battery:
            operations.append(OP_GET_BATTERY)
        if include_details:
            operations += [OP_GET_SETTINGS, OP_GET_DEVICE_INFO]

        results: dict[str, Any] = {}
        try:
            if len(operations) > 1 and self._batch_supported is not False:
                results = await self._read_batch(serial_number, operations)
            else:
                for operation in operations:
                    results[operation] = await self._read_single(serial_number, operation)
        except ConfigEntryAuthFailed:
            raise
        except DanalockJobSkipped:
            _LOGGER.debug("Skipped reading %s: a command is waiting for the bridge", serial_number)
        except DanalockApiClientError as err:
            message = err.user_message if isinstance(err, DanalockJobError) else str(err)
            _LOGGER.warning("Could not read lock %s right now: %s", serial_number, message)

        return {
            LOCK_STATE: _parse_state(results.get(OP_GET_STATE)),
            LOCK_BATTERY: _parse_battery(results.get(OP_GET_BATTERY)),
            LOCK_SETTINGS: _parse_settings(results.get(OP_GET_SETTINGS)),
            LOCK_FIRMWARE: _parse_firmware(results.get(OP_GET_DEVICE_INFO)),
        }

    async def get_lock_state(self, serial_number: str) -> str | None:
        """Return 'Locked'/'Unlocked', or None if it could not be determined right now."""
        data = await self.get_lock_data(serial_number, include_battery=False)
        return data[LOCK_STATE]

    async def get_battery_level(self, serial_number: str) -> int | None:
        """Return the battery percentage, or None if unavailable right now."""
        try:
            result = await self._read_single(serial_number, OP_GET_BATTERY)
        except ConfigEntryAuthFailed:
            raise
        except DanalockApiClientError as err:
            _LOGGER.warning("Could not read battery of lock %s: %s", serial_number, err)
            return None
        return _parse_battery(result)

    async def get_latest_firmware(self, serial_number: str) -> str | None:
        """Latest firmware Danalock publishes for this lock (public endpoint, no auth)."""
        try:
            async with self._session.get(
                FIRMWARE_LATEST_URL.format(serial=serial_number),
                headers={"Accept": "application/json", "User-Agent": DEFAULT_USER_AGENT},
                timeout=aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT),
            ) as response:
                if response.status != 200:
                    return None
                body = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            _LOGGER.debug("Latest firmware lookup for %s failed: %s", serial_number, err)
            return None
        version = body.get("version") if isinstance(body, dict) else None
        return version.strip() if isinstance(version, str) and version.strip() else None

    async def _read_single(self, serial_number: str, operation: str) -> dict[str, Any]:
        return await self._execute_and_poll(serial_number, operation)

    async def _read_batch(self, serial_number: str, operations: list[str]) -> dict[str, Any]:
        """Run several read operations in one bridge job, like the official web app does."""
        arguments = [
            {"operation": op, "accept": [{}]} if op == OP_GET_STATE else {"operation": op}
            for op in operations
        ]
        try:
            result = await self._execute_and_poll(serial_number, OP_BATCH, arguments)
            items = result.get("batch_results")
            if not isinstance(items, list):
                raise DanalockJobError(f"batch returned no batch_results: {result!r}")
        except DanalockJobError as err:
            if self._batch_supported or err.transient or isinstance(err, DanalockJobSkipped):
                raise
            # Never worked on this account. Only conclude "unsupported" if single reads do
            # work; if they fail too, the lock/bridge had a problem, not the batch.
            results = {op: await self._read_single(serial_number, op) for op in operations}
            _LOGGER.info("Bridge batch reads unavailable (%s); using single reads", err)
            self._batch_supported = False
            return results

        self._batch_supported = True
        results: dict[str, Any] = {}
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("operation"), str):
                results[item["operation"]] = item.get("result") if isinstance(item.get("result"), dict) else {}
        return results

    # ------------------------------------------------------------------ commands

    async def lock(self, serial_number: str) -> dict[str, Any]:
        """Lock. Returns {state, battery_level}. Raises DanalockApiClientError/ConfigEntryAuthFailed."""
        return await self._operate(serial_number, ARG_LOCK, API_STATE_LOCKED)

    async def unlock(self, serial_number: str) -> dict[str, Any]:
        """Unlock. Returns {state, battery_level}. Raises DanalockApiClientError/ConfigEntryAuthFailed."""
        return await self._operate(serial_number, ARG_UNLOCK, API_STATE_UNLOCKED)

    async def _operate(self, serial_number: str, argument: str, target_state: str) -> dict[str, Any]:
        """Run afi.lock.operate.

        Locking/unlocking is idempotent: re-sending it answers "nothing changed" when the
        first attempt did go through. That makes one retry after a timeout or a dropped
        Bluetooth link safe, and it fixes most "sometimes it just doesn't work" cases.
        """
        for attempt in range(COMMAND_TIMEOUT_RETRIES + 1):
            try:
                result = await self._execute_and_poll(serial_number, OP_LOCK, [argument], command=True)
            except DanalockJobError as err:
                if err.nothing_changed:
                    _LOGGER.debug("Lock %s was already %s", serial_number, target_state)
                    result = err.result
                    break
                retryable = err.bridge_timed_out or err.unreachable
                if retryable and attempt < COMMAND_TIMEOUT_RETRIES:
                    _LOGGER.info("%s of %s failed (%s); retrying once", argument, serial_number, err.user_message)
                    continue
                raise
            else:
                break
        return {LOCK_STATE: target_state, LOCK_BATTERY: _parse_battery(result)}

    # ------------------------------------------------------------------ bridge jobs

    async def _execute_and_poll(
        self,
        serial_number: str,
        operation: str,
        arguments: list[Any] | None = None,
        *,
        command: bool = False,
    ) -> dict[str, Any]:
        """Run one bridge job, serialized per account (a bridge does one thing at a time).

        Commands get priority: a background read that is still queued when a command
        arrives is skipped instead of making the user wait for a whole Bluetooth session.
        """
        if command:
            self._commands_waiting += 1
        waiting = command
        try:
            async with self._bridge_lock:
                if command:
                    self._commands_waiting -= 1
                    waiting = False
                busy_attempts = 0
                loading_retried = False
                while True:
                    if not command and self._commands_waiting:
                        raise DanalockJobSkipped(f"{operation} skipped: a command is waiting")
                    try:
                        return await self._execute_and_poll_once(serial_number, operation, arguments)
                    except DanalockJobError as err:
                        if err.lock_loading and not loading_retried:
                            loading_retried = True
                            delay: float = LOCK_LOADING_RETRY_DELAY
                        elif err.busy and busy_attempts < BRIDGE_BUSY_RETRIES:
                            busy_attempts += 1
                            delay = BRIDGE_BUSY_RETRY_DELAY * busy_attempts
                        else:
                            raise
                        _LOGGER.debug(
                            "Bridge not ready for %s on %s (%s); retrying in %ss",
                            operation, serial_number, err.user_message, delay,
                        )
                        await asyncio.sleep(delay)
        finally:
            if waiting:
                self._commands_waiting -= 1

    async def _execute_and_poll_once(
        self,
        serial_number: str,
        operation: str,
        arguments: list[Any] | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"device": serial_number, "operation": operation}
        if arguments:
            payload["arguments"] = arguments
        started = monotonic()
        record: dict[str, Any] = {"at": round(time()), "operation": operation, "polls": 0}
        if operation == OP_LOCK and arguments:
            record["operation"] = f"{operation} {arguments[0]}"
        try:
            result = await self._run_job(payload, operation, serial_number, started, record)
        except DanalockApiClientError as err:
            record["outcome"] = "failed"
            if isinstance(err, DanalockJobError):
                record.update(
                    bridge_status=err.bridge_status,
                    afi_status=err.afi_status,
                    text=err.text or None,
                    timed_out=err.timed_out,
                )
            else:
                record["text"] = type(err).__name__
            raise
        else:
            record["outcome"] = "ok"
            return result
        finally:
            record["seconds"] = round(monotonic() - started, 2)
            self.job_history.append(record)

    async def _run_job(
        self,
        payload: dict[str, Any],
        operation: str,
        serial_number: str,
        started: float,
        record: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            exec_response = await self._request("POST", EXECUTE_URL, json=payload, headers=JSON_HEADERS)
        except DanalockApiError as err:
            if err.status is not None and err.status >= 500:
                raise
            raise DanalockJobError(f"Bridge rejected {operation}: {err}") from err
        job_id = exec_response.get("id") if isinstance(exec_response, dict) else None
        if not job_id:
            raise DanalockJobError(f"No job id returned for {operation}: {exec_response!r}")

        while True:
            elapsed = monotonic() - started
            if elapsed >= JOB_POLL_TIMEOUT:
                raise DanalockJobError(f"{operation} timed out after {JOB_POLL_TIMEOUT}s", timed_out=True)
            await asyncio.sleep(JOB_POLL_FAST_INTERVAL if elapsed < JOB_POLL_FAST_WINDOW else JOB_POLL_INTERVAL)
            record["polls"] += 1
            try:
                poll = await self._request("POST", POLL_URL, json={"id": job_id}, headers=JSON_HEADERS)
            except DanalockApiError as err:
                if err.status is not None and (err.status >= 500 or err.status == 429):
                    # The job keeps running on the bridge; a hiccup while *asking* about it
                    # must not turn a lock/unlock that happened into a reported failure.
                    record["poll_errors"] = record.get("poll_errors", 0) + 1
                    continue
                raise DanalockJobError(f"Polling {operation} failed: {err}") from err
            except DanalockApiClientError:
                record["poll_errors"] = record.get("poll_errors", 0) + 1
                continue  # network blip: keep asking until the deadline
            if not isinstance(poll, dict):
                raise DanalockJobError(f"Unexpected poll payload for {operation}: {poll!r}")
            status = poll.get("status")
            result = poll.get("result") if isinstance(poll.get("result"), dict) else {}
            if status == JOB_STATUS_SUCCEEDED:
                _LOGGER.debug("%s on %s took %.1fs", operation, serial_number, monotonic() - started)
                return result
            if status == JOB_STATUS_FAILED:
                error = DanalockJobError(f"{operation} failed: {result or 'unknown failure'}", result=result)
                _LOGGER.debug("%s on %s failed after %.1fs: %s", operation, serial_number, monotonic() - started, result)
                raise error
            # "Created", "InProgress" (or anything new) means: keep waiting.
