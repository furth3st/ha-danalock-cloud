"""Shared fixtures: a fake Danalock cloud + bridge served through aioclient_mock."""
from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from custom_components.danalock_cloud import api as api_module
from custom_components.danalock_cloud.const import (
    ACCESS_TOKEN,
    CONF_PASSWORD,
    CONF_USERNAME,
    DOMAIN,
    EXECUTE_URL,
    FIRMWARE_LATEST_URL,
    LOCKS_URL,
    POLL_URL,
    REFRESH_TOKEN,
    TOKEN_EXPIRES_AT,
    TOKEN_URL,
)

USERNAME = "owner@example.com"
PASSWORD = "correct horse"
SERIAL = "AA:BB:CC:DD:EE:FF"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load the integration from custom_components/."""
    yield


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch):
    """No real waiting between bridge polls / busy retries in tests."""
    monkeypatch.setattr(api_module, "JOB_POLL_INTERVAL", 0)
    monkeypatch.setattr(api_module, "JOB_POLL_FAST_INTERVAL", 0)
    monkeypatch.setattr(api_module, "BRIDGE_BUSY_RETRY_DELAY", 0)
    monkeypatch.setattr(api_module, "LOCK_LOADING_RETRY_DELAY", 0)


class FakeDanalock:
    """Minimal behavioural model of api.danalock.com + the Danabridge job service."""

    def __init__(self) -> None:
        self.password = PASSWORD
        self.require_totp: str | None = None
        self.token_outage = False  # token endpoint answers 503
        self.refresh_valid = True
        self.locks_status = 200
        self.reject_access_token_once = False
        self.expires_in: Any = 3600
        self.state = "Locked"
        self.battery = 87
        self.busy_polls = 0  # next N jobs fail with BridgeBusy
        self.operate_fails = False
        self.jam = False  # operate fails with AFI status 0x17 (blocked)
        self.operate_timeouts = 0  # next N operates fail with the bridge's JobTimeout
        self.lock_loading_once = False  # next job fails with bridge status 4
        self.in_progress_polls = 1  # polls answering "InProgress" before each job finishes
        self.stuck = False  # jobs never finish
        self.batch_supported = True
        self.poll_errors = 0  # next N polls answer HTTP 502
        self.reads_fail = False  # every read job fails with an unknown lock-side error
        self.auto_lock = 0
        self.firmware = "0.25.0"
        self.token_forms: list[dict[str, str]] = []
        self.calls: list[str] = []
        self._jobs: dict[str, dict[str, Any]] = {}
        self._issued = 0

    # -- helpers
    def _resp(self, method, url, status=200, json=None):
        return AiohttpClientMockResponse(method, url, status=status, json=json)

    def _new_tokens(self) -> dict[str, Any]:
        self._issued += 1
        return {
            "access_token": f"access-{self._issued}",
            "refresh_token": f"refresh-{self._issued}",
            "token_type": "Bearer",
            "expires_in": self.expires_in,
        }

    # -- endpoints
    async def token(self, method, url, data):
        form = dict(parse_qsl(data)) if isinstance(data, (str, bytes)) else dict(data or {})
        form = {k: (v.decode() if isinstance(v, bytes) else v) for k, v in form.items()}
        self.token_forms.append(form)
        self.calls.append(f"token:{form.get('grant_type')}")
        if self.token_outage:
            return self._resp(method, url, 503, {"error": "service_unavailable"})
        if form.get("grant_type") == "refresh_token":
            if not self.refresh_valid:
                return self._resp(method, url, 400, {"error": "invalid_grant", "error_description": "Invalid refresh token"})
            return self._resp(method, url, 200, self._new_tokens())
        ok = form.get("username") == USERNAME and form.get("password") == self.password
        if ok and self.require_totp is not None and form.get("totp") != self.require_totp:
            ok = False
        if not ok:
            return self._resp(method, url, 400, {"error": "invalid_grant", "error_description": "The user credentials were incorrect."})
        return self._resp(method, url, 200, self._new_tokens())

    async def locks(self, method, url, data):
        self.calls.append("locks")
        if self.reject_access_token_once:
            self.reject_access_token_once = False
            return self._resp(method, url, 401, {"error": "invalid_token"})
        if self.locks_status != 200:
            return self._resp(method, url, self.locks_status, {"error": "boom"})
        return self._resp(
            method,
            url,
            200,
            [
                {"id": "lock-1", "name": "Front door", "type": "afi", "afi": {"serial_number": SERIAL, "device_type": "danalockv3"}},
                # a non-V3 lock on the same account: must be skipped, not crash setup
                {"id": "lock-2", "name": "Garage", "type": "mwm", "mwm": {"mac_address": "11:22:33:44:55:66"}},
            ],
        )

    async def execute(self, method, url, data):
        payload = data if isinstance(data, dict) else {}
        self.calls.append(f"execute:{payload.get('operation')}")
        job = f"job-{len(self._jobs) + 1}"
        self._jobs[job] = {**payload, "_polls": 0}
        return self._resp(method, url, 200, {"id": job})

    def _run(self, op, arguments) -> tuple[str, dict[str, Any]]:
        """Execute one AFI operation on the fake lock: (job status, result)."""
        if self.reads_fail and op != "afi.lock.operate":
            return "Failed", {"bridge_server_status_text": "SomethingOdd"}
        if op == "afi.lock.get-state":
            return "Succeeded", {"state": self.state}
        if op == "afi.power-source.get-information2":
            return "Succeeded", {"battery_level": self.battery}
        if op == "afi.lock.get-settings":
            return "Succeeded", {"auto_lock": self.auto_lock, "hold_back_latch": 0, "end_to_end_mode": 0}
        if op == "afi.device.get-information":
            return "Succeeded", {"firmware_version": self.firmware}
        if op == "afi.lock.operate":
            if self.jam:
                return "Failed", {"bridge_server_status": 0, "afi_status": 0x17, "afi_status_text": "Blocked"}
            if self.operate_fails:
                return "Failed", {"bridge_server_status_text": "Something broke"}
            if self.operate_timeouts:
                self.operate_timeouts -= 1
                return "Failed", {"afi_status_text": "JobTimeout"}
            target = "Locked" if arguments == ["lock"] else "Unlocked"
            if self.state == target:
                return "Failed", {"afi_status": 7, "afi_status_text": "NothingChanged", "battery_level": self.battery}
            self.state = target
            return "Succeeded", {"afi_status": 0, "battery_level": self.battery}
        if op == "batch" and self.batch_supported:
            results = []
            for item in arguments or []:
                _, result = self._run(item.get("operation"), item.get("arguments"))
                results.append({"operation": item.get("operation"), "result": result})
            return "Succeeded", {"batch_results": results}
        return "Failed", {"bridge_server_status_text": "UnknownOperation"}

    async def poll(self, method, url, data):
        job = (data or {}).get("id")
        payload = self._jobs.get(job, {})
        if self.poll_errors:
            self.poll_errors -= 1
            return self._resp(method, url, 502, {"error": "bad gateway"})
        payload["_polls"] = payload.get("_polls", 0) + 1
        if self.stuck or payload["_polls"] <= self.in_progress_polls:
            return self._resp(method, url, 200, {"status": "InProgress", "result": None})
        if self.lock_loading_once:
            self.lock_loading_once = False
            return self._resp(method, url, 200, {"status": "Failed", "result": {"bridge_server_status": 4}})
        if self.busy_polls:
            self.busy_polls -= 1
            return self._resp(method, url, 200, {"status": "Failed", "result": {"bridge_server_status_text": "BridgeBusy"}})
        status, result = self._run(payload.get("operation"), payload.get("arguments"))
        return self._resp(method, url, 200, {"status": status, "result": result})

    async def latest_firmware(self, method, url, data):
        return self._resp(method, url, 200, {"version": "0.26.0"})

    def register(self, aioclient_mock: AiohttpClientMocker) -> None:
        aioclient_mock.post(TOKEN_URL, side_effect=self.token)
        aioclient_mock.get(LOCKS_URL, side_effect=self.locks)
        aioclient_mock.post(EXECUTE_URL, side_effect=self.execute)
        aioclient_mock.post(POLL_URL, side_effect=self.poll)
        aioclient_mock.get(FIRMWARE_LATEST_URL.format(serial=SERIAL), side_effect=self.latest_firmware)


@pytest.fixture
def danalock(aioclient_mock: AiohttpClientMocker) -> FakeDanalock:
    """A fake Danalock backend wired into the HA aiohttp session."""
    fake = FakeDanalock()
    fake.register(aioclient_mock)
    return fake


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """An existing entry with tokens that are already expired (forces a refresh on use)."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=USERNAME,
        unique_id=USERNAME.lower(),
        data={
            CONF_USERNAME: USERNAME,
            CONF_PASSWORD: PASSWORD,
            ACCESS_TOKEN: "old-access",
            REFRESH_TOKEN: "old-refresh",
            TOKEN_EXPIRES_AT: 0,
        },
    )
