# Emaldo local cutover

**Home Assistant component + reference takeover stack for the "Emaldo
bankruptcy" case.**

The Emaldo "Power Core" home battery has no local API. Everything —
monitoring, scheduling, charge/discharge control — runs through the
vendor's cloud: a REST API (`api.emaldo.com`, `dp.emaldo.com`), an E2E
UDP relay server, and the vendor OTA channel. If Emaldo goes bankrupt
and switches its servers off, **your battery keeps working physically
but becomes unmanageable**: the mobile app and the Home Assistant
integration both die with the cloud.

License: MIT (see LICENSE).

This project keeps you in control of your own hardware after that
happens: you run the cloud's endpoints yourself, using your module's
identity captured while the vendor is still alive.

* We paid for these batteries; the vendor refuses to offer a customer
  API. This repo exists so owners can keep operating equipment they
  own. Use at your own risk; none of this is supported by the vendor.

## ⚠ Hard deadline: act while the vendor's servers still exist

The module's E2E identity (`end_id`, `end_secret`, `chat_secret`,
`group_id`) and its registration token are **issued by the vendor** to
your specific module. They cannot be reconstructed after shutdown.
Everything below must therefore be captured **before** the servers go
away — the takeover has to be *in production* before the final day, not
attempted afterwards.

Capture checklist (do early, then run the takeover live for a while
before the end):

1. **Module registration JWT** — captured from the module's own boot
   login (`fake_api.log` line for `/bmt/login/`, `token` field).
2. **Vendor-issued module E2E identity** — one real
   `/bmt/e2e-bmt-login/` response (saved as `e2e_static.json` by the
   reference `fake_api.py` in online mode automatically).
3. **App-side E2E credentials** — one real sequence of
   `/home/e2e-login/`, `/bmt/e2e-user-login/`, `/bmt/search-bmt/`
   (saved as `e2e_new_creds.json` when you run the HA/app integration
   through your proxy once).
4. **Replay corpus** — the reference API records every vendor REST
   answer (`gm1_corpus.jsonl`): stats, schedules, plans. After the
   shutdown these replay (stale) instead of failing.
5. **Override day-plan state** — one E2E `0x1a` override write passes
   through your relay and is stored (`override_state.json`).
6. **Full dress rehearsal** — point your stack at a dead vendor
   (`EMALDO_PROD_API=https://127.0.0.1:1`) and verify the entire client
   sequence still completes (see Testing below). Do this weeks before
   the real thing.

While the vendor lives, **keep replaying the module's logins and metric
uploads to the live vendor** (reference API does this automatically):
the vendor auto-**unbounds silent BMTs within ~20–45 minutes** and an
unbound module gets a different treatment (DP stats, OTA targeting).
The takeover can coexist with the vendor: module and app talk to you,
you mirror to the vendor.

## Architecture

