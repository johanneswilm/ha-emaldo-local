# Vendor bridge — Emaldo cloud compatibility without vendor code on the device

> Status: **design / not yet implemented**. This document describes the
> architecture; the running code today is the [takeover stack](../README.md)
> (stock firmware, server-in-the-middle). The bridge is the next step: it
> provides the same cloud compatibility while the battery runs the clean,
> vendor-free [`emaldo-local`](https://github.com/johanneswilm/emaldo-local)
> firmware.

## The problem it solves

`emaldo-local` (our replacement firmware) deliberately speaks *nothing* of
the vendor's protocols. That is a feature — nothing Emaldo-shaped on the
device to audit, maintain, or be surprised by — but it makes the battery
invisible to Emaldo's cloud. Invisible means: no `iot-online` beacon, no
grid-rewards eligibility, no view in the vendor app.

Users with a grid-rewards contract (especially *flexible* ones, where
participation is voluntary and absence is no breach) may want to keep that
eligibility while running local firmware — at least until the vendor's
bankruptcy/payout picture is clear.

## Architecture

```
                        ┌──────────────────────────────┐
 Emaldo cloud           │           Synology           │
 (dp/api.emaldo.com,    │                              │
  e2e relay, MQTT) ←──→ │  vendor bridge (two-headed)  │ ←LAN→ battery
                        │                              │        (emaldo-local,
                        │  head 1: device emulation    │         REST/MQTT)
                        │  head 2: dispatch translator │
                        └──────────────────────────────┘
```

- **Battery side**: `emaldo-local` talks only its own API (REST + MQTT)
  over the LAN. No DNS spoofing, no router port-forward, no vendor keys on
  the device.
- **Head 1 — device emulation**: the bridge *is* the battery as far as
  Emaldo's cloud is concerned. It generates the module's entire vendor
  traffic from its own protocol implementation: the gm2 login family
  (`reg-token-ack`, `iot-online`, `update-eid`, `get-base-info`,
  `submit-sync-24h`, price polls, …), the e2e relay session
  (chat-rotated keys, alive/heartbeat cadence), and the vendor MQTT
  connection using the module's broker credentials. All of these protocols
  are already implemented in the takeover stack (`fake_api.py`,
  `e2e_relay.py`); the delta is *generate instead of forward*.
- **Head 2 — dispatch translation**: vendor commands become emaldo-local
  API calls, and emaldo-local state becomes vendor telemetry.
  - Vendor → device: MQTT fcr set-schedule topics, e2e `msct` settings,
    96-slot overrides (formats decoded in [PROTOCOL.md](PROTOCOL.md) §8 and
    [APP_COMMANDS.md](APP_COMMANDS.md)) → charge/discharge schedule and
    mode writes via the emaldo-local REST API.
  - Device → vendor: SoC, module power/temperatures, inverter state from
  the emaldo-local telemetry feed → the vendor protocol's answer fields.

## Why this shape (and not emulation on the device)

1. **The firmware stays 100 % free and simple.** Protocol emulation is the
   fragile, fast-moving part; it belongs in inspectable Python on a box
   you control, not in firmware that must be flashed to fix.
2. **Iteration speed.** When Emaldo's server changes something, fix the
   bridge — no reflash, no risk.
3. **Shared with the takeover stack.** The bridge and the current takeover
   stack are the same code path with a different device backend ("forward
   to stock firmware" vs "translate to emaldo-local API").
4. **Resilience.** If the Synology is down, the battery keeps executing
   its on-device schedules autonomously; Emaldo merely sees the unit
   offline (with a flexible contract, harmless).

## Fidelity requirements (the hard part)

Emaldo's server must see a *plausible device*. The bridge must reproduce:

- **Cadence**: alive/heartbeat intervals, `get_price_setting` polls,
  beacon spacing — wrong timing is as detectable as wrong crypto.
- **Consistency**: telemetry values must be self-consistent (SoC, stored
  energy, module currents); version strings must match a real build.
- **Chat rotation**: the e2e chat secret rotation schedule must be
  maintained exactly as the module would.

**Method**: record several weeks of the battery's genuine traffic in
takeover mode as a behavioral template (the corpus capture already does
this), then generate synthetic traffic matching that shape, and keep the
bridge's output diffable against the template in CI.

## What this does *not* solve

- **Payout solvency.** The bridge preserves *eligibility* for grid
  rewards; whether Emaldo (in bankruptcy) actually pays is out of scope.
- **Guaranteed-contract obligations.** A guaranteed-rewards contract may
  require dispatch response times a sleeping bridge/battery cannot meet.
  With a flexible contract the bridge can simply mark the unit unavailable
  for dispatches it does not want.

## MQTT / FCR status (reverse-engineered 2026-11)

The cred handout is fully decoded: the module POSTs to `{mqtt_broker_router_url}/auth/broker_url/`, the server answers `{"token", "expired_at", "broker_url"}` (gm2 envelope), and the module's parser reads only `broker_url`. **But the MQTT client only starts on units enrolled in FCR/mFRR balancing programs** — verified three ways: the real `get_master_config_area` contains no enable flags (only area metadata), the vendor app channel has sent zero FCR/rewards frames to a flexible-rewards unit across months of capture, and the polling cadence matches a credential-refresh loop, not a connect attempt. Flexible-rewards dispatches travel the e2e/AI-plan channel instead — which the bridge already emulates. The fcr/mfrr schedule payload formats remain uncaptured until an FCR-enrolled device appears; the opcode/state vocabulary is in [PROTOCOL.md](PROTOCOL.md) §7.4.

## Relationship to other components

- [`emaldo-local` IMPLEMENTATION_MAP](https://github.com/johanneswilm/emaldo-local/blob/main/docs/IMPLEMENTATION_MAP.md)
  FW-3 "e2e-compat bridge" — the *on-device* variant of cloud compat. The
  vendor bridge here is the preferred alternative: same compatibility,
  none of the vendor code on the device.
- [CUSTOM_FIRMWARE.md](CUSTOM_FIRMWARE.md) — firmware install paths and
  rollback via the update channel.
