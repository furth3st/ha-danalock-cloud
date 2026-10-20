# Changelog

## 1.4.0

Responsiveness and reliability release. Bridge behaviour is now modelled on the official my.danalock.com web app.

### Faster
- Bridge jobs are checked every 0.5 s (was 2 s) while fresh, then back off. Saves up to ~2 s per command.
- State, battery, settings and firmware are read in one `batch` bridge job (one Bluetooth session instead of up to four); falls back to single reads automatically if the bridge doesn't support it.
- A successful lock/unlock is used as the confirmation (it returns the battery level too) instead of occupying the bridge with a re-read 15 s later.
- Lock/unlock commands go before queued background polls.

### More reliable
- "Already locked/unlocked" (AFI status 7) is a success, not an error.
- A command that the bridge times out on, or that loses the bridge -> lock Bluetooth link, is retried once (lock/unlock is idempotent).
- Bridge status 4 ("lock is loading") is waited out for 10 s instead of failing.
- Failed commands carry a clear reason (jammed, bridge busy, timed out, bridge offline, critical battery).
- A network blip or Danalock 5xx while waiting for a command's result no longer reports a failure (and rolls back the state) when the lock did move; polling continues until the deadline.
- Two commands on the same lock at once (e.g. an automation and a tap) now run one after the other instead of clearing each other's progress state.
- A rotated refresh token is saved immediately, so a restart can't leave an already-revoked token behind.
- Device lookup no longer uses `device_registry.async_get_device(identifiers=...)`, deprecated in HA 2026.x and removed in 2027.8.

### Added
- `danalock-lock-card`: a dashboard card with live progress, real error messages and state freshness. Served and registered automatically.
- Lock state `jammed`, and attributes for dashboards: `command_in_progress`, `command_started`, `expected_duration`, `last_command*`, `state_updated`, `state_fresh`, `battery_level`, `auto_lock`.
- Firmware version on the device page and a read-only firmware update entity (latest version from Danalock's public firmware service).
- Auto-lock aware re-check: after an unlock, the lock is read again shortly after its auto-lock time.
- "Refresh devices" also re-reads battery, settings and firmware; while a firmware update is pending the version is re-checked hourly, so "update available" clears soon after installing it in the app.
- Diagnostics include the last 25 bridge jobs (operation, duration, polls, error codes) and per-lock settings/firmware.

## 1.3.0

Stability release for current Home Assistant versions. Requires Home Assistant 2024.12 or newer.

### Fixed
- Setting up the integration failed with "Unknown error occurred" / `KeyError: 'token_expires_at'` (#11, #10 — thanks @pengelbrecht).
- Opening the integration's options (gear icon) failed with "500 Internal Server Error" on Home Assistant 2025.12+ (#8, #10).
- Re-authentication crashed instead of showing the password prompt.
- A temporary Danalock outage during a token refresh permanently deleted the saved password, forcing a manual re-login. Outages now never touch saved credentials; the integration retries instead.
- An expired/revoked access token went straight to "re-authenticate"; it is now refreshed and the request retried once.
- Several locks on one bridge were polled in parallel, which the bridge rejects (`BridgeBusy`), leaving states unknown. Bridge operations are now queued per account, with a short retry on `BridgeBusy`.
- A failed lock/unlock left an optimistic (wrong) state and failed silently. Failed commands now raise an error in Home Assistant (so automations/notifications can react) and roll the state back.
- The lock state no longer flips to unknown just because one poll hit a busy bridge; the last known state is kept.
- The refresh service is registered once and no longer removed incorrectly when an entry unloads.
- UI texts were missing for custom-integration installs (added `translations/en.json`).
- "Download diagnostics" crashed (it read a coordinator attribute that doesn't exist).

### Added
- Two-factor authentication support: enter the current code in the optional *2FA code* field. The code is used once and never stored.
- Local brand assets for Home Assistant 2026.3+ (#13 — thanks @frenck).
- Automated tests against Home Assistant, plus weekly CI against the newest Home Assistant release.

### Changed
- Battery level is read about once an hour instead of every poll (spares the bridge).
- Usernames are trimmed of stray whitespace.
- Locks on the account that aren't Danalock V3 devices (e.g. the newer `mwm` type) are skipped with a log warning explaining why, instead of disappearing silently.
- Clearer login error text (stray spaces, 2FA, Google/Apple sign-in accounts).
- Removed the `aiohttp` requirement from the manifest (Home Assistant already ships it).
