# App command channel (msct) — transport, opcodes, quirks

Recovered from the official Android app (`com.hps.emaldo`, decompiled with
jadx) and live captures against a Power Core 2.0 (2026-11). This is the
channel the app uses for device commands that are NOT the plain e2e
subscribe/read family documented in `docs/PROTOCOL.md` §5-§11.

## 1. Transport

App commands are **msct** messages
(`com.dinsafer.dssupport.msctlib`):

- Over the cloud: msct over **KCP/UDP** to the e2e relay host:port
  (`MultipleSender(e2eDomain, port)`), with an AES converter keyed by the
  e2e `chat_secret`. A session-establishment exchange (msct request with
  `OPTION_STATUS` in the response) precedes the KCP channel.
- Direct/P2P: `P2PSender("", 8888)` exists in the app; the port was closed
  on our device in normal operation (role unconfirmed — likely a
  provisioning/local fallback).
- The **module** side logs these as numeric commands, e.g. its string
  table contains `recv 0xa103 cmd get all support features` — proof the
  module accepts this family on its e2e-facing side.

### msct/e2e frame option vocabulary (captured live)

Frames observed on the wire (relay → device session; same tag style as the
e2e envelope):

| bytes | meaning |
|-------|---------|
| `a0 a0` | sender end_id (32 chars) |
| `a0 a1` | group_id (32-33 chars, `@0` suffixed) |
| `a0 a2` | receiver end_id (32 chars) |
| `90 a3` | AES nonce (16 chars) |
| `86 f5` + ASCII | method as ASCII name (observed: `onotice` from the vendor server) |
| `8c f6` + ASCII | msgid (observed: `srvr_<digits>` server-side ids, `and_<...>` app ids) |
| `86 a0` + len + ASCII | sender name (observed: `SERVER`) |
| `10 b7` + `application/byte` + CT | encrypted payload (AES-CBC, chat_secret) |

## 2. Command table (app → module)

Decompiled from the app's command dispatcher (`defpackage/ti5.java` in
jadx output). The APK builds each command with an `OPTION_METHOD` short;
as a signed 16-bit value, `-N` maps to wire opcode `0x10000 - N`:

| command name | APK short | wire opcode | payload | response payload |
|---|---|---|---|---|
| `get_chips_status` | -24559 | `0xA011` | empty | bytes: `[0]=error, [1]=status` (drives "update available" UI) |
| `get_chips_update_progress` | -24558 | `0xA012` | empty | `{failed, progress, error[]}` |
| `update_chips` | -24557 | `0xA103` | empty | bytes: `[0]=error, [1]=status` |
| `get_advance_info` | -24560 | `0xA010` | empty | `{version, hardware_version, sidecar_sn, ...}` |
| `get_firmwares` | -24521 | `0xA037` | empty | list |
| `get_fuse_specs` | -24519 | `0xA039` | id string | — |
| `reset` | -24568 | `0xA008` | empty | — |

Additional opcodes present in the dispatcher (names to be confirmed):
`0xA007`, `0xA009`, `0xA010`-family neighbours, `0xA038`, `0xA024`,
`0xA100`, `0xA101`, `0xA102`, `0xA10A`-`0xA121` range, `0xA113`,
`0xA114`, `0xA115`, `0xA117`, `0xA119`, `0xA11C`, `0xA11E`, `0xA15F`.
The `0xA11x`-`0xA12x` range largely maps to heat-pump (`EGCmd.*`) commands
for other devices in the vendor family — irrelevant for the Power Core but
useful if you own one of those.

Response framing: msct `MsctResponse` with `OPTION_STATUS` (0 = ok) and
the payload above; timeouts/`-2`/`-999` status codes drive the app's
retry-reconnect logic (`C(str)` → 3 tries → reconnect).

## 3. App behavior quirks (observed live, 2026-11)

- **"Up to date" comes from the cloud.** The firmware screen's verdict is
  a vendor-cloud comparison; a locally-staged OTA package (see
  `docs/OTA.md`) is invisible to it. The app can therefore never trigger a
  local/staged update.
- **`/bmt/e2e-user-login/` rotates the battery address id on every
  call** (8/8 calls observed, same account token; `chat_secret` stayed
  constant). `list-bmt`'s `end_id` only changes on battery-side logins.
  Any integration that caches the address id inherits a ≤(mirror interval)
  lifetime.
- **Live data is flaky by design.** The vendor relay enforces a ~30 s
  session TTL (21204 + ~30 s reject window; see `docs/PROTOCOL.md` §5.1).
  The official app does not re-handshake reliably: expect ~15-30 s of live
  numbers after app start, then "-" until the app is restarted. Seen
  identically against the genuine vendor servers, so it is an app defect,
  not a bridge artifact. Integrations that re-handshake every ≤7 s (as the
  HA client in this repo does) do not suffer from it.
- The app's firmware screen additionally falls back to **BLE direct**
  (`connectDevice(device, true)`) on retries — while connected, the module
  serves only that one BLE central and diagnostic BLE is blocked.
