/*
 * Danalock lock card — shipped with the danalock_cloud integration and loaded automatically.
 *
 *   type: custom:danalock-lock-card
 *   entity: lock.front_door
 *   name: Front door          # optional
 *   confirm_unlock: false     # optional: tap "Unlock" twice to confirm
 *   show_battery: true        # optional
 *
 * Commands go cloud -> Danabridge -> Bluetooth -> lock and take several seconds. The card
 * shows that honestly: a progress ring sized from how long this lock usually takes, the
 * real failure reason when it fails, and how fresh the shown state is.
 */
const CARD_VERSION = "1.4.0";
const ERROR_VISIBLE_MS = 120000;
const CONFIRM_WINDOW_MS = 4000;

const fmtSeconds = (s) => (s < 10 ? s.toFixed(1) : Math.round(s)) + " s";

function fmtAgo(iso) {
  if (!iso) return null;
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`;
  return `${Math.round(seconds / 86400)} d ago`;
}

class DanalockLockCard extends HTMLElement {
  setConfig(config) {
    if (!config || !config.entity || !String(config.entity).startsWith("lock.")) {
      throw new Error("danalock-lock-card: set 'entity' to a lock entity");
    }
    this._config = { show_battery: true, confirm_unlock: false, ...config };
    this._pending = null; // command sent from this card, before HA reports locking/unlocking
    this._localError = null;
    this._confirmUntil = 0;
    this._dismissedKey = null;
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    const stateObj = hass.states[this._config.entity];
    // once HA itself reports the command, the card no longer needs its local "sending" flag
    if (this._pending && stateObj && (["locking", "unlocking"].includes(stateObj.state) || stateObj.attributes.command_in_progress)) {
      this._pending.acknowledged = true;
    }
    // hass is reassigned on every state change in the whole instance: only redraw for ours
    if (stateObj !== this._lastStateObj || !this.shadowRoot.innerHTML) {
      this._lastStateObj = stateObj;
      this._render();
    }
  }

  getCardSize() {
    return 3;
  }

  static getStubConfig(hass) {
    const entity = Object.keys(hass.states).find((id) => id.startsWith("lock.")) || "lock.front_door";
    return { entity };
  }

  connectedCallback() {
    // keep "confirmed 3 min ago" and error fading current between state changes
    if (!this._clock) this._clock = setInterval(() => this._render(), 30000);
    this._render();
  }

  disconnectedCallback() {
    this._stopTicker();
    if (this._clock) {
      clearInterval(this._clock);
      this._clock = null;
    }
  }

  // ------------------------------------------------------------------ view model

  _model() {
    const stateObj = this._hass && this._hass.states[this._config.entity];
    if (!stateObj) return { kind: "missing" };
    const a = stateObj.attributes;
    const now = Date.now();
    let kind = stateObj.state; // locked | unlocked | locking | unlocking | jammed | open | unavailable | unknown
    let command = a.command_in_progress || (kind === "locking" ? "lock" : kind === "unlocking" ? "unlock" : null);
    let started = a.command_started ? new Date(a.command_started).getTime() : null;

    if (!command && this._pending) {
      if (now - this._pending.at > 15000 || this._pending.acknowledged) {
        this._pending = null; // HA finished (or never picked it up); trust HA's state again
      } else {
        command = this._pending.command;
        started = this._pending.at;
      }
    }
    if (command) kind = command === "lock" ? "locking" : "unlocking";

    const expected = Math.max(2, Number(a.expected_duration) || 8);
    const elapsed = started ? Math.max(0, (now - started) / 1000) : 0;

    // One failure can arrive twice (the service-call error and the entity attribute):
    // both are tied to the entity's last_command_at so "dismiss" hides that failure for good.
    const failureKey = a.last_command_result === "failed" ? `${a.last_command}|${a.last_command_at}` : null;
    const failureAge = a.last_command_at ? now - new Date(a.last_command_at).getTime() : Infinity;
    let error = null;
    if (this._localError && now - this._localError.at < ERROR_VISIBLE_MS) {
      error = this._localError.message;
    } else if (!command && failureKey && a.last_command_error && failureAge < ERROR_VISIBLE_MS) {
      error = `Could not ${a.last_command}: ${a.last_command_error}`;
    }
    if (error && failureKey && failureKey === this._dismissedKey) error = null;

    return {
      kind,
      command,
      elapsed,
      expected,
      error,
      failureKey,
      name: this._config.name || a.friendly_name || this._config.entity,
      battery: a.battery_level,
      stateUpdated: a.state_updated,
      stale: a.state_fresh === false,
      lastDuration: a.last_command_duration,
      autoLock: a.auto_lock,
    };
  }

  // ------------------------------------------------------------------ actions

  async _act(command) {
    const m = this._model();
    if (m.command || ["unavailable", "missing"].includes(m.kind)) return;
    if (command === "unlock" && this._config.confirm_unlock && Date.now() > this._confirmUntil) {
      this._confirmUntil = Date.now() + CONFIRM_WINDOW_MS;
      this._render();
      setTimeout(() => this._render(), CONFIRM_WINDOW_MS + 50);
      return;
    }
    this._confirmUntil = 0;
    this._localError = null;
    this._dismissedKey = null;
    this._pending = { command, at: Date.now(), acknowledged: false };
    this._render();
    try {
      await this._hass.callService("lock", command, { entity_id: this._config.entity });
    } catch (err) {
      this._localError = { message: (err && err.message) || String(err), at: Date.now() };
    } finally {
      this._pending = null;
      this._render();
    }
  }

  async _refresh() {
    try {
      await this._hass.callService("danalock_cloud", "refresh_devices", {});
    } catch (err) {
      this._localError = { message: (err && err.message) || String(err), at: Date.now() };
      this._render();
    }
  }

  // ------------------------------------------------------------------ rendering

  _startTicker() {
    if (!this._ticker) this._ticker = setInterval(() => this._render(), 250);
  }

  _stopTicker() {
    if (this._ticker) {
      clearInterval(this._ticker);
      this._ticker = null;
    }
  }

  _render() {
    if (!this.shadowRoot || !this._config) return;
    const m = this._model();
    m.command ? this._startTicker() : this._stopTicker();

    const look = {
      locked: { icon: "mdi:lock", label: "Locked", tone: "ok", action: "unlock" },
      unlocked: { icon: "mdi:lock-open-variant", label: "Unlocked", tone: "warn", action: "lock" },
      open: { icon: "mdi:door-open", label: "Open", tone: "warn", action: "lock" },
      locking: { icon: "mdi:lock-clock", label: "Locking…", tone: "busy", action: null },
      unlocking: { icon: "mdi:lock-clock", label: "Unlocking…", tone: "busy", action: null },
      jammed: { icon: "mdi:lock-alert", label: "Jammed", tone: "bad", action: "lock" },
      unavailable: { icon: "mdi:lock-off", label: "Unavailable", tone: "off", action: null },
      unknown: { icon: "mdi:lock-question", label: "Unknown", tone: "off", action: "lock" },
      missing: { icon: "mdi:lock-off", label: "Entity not found", tone: "off", action: null },
    }[m.kind] || { icon: "mdi:lock-question", label: m.kind, tone: "off", action: null };

    // progress: linear up to the expected time, then crawls towards 95 % instead of lying "done"
    let progress = 0;
    let detail = "";
    if (m.command) {
      progress = m.elapsed <= m.expected
        ? (m.elapsed / m.expected) * 0.8
        : 0.8 + 0.15 * (1 - Math.exp(-(m.elapsed - m.expected) / m.expected));
      detail = m.elapsed <= m.expected * 1.5
        ? `${fmtSeconds(m.elapsed)} · usually ~${fmtSeconds(m.expected)}`
        : `${fmtSeconds(m.elapsed)} · bridge is slow, still trying…`;
    } else if (["unavailable", "unknown", "missing"].includes(m.kind)) {
      const ago = fmtAgo(m.stateUpdated);
      detail = m.kind === "missing" ? "" : ago ? `Can't reach the lock · last confirmed ${ago}` : "Can't reach the lock";
    } else if (m.stale) {
      const ago = fmtAgo(m.stateUpdated);
      detail = ago ? `Couldn't check just now · last confirmed ${ago}` : "Couldn't check the lock just now";
    } else if (m.stateUpdated) {
      detail = `Confirmed ${fmtAgo(m.stateUpdated)}`;
      if (m.lastDuration) detail += ` · last command ${fmtSeconds(m.lastDuration)}`;
    }

    const confirming = this._confirmUntil > Date.now();
    const actionLabel = look.action === "unlock" ? (confirming ? "Tap again to unlock" : "Unlock") : look.action === "lock" ? "Lock" : "";
    const ring = 2 * Math.PI * 52;
    const battery = this._config.show_battery && m.battery != null
      ? `<span class="battery ${m.battery <= 20 ? "low" : ""}"><ha-icon icon="mdi:battery${m.battery >= 95 ? "" : "-" + Math.max(10, Math.round(m.battery / 10) * 10)}"></ha-icon>${m.battery}%</span>`
      : "";
    const jammedExtra = m.kind === "jammed" ? `<button class="secondary" data-act="unlock">Unlock</button>` : "";

    this.shadowRoot.innerHTML = `
      <style>
        :host { --ok: var(--success-color, #43a047); --warn: var(--warning-color, #ffa000); --bad: var(--error-color, #db4437); --busy: var(--info-color, #039be5); --off: var(--disabled-color, #9e9e9e); }
        ha-card { padding: 16px; display: flex; flex-direction: column; gap: 12px; }
        .head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
        .name { font-size: 1.1em; font-weight: 500; color: var(--primary-text-color); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .battery { display: inline-flex; align-items: center; gap: 2px; color: var(--secondary-text-color); font-size: 0.9em; }
        .battery.low { color: var(--bad); }
        .battery ha-icon { --mdc-icon-size: 18px; }
        .body { display: flex; align-items: center; gap: 16px; }
        .dial { position: relative; width: 120px; height: 120px; flex: none; border: none; background: none; padding: 0; cursor: pointer; border-radius: 50%; }
        .dial:disabled { cursor: default; }
        .dial:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 2px; }
        .dial svg { position: absolute; inset: 0; transform: rotate(-90deg); }
        .track { fill: none; stroke: var(--divider-color, rgba(127,127,127,.25)); stroke-width: 6; }
        .bar { fill: none; stroke: var(--${look.tone}); stroke-width: 6; stroke-linecap: round; transition: stroke-dashoffset .25s linear; }
        .face { position: absolute; inset: 10px; border-radius: 50%; display: flex; align-items: center; justify-content: center; background: color-mix(in srgb, var(--${look.tone}) 14%, transparent); }
        .face ha-icon { --mdc-icon-size: 48px; color: var(--${look.tone}); }
        .busy .face ha-icon { animation: pulse 1.2s ease-in-out infinite; }
        @keyframes pulse { 50% { opacity: .45; } }
        .info { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
        .label { font-size: 1.5em; font-weight: 500; color: var(--${look.tone}); }
        .detail { font-size: .85em; color: var(--secondary-text-color); }
        .buttons { display: flex; gap: 8px; flex-wrap: wrap; }
        button.main, button.secondary { font: inherit; border-radius: 18px; padding: 6px 16px; cursor: pointer; border: 1px solid var(--${look.tone}); }
        button.main { background: var(--${look.tone}); color: var(--text-primary-color, #fff); }
        button.main.confirm { background: var(--bad); border-color: var(--bad); }
        button.secondary { background: transparent; color: var(--primary-text-color); border-color: var(--divider-color, #ccc); }
        .error { display: flex; align-items: flex-start; gap: 8px; padding: 8px 10px; border-radius: 8px; background: color-mix(in srgb, var(--bad) 12%, transparent); color: var(--primary-text-color); font-size: .9em; }
        .error ha-icon { color: var(--bad); --mdc-icon-size: 20px; flex: none; }
        .error span { flex: 1; }
        .icon-btn { background: none; border: none; cursor: pointer; color: var(--secondary-text-color); padding: 2px; }
        .icon-btn ha-icon { --mdc-icon-size: 18px; }
      </style>
      <ha-card>
        <div class="head">
          <span class="name">${this._escape(m.name)}</span>
          <span>${battery}<button class="icon-btn" data-refresh title="Check the lock now"><ha-icon icon="mdi:refresh"></ha-icon></button></span>
        </div>
        <div class="body ${m.command ? "busy" : ""}">
          <button class="dial" ${look.action ? `data-act="${look.action}"` : "disabled"} aria-label="${look.label}${actionLabel ? " – " + actionLabel : ""}">
            <svg viewBox="0 0 120 120"><circle class="track" cx="60" cy="60" r="52"/>
              <circle class="bar" cx="60" cy="60" r="52" stroke-dasharray="${ring}" stroke-dashoffset="${m.command ? ring * (1 - progress) : look.tone === "off" ? ring : 0}"/></svg>
            <div class="face"><ha-icon icon="${look.icon}"></ha-icon></div>
          </button>
          <div class="info">
            <span class="label">${look.label}</span>
            ${detail ? `<span class="detail">${this._escape(detail)}</span>` : ""}
            <div class="buttons">
              ${look.action ? `<button class="main ${confirming ? "confirm" : ""}" data-act="${look.action}">${actionLabel}</button>` : ""}
              ${jammedExtra}
            </div>
          </div>
        </div>
        ${m.error ? `<div class="error"><ha-icon icon="mdi:alert-circle"></ha-icon><span>${this._escape(m.error)}</span><button class="icon-btn" data-dismiss title="Dismiss"><ha-icon icon="mdi:close"></ha-icon></button></div>` : ""}
      </ha-card>`;

    this.shadowRoot.querySelectorAll("[data-act]").forEach((el) =>
      el.addEventListener("click", (ev) => {
        ev.stopPropagation();
        this._act(el.getAttribute("data-act"));
      })
    );
    const refresh = this.shadowRoot.querySelector("[data-refresh]");
    if (refresh) refresh.addEventListener("click", () => this._refresh());
    const dismiss = this.shadowRoot.querySelector("[data-dismiss]");
    if (dismiss) dismiss.addEventListener("click", () => {
      this._dismissedKey = m.failureKey;
      this._localError = null;
      this._render();
    });
  }

  _escape(text) {
    return String(text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  }
}

if (!customElements.get("danalock-lock-card")) {
  customElements.define("danalock-lock-card", DanalockLockCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "danalock-lock-card",
    name: "Danalock lock",
    description: "Lock/unlock with live progress, real error messages and state freshness.",
    preview: true,
  });
  console.info(`%c DANALOCK-LOCK-CARD %c ${CARD_VERSION} `, "background:#a2c517;color:#fff", "");
}
