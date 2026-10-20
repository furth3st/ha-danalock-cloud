# Danalock Cloud Integration for Home Assistant

[![HACS Custom][hacs-shield]][hacs-url]
[![GitHub Release][releases-shield]][releases-url]
[![License][license-shield]][license-url]

<!-- Assumes you'll add 'danalock_logo.png' to an 'images' folder in your repo root -->
<p align="center">
  <img src="images/danalock_logo.png" alt="Danalock Logo" width="150"/>
</p>

This is a custom integration for Home Assistant to control [Danalock V3](https://danalock.com/) smart locks via the Danalock Cloud API and the Danabridge V3. It provides native `lock` and `sensor` (battery) entities, offering a more integrated experience than relying on complex Node-RED flows or REST commands for basic cloud control.

**Important Note:** This integration communicates with your Danalock devices through the Danalock Cloud. This means it requires an active internet connection for both Home Assistant and your Danabridge. Cloud communication inherently involves some latency.

## Acknowledgements

This integration was inspired by and utilizes understanding of the Danalock API derived from the work done by **[@erikwihlborg76](https://github.com/erikwihlborg76)** on the [unofficial Danalock Node-RED flow](https://github.com/erikwihlborg76/unofficial-danalock-web-api). Many thanks for the foundational work in understanding the API interactions!

## Features

*   **Lock Control:** Lock and unlock your Danalock devices, with `locking`/`unlocking` shown while the command runs and `jammed` when the bolt is blocked.
*   **Lock Card:** A dashboard card made for this lock (see below), loaded automatically.
*   **State Reporting:** Reports the current lock state (Locked/Unlocked) polled from the cloud.
*   **Battery Level:** Provides a sensor entity for the lock's battery percentage (also refreshed by every lock/unlock).
*   **Firmware:** The installed firmware is shown on the device page, and an update entity tells you when Danalock publishes a newer one (install it with the Danalock app).
*   **Cloud Polling:** Periodically fetches status updates from the Danalock Cloud. The interval is configurable.
*   **Optional Optimistic State:** Choose to have the UI update instantly after sending a command (see Options section).
*   **UI Configuration:** Easy setup and configuration directly through the Home Assistant user interface.
*   **Automatic Re-authentication:** Attempts to automatically re-authenticate if tokens expire, minimizing user intervention.
*   **Diagnostics:** Provides diagnostic information via the Home Assistant UI to aid in troubleshooting.
*   **Refresh Service:** Allows manual triggering of data updates for all locks via a Home Assistant service call.

## Important Requirements & Limitations

*   **Danabridge V3 Required:** This integration **requires** a Danabridge V3 connected to your lock(s) and your local network. It does not communicate directly with the lock via Bluetooth.
*   **Two-Factor Authentication (2FA) is supported:** If your Danalock account uses 2FA, enter the current 6-digit code in the optional *2FA code* field during setup. The code is only used once to sign in and is never stored; after that the integration stays signed in with refresh tokens. If Danalock ever forces a fresh sign-in, Home Assistant will ask for your password (and a new code) again.
*   **Owner Account Highly Recommended:** It is strongly recommended (and likely required) to use the Danalock account that **owns and manages** the lock and bridge. Accounts with only shared access may lack the necessary API permissions for this integration to function correctly.
*   **Cloud Dependency & Latency:** All communication goes through the Danalock cloud, meaning an internet connection is essential. Operations may experience delays typical of cloud services. (See "Notice on State Updates and Delays" below).

## Notice on State Updates and Delays

Due to the nature of cloud-based control and the Danalock API:

*   **Command Execution Delay:** When you send a lock or unlock command from Home Assistant, the command goes to the Danalock Cloud, then to your Danabridge, and finally to the lock via Bluetooth. This takes a few seconds; most of it is the bridge's Bluetooth connection to the lock. The integration checks for the result every 0.5 s (like the official web app), so it reports success as soon as the lock is done.
*   **State Confirmation:** A successful lock/unlock *is* the confirmation (the lock reports the result and its battery level), so no extra read is made. If the lock has auto-lock enabled, it is read again a few seconds after the auto-lock should have happened.
*   **Polling Interval:** The lock state is polled every 5 minutes by default (configurable). Battery, settings and firmware are read less often and in the same bridge session as the state, since every Bluetooth session occupies the bridge.
*   **Failed Commands Are Reported:** If a lock/unlock command fails (bridge offline, lock jammed, ...), the action raises an error in Home Assistant with the reason and fires `danalock_cloud_command_failure`, so automations and notifications can react. A door lock should never fail silently.
*   **Automatic Retries Where Safe:** "Already locked/unlocked" counts as success. A command that times out on the bridge or loses the Bluetooth link is retried once (safe: repeating it only answers "nothing changed"). "Lock is loading" is waited out (10 s).
*   **One Bridge Operation at a Time:** State polls and commands for an account are queued instead of colliding, a short retry is made when the bridge reports `BridgeBusy`, and your commands always go before queued background polls.
*   **"Bridge Busy" is Expected Behavior:** The Danalock API can only handle **one operation at a time** per bridge. If you send a command and then immediately try to poll for the state, the bridge will report "Busy".
    *   The lock state in Home Assistant will **retain its last known valid state** and will *not* become "unknown" due to a transient `BridgeBusy` error.
    *   The actual state will be updated once a subsequent poll is successful.
*   **Optimistic Mode:** To provide faster UI feedback, you can enable "Optimistic State Updates" in the integration options. See the "Options" section for more details.

## Installation

### Prerequisites

*   A working Home Assistant installation.
*   [HACS (Home Assistant Community Store)](https://hacs.xyz/) installed and configured.
*   Your Danalock account username (email) and password.
*   A Danalock V3 lock correctly paired with a Danabridge V3 that is connected to your network.
*   Home Assistant **2024.12 or newer**.

### Installation via HACS

1.  **Add Custom Repository:**
    *   In HACS, go to "Integrations".
    *   Click the three dots (⋮) in the top right corner and select "Custom repositories".
    *   Enter the URL of this repository: `https://github.com/furth3st/ha-danalock-cloud`
    *   Select `Integration` as the category.
    *   Click "Add".
2.  **Install Integration:**
    *   Close the custom repositories dialog.
    *   The "Danalock Cloud" integration should now appear in your HACS integrations list (you might need to search for it).
    *   Click "Install" and follow the prompts.
3.  **Restart Home Assistant:** After HACS completes the installation, restart Home Assistant as prompted.

## Configuration

1.  After restarting Home Assistant, navigate to **Settings** -> **Devices & Services**.
2.  Click the **+ ADD INTEGRATION** button in the bottom right corner.
3.  Search for "Danalock Cloud" and select it.
4.  You will be prompted to enter your Danalock account **Username** (email) and **Password**.
    *   Use the account that **owns** the lock and bridge.
    *   If the account has two-factor authentication, also fill in the **2FA code** field (leave it empty otherwise).
5.  Click **Submit**.

The integration will attempt to authenticate with the Danalock Cloud, discover your locks associated with the bridge, and create the corresponding `lock` and `sensor` (for battery) entities in Home Assistant.

## Options

After setup, you can configure the integration's behavior:

1.  Go to **Settings** -> **Devices & Services**.
2.  Find the "Danalock Cloud" integration card.
3.  Click **CONFIGURE**.
4.  Adjust the available options:
    *   **Polling Interval (minutes):** The default is 5 minutes. Higher values are gentler on the API and recommended if you see frequent `BridgeBusy` errors.
    *   **Use Optimistic State Updates:**
        *   **Disabled (Default):** The lock's state only changes after the API confirms it. This is more accurate but has a delay.
        *   **Enabled:** The lock's state changes instantly in the UI when you send a command. If the command fails, the state is rolled back immediately.
5.  Click **Submit**.

## Lock Card

The integration ships a dashboard card built around how this lock behaves. It is loaded automatically (no resource to add); after updating, refresh the browser once.

```yaml
type: custom:danalock-lock-card
entity: lock.front_door
# name: Front door        # optional
# confirm_unlock: true    # optional: tap "Unlock" twice
# show_battery: false     # optional
```

*   Tap the dial or the button to lock/unlock. While the bridge works, a progress ring fills based on how long *your* lock usually takes, with a running timer ("bridge is slow, still trying…" when it takes unusually long).
*   If a command fails, the card shows the real reason (jammed, bridge busy, timed out, bridge offline, critical battery) instead of silently flipping back.
*   It shows how fresh the state is ("Confirmed 2 min ago", or "Couldn't check just now · last confirmed …" when the last poll failed).
*   Battery level and a "check now" button are in the header.

The card is also in the dashboard card picker as **Danalock lock**. The same information is available to your own cards and templates as attributes of the lock entity: `command_in_progress`, `command_started`, `expected_duration`, `last_command`, `last_command_result`, `last_command_error`, `last_command_duration`, `state_updated`, `state_fresh`, `battery_level`, `auto_lock`.

## Services

This integration provides the following service:

*   **`danalock_cloud.refresh_devices`**:
    *   **Description:** Forces an immediate refresh of all Danalock device states and battery levels from the cloud API for all configured Danalock Cloud accounts.
    *   **Use Cases:** Useful in automations after a lock/unlock command if you want to try and get an updated state sooner than the next scheduled poll, or for debugging if you suspect the state is stale.
    *   **Example Service Call in Automation:**

        action:
          - service: danalock_cloud.refresh_devices

## Troubleshooting

*   **Authentication Failed / Invalid Auth** (`invalid_grant: The user credentials were incorrect.`):
    *   This message comes from Danalock's own login server: it did not accept the e-mail/password combination.
    *   Check for typos and stray spaces (common when pasting on mobile).
    *   If the account has two-factor authentication, enter the current 2FA code.
    *   Accounts created through **Google/Apple sign-in** may not have a Danalock password; set one at my.danalock.com first.
    *   Use the account that *owns* the lock and bridge.
    *   Temporary Danalock outages never discard your saved login; the integration simply retries. You are only asked to re-authenticate when Danalock explicitly rejects the saved password.

*   **Lock State Not Updating / `BridgeBusy` Errors in Logs:**
    *   Refer to the "Notice on State Updates and Delays" section above. Occasional `BridgeBusy` is expected (for example when the Danalock app uses the bridge at the same time) and is retried automatically.
    *   **Try Increasing Polling Interval:** If it happens a lot, go to the integration options and increase the polling interval (e.g., to 15, 30, or 60 minutes).
    *   **Restart Danabridge:** Power cycle your physical Danabridge V3 device.

*   **Commands Slow or Occasionally Failing:**
    *   Download diagnostics (see below) after a few lock/unlocks: the `bridge.recent_jobs` section lists every bridge job with its duration, number of polls and the exact error code. Long durations with success point at the Bluetooth link between bridge and lock: move the Danabridge closer to the lock (same room, line of sight, away from metal) and make sure it has a strong 2.4 GHz Wi-Fi signal.
    *   For instant local control, a Bluetooth adapter or an ESPHome Bluetooth proxy near the door plus the community [Danalock Bluetooth](https://github.com/buggy-shep/danalock-ble-ha-integration) integration avoids the cloud and bridge entirely.

*   **"No locks found":**
    *   Confirm the Danalock account used is the owner of the locks and the bridge.
    *   Ensure the Danabridge is online and correctly paired with your locks in the Danalock app.

*   **Diagnostics:**
    If you encounter persistent issues, download diagnostic information to help troubleshoot:
    1.  Go to **Settings** -> **Devices & Services**.
    2.  Find the Danalock Cloud integration card.
    3.  Click the three dots (...) menu on the card.
    4.  Select **Download diagnostics**.
    This will download a text file. Please review it for any obvious errors and attach it (after redacting any sensitive information you don't want to share) when reporting an issue.

## Contributing

Contributions are welcome! Please feel free to open an issue or submit a pull request.

## Disclaimers

This is an unofficial, community-developed integration. Danalock® and the Danalock logo are trademarks of Danalock International ApS. This project is not affiliated with, endorsed by, or sponsored by Danalock International ApS. The logo is used here for identification purposes only, to indicate the product this integration is designed to work with.

## License

This project is licensed under the Apache License 2.0 - see the [LICENSE](LICENSE) file for details.

---
**Disclaimer:**

This is an unofficial, community-developed integration. Danalock® and the Danalock logo are trademarks of Danalock International ApS. This project is not affiliated with, endorsed by, or sponsored by Danalock International ApS. The logo is used here for identification purposes only, to indicate the product this integration is designed to work with.


<!-- Badges -->
[hacs-shield]: https://img.shields.io/badge/HACS-Custom-41BDF5.svg
[hacs-url]: https://hacs.xyz/
[releases-shield]: https://img.shields.io/github/v/release/furth3st/ha-danalock-cloud.svg?style=flat-square
[releases-url]: https://github.com/furth3st/ha-danalock-cloud/releases
[license-shield]: https://img.shields.io/github/license/furth3st/ha-danalock-cloud.svg?style=flat-square
[license-url]: https://github.com/furth3st/ha-danalock-cloud/blob/main/LICENSE