| piece | role |
| --- | --- |
| `reference/fake_api.py` | serves BOTH APIs on one HTTPS port: **gm1** (the app/HA REST API: login, homes, device list, E2E credential exchange, stats, schedule) and **gm2** (the module's own API: boot login, `e2e-bmt-login`, config, metrics). Online mode proxies to the vendor + records; `EMALDO_OFFLINE=1` answers everything locally. |
| `reference/e2e_relay.py` | UDP `:1050` + util `:1051`: forwards frames between app clients and the battery module with the captured identity; answers `alive/wake/heartbeat` with vendor byte-exact templates (the module expects them); **stores `0x1a` override writes and answers `0x1b`/`0xa0` subscriptions itself** (the module itself refuses override reads — the vendor relay is the state store, so yours must be too). |
| `reference/arp_relay.py` | capture stage: transparent ARP forwarding of one LAN host + tcpdump, incl. DNAT of the module's vendor sessions to your hosts. Capture only — not needed once the module's API base URL points at you. |
| this component | Home Assistant side: repoints the [Emaldo integration](https://github.com/wertigpar/ha-emaldo) client library at your API host at runtime. |

Framing/crypto summary (see also the reference code, which implements
all of it):

* gm1 envelopes: form fields `gm=1&json=<hex>&token=<hex>`; payload =
  `rc4(APP_SECRET, snappy(json))`, outer JSON has hex in `Result`.
* gm2 envelopes (module): `gm=2&iv=<16 chars>&json=<b64>&token=<b64>`,
  AES-256-CBC with `APP_SECRET`, zero/char-padded.
* E2E payload: AES-256-CBC with `chat_secret` over MSCT-framed messages
  (plaintext header: end/group ids, method, nonce, msgid).
* The module **validates server TLS certificates** — serve your API on
  a hostname *you* control with a real certificate (e.g. Let's Encrypt
  via your own domain / reverse proxy). Self-signed does not work.
* Point the module at your host: its API base URL is config-settable
  (`http base url` / `http data url` — module config file or the BLE
  service's setters). The `host`/`util_host` fields inside
  `e2e-bmt-login` responses then steer its UDP sessions to your relay.

## What the takeover must answer — and BLOCK

The module must never reach the vendor again after cutover; every
request it makes must be intercepted and answered by you. The
reference API's routing (`gm1_proxy`, `fake_result`, `prod_login`)
implements exactly this split:

| request | what you must do |
| --- | --- |
| `POST /bmt/e2e-bmt-login/` (gm2, the module's own login) | **Critical.** Answer with the captured pinned identity + YOUR relay `host`/`util_host`, a **fresh `addtime`** and your **current rotating `chat_secret`** (see rotation below). Never forward after shutdown. |
| module boot/registration, `iot-online`, `global-config`, `get_master_switch`, price defaults … | plausible local answers (`gmtime`, empty config `{}`). A wrong shape makes the module retry-loop. |
| **`/bmt/get-latest-ver-url/`** | **Return `{"versions": [], "count": 0}` and never proxy it to the vendor.** This is the firmware OTA source; the update check is **MD5-only (no signature)**, so whoever answers this endpoint controls your battery's firmware. Blocking it here is the OTA guarantee. |
| module metric/log uploads | while the vendor lives: proxy verbatim (keeps the binding alive, see unbind note). After shutdown: accept and ignore. |
| app/HA `/user/login/`, `/home/list-homes/` | live-proxy while alive (corpus-recorded), replay the corpus afterwards. |
| app/HA `/bmt/list-bmt/`, `/bmt/search-bmt/`, `/home/e2e-login/`, `/bmt/e2e-user-login/` | **synthesize from your captured identity files** — the vendor's records decay (it unbinds/rotates; post-shutdown there is nothing). Return YOUR relay host and current chat secret. |
| app/HA `/bmt/stats/...`, schedule | live-proxy while alive; on vendor failure (`Status 0` or dead) **replay the newest good corpus entry** (skip entries with `Status != 1` or empty `Result` — the vendor emits hollow successes). |
| module direct internet fallbacks | optionally pin the module to your LAN (iptables on your gateway: allow LAN + local DNS, drop the rest — incl. its `generate_204` connectivity probes, Cloudflare DoH and the vendor eSIM profile download). See eSIM warning below — a LAN pin cannot reach the module's own cellular. |
| battery ↔ your relay UDP | must stay reachable both ways — do **not** firewall 1050/1051 between the module and your relay. |

### Session rotation — why naive static replay dies after one hour

The battery re-rolls its app-facing E2E session roughly every hour and
adopts whatever `chat_secret` its *most recent module login* carried;
it then ignores frames under the old key, silently (REST logins and UDP
keepalives keep working, which makes this hard to spot). The vendor
handles this by rotating the chat secret when the app re-logins.
Replicate it: rotate `chat_secret`/`home_chat_secret` on a timer
(reference: every 25 min, `e2e_active.json`), serve the new value to the
module at its next ≤10 s login and to app clients at their next
credential fetch (they re-fetch automatically on stream errors — the
integration library was built for exactly this vendor rotation). Anchor
`addtime` to *now* on every module login response. The relay must hot-
reload group keys when the rotation file changes.

Also: **run a single E2E app client per install** (e.g. only the HA
integration). Our stack synthesizes one sender identity; two concurrent
E2E clients under the same identity collide at the battery and answers
go missing. Sample telemetry from the HA state machine instead.

## Reference implementation

```
cd reference
pip install -r requirements.txt      # pycryptodome, cramjam, scapy
export EMALDO_HOME=/var/lib/emaldo   # state dir, default: this directory
export EMALDO_REG_TOKEN='<captured module JWT>'
python3 fake_api.py 8199 &           # behind your reverse proxy (TLS!)
python3 e2e_relay.py &               # :1050 + :1051 UDP
```

State files in `$EMALDO_HOME` (all captured/generated by the steps
above): `e2e_static.json` (module identity), `e2e_new_creds.json` (app
identity), `e2e_active.json` (rotating chat — regenerates itself),
`gm1_corpus.jsonl` (replay corpus), `override_state.json`,
`fake_api.log` (every decrypted request — your capture record).

Env: `EMALDO_OFFLINE=1` (post-shutdown mode: no vendor calls at all),
`EMALDO_PROD_API` (vendor base, also how to *simulate* the shutdown),
`EMALDO_RELAY_HOST`/`EMALDO_UTIL_HOST` (returned to the module),
`EMALDO_ROTATE_S` (chat rotation period, default 1500).

These are reference-quality — extracted from a working production
deployment (a live battery running E2E through them for months of
testing), not a polished product. Expect to adapt them to your network.

## Testing (dress rehearsal)

Before trusting the setup for the real shutdown: run the API with
`EMALDO_PROD_API=https://127.0.0.1:1` (simulated bankruptcy) on a test
port and drive one full client sequence — login, list homes, list
devices, E2E credentials, live battery read, overrides, power flow,
schedule, stats — expecting: login from corpus replay, device record
synthesized, credentials from `e2e_active.json`, live battery answers
via your relay (`FWD dev->app` frames in `relay_live.log`), stats from
corpus. Also verify chat rotation survives: wait one rotation period and
watch the data flow resume automatically.

## Home Assistant

The component repoints the loaded Emaldo integration to your API host
(the library resolves hosts through module globals, so live clients
follow). One config entry: enter your takeover API's hostname.

1. HACS → Integrations → three-dot menu → Custom repositories →
   add this repo, *Integration*.
2. Download "Emaldo local cutover", restart Home Assistant.
3. Settings → Devices & Services → Add Integration →
   "Emaldo local cutover" → enter your takeover API host.
4. Reload the existing Emaldo config entry (or restart).

## Client credentials

The official mobile app and this ecosystem share fixed client
credentials (extracted from the vendor app; the vendor refuses to offer
a customer API). A takeover server must answer calls authenticated with
them:

```
APP_ID     = CXRqKjx2MzSAkdyucR9NDyPiiQR2vQcQ
APP_SECRET = FpF4Uqiio9k8p9VUSX36UZxy9wLs7ybT
```

They authenticate the *client*, not an account: your vendor account
email/password is still what logs in (against your takeover server once
repointed). Per-install device identities (E2E `end_id`/secrets, module
registration token) are issued by the vendor to YOUR module and are not
— and cannot be — shared here.

## Security notes

* **Firmware:** OTA integrity in the field is `verifyMd5` only — no
  cryptographic signature found in the module software. Your control of
  `get-latest-ver-url` is what prevents anyone (incl. whoever acquires
  the vendor's infrastructure) from flashing your battery. Never proxy
  it; return an empty version list.
* **Cellular/eSIM:** the module carries a full GSM/eSIM stack (vendor
  eSIM profile download + APN config; a connectivity monitor raises
  `ppp0` when LAN degrades). If your installation has a **paid eSIM
  data plan**, the vendor — or whoever acquires its infrastructure —
  can reach your module over cellular, bypassing everything you run on
  your LAN, including OTA. Do not pay for / disable the eSIM data plan.
* **BLE:** the module exposes setters (`http base url`, `wifi
  password`, `gsm-interface`) over Bluetooth; BLE crypto is enabled by
  default. Only a physically present attacker can use it.
* Vendor DNS is irrelevant to you (the module resolves over Cloudflare
  DoH), but your hostname must have a valid public certificate —
  the module refuses self-signed.

## Related projects (the community that got here first)

This takeover stands on work by others; before building anything new,
talk to them — several have already reverse-engineered the same API:

* [wertigpar/ha-emaldo](https://github.com/wertigpar/ha-emaldo) — the
  Home Assistant integration this component repoints (also 7 public
  forks with local adaptations).
* [wertigpar/emaldo_python](https://github.com/wertigpar/emaldo_python)
  — reverse-engineered Python API/CLI library incl. E2E client and its
  own PROTOCOL.md; our docs extend, not replace, it.
* [wertigpar/ha-battery-optimizer](https://github.com/wertigpar/ha-battery-optimizer)
  — profit-optimization layer on top of the integration (we run a fork).
* [pylenius/ha-emaldo](https://github.com/pylenius/ha-emaldo) — the
  earlier independent unofficial integration (May 2026).

If you have a takeover setup (or a better one), please open an issue
here — the goal is one working path for everyone before the cloud
disappears.

## Documentation map

* [`docs/PROTOCOL.md`](docs/PROTOCOL.md) — the protocol bible: REST
  (gm1/gm2), e2e relay sessions, packet formats, every known command and
  payload layout, module-side API.
* [`docs/OTA.md`](docs/OTA.md) — the OTA/update contract: manifest
  schema, the md5-skip and stage-don't-install behaviors, the safe
  self-test recipe, and why you end up owning the module's binary.
* [`docs/APP_COMMANDS.md`](docs/APP_COMMANDS.md) — the app's msct command
  channel (transport, the `0xA0xx` opcode table incl. `update_chips`),
  and official-app quirks (cloud-side "up to date" verdict, address-id
  rotation per `e2e-user-login`, the ~30 s live-data flakiness).
* [`docs/CUSTOM_FIRMWARE.md`](docs/CUSTOM_FIRMWARE.md) — design notes for
  a community replacement firmware: local web UI for MQTT-broker config,
  HA MQTT discovery, BLE provisioning, phasing, and the remaining RE work
  items.
* [`docs/VENDOR_BRIDGE.md`](docs/VENDOR_BRIDGE.md) — keeping grid-rewards
  eligibility while running the vendor-free firmware: a two-headed bridge
  on a home server that emulates the device toward Emaldo's cloud and
  translates dispatches into the local firmware's API.

## Companion: emaldo-local firmware

The firmware half of this project lives in its own repo:
[johanneswilm/emaldo-local](https://github.com/johanneswilm/emaldo-local) —
an MIT-licensed, from-scratch replacement for the vendor module app
(web config UI, MQTT + Home Assistant discovery, signed self-updates).
**Untested and not for use while the vendor cloud still matters to you**
(see its README); the takeover stack in this repo is the right tool
until then.

## Known pitfalls (learned the hard way)

* Vendor "successes" with empty `Result` and `Status 0` with HTTP 200 —
  filter corpus replays on both.
* Corpus replay keys must include the stats sub-path
  (`/bmt/stats/grid` vs `/bmt/stats/...plan...`) or you serve a grid
  day for a schedule request.
* The module logs in every ~10 s; the vendor's DP stats flap for hours
  (mongo fallout after unbind) — your live-proxy mode must tolerate it
  and fall back to corpus per call.
* Two E2E app clients under one synthesized sender identity → missing
  answers (single-client rule).
* `addtime` from a recorded vendor response is stale — re-stamp it.
* After ~1 h the battery ignores the old chat secret (rotation above).
