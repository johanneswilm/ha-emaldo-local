# Emaldo local takeover — design (verified 2026-11-17)

Full local control proven end-to-end: battery telemetry (SOC, V, A, temps,
serials, SOH for all 3 modules) read through our own API + relay, no vendor
cloud involved. See `README.md` for the running stack.

## Architecture

```
 battery module (LAN IP, Go)
   ├── HTTPS API  ──► your TLS reverse proxy on a hostname you own
   │                (e.g. emaldo.yourdomain.example) ──► fake_api.py :8199
   └── e2e UDP :1050/:1051 (host from login rsp) ──► e2e_relay.py (this host)
                                                       ▲
 HA / e2e.py app client (sender creds e2e_new_creds) ──┘
```

## Crypto layers (all broken; see docs/PROTOCOL.md)

| layer | scheme |
|---|---|
| API gm2 request | form `gm=2&iv&json&token`; AES-256-CBC, key=global APP_SECRET (32B zero-padded), IV = the 16-alnum request IV; JSON envelope `{Status:1,Action,Cmd,ErrorMessage,MessageId,Result}`; `Result` = base64(AES-CBC(APP_SECRET, **same request IV**, Rsp-JSON)) — server reuses the request IV for the response |
| login identity | server-issued `{id, group_id, end_id, end_secret, chat_secret, host, util_host, addtime, gmtime}`; module REJECTS self-issued identities (roster mismatch) — must present the home's real `group_id`/`chat_secret` |
| e2e frames | TLV binary (`0xa0a0` end_id, `0xa0a1` group, `0xa0a2` receiver, `0x90a3` nonce, `0x86f6` msgid), payloads RC4(`chat_secret`); relay is a blind forwarder — **no relay-side crypto**, so a dumb relay suffices |

Key constants (global in both app and module binaries): APP_SECRET
`FpF4Uqiio9k8p9VUSX36UZxy9wLs7ybT`; home group `<redacted-home-group-id>`;
chat_secret `<redacted-home-chat-secret>` (stable per home across logins);
module reg JWT + reg_token `CXRq...` replay verbatim.

## Identity strategy

- **Online mode (default):** `fake_api.py` proxies `/bmt/e2e-bmt-login/` to
  the live `api.emaldo.com`, then rewrites only `host`/`util_host` to our
  relay. Real identity ⇒ module accepts. Each successful replay overwrites
  `e2e_static.json` (archived capture).
- **Offline mode (`EMALDO_OFFLINE=1`):** replays `e2e_static.json` verbatim
  (fixed `end_id`/`end_secret` ⇒ stable for the app side too). Proven to work
  after the vendor relay was cut. This is the bankruptcy configuration.
- Login cadence: module re-logs-in every 10 s while its app-side channel is
  idle; longer intervals once an app session is active. Static replay keeps
  the same identity every time, so churn is harmless.

## App side

The app-side client library (`client.py`/`e2e.py`) is the community
SDK — [wertigpar/emaldo_python](https://github.com/wertigpar/emaldo_python)
(full session semantics:
alive(home)/alive(app)/wake/heartbeat then typed commands; `read_battery_info`
0x06 slot probes, `read_power_flow`, `read_overrides`, `read_accessories`)
drives everything against your relay `:1050` with creds in
`e2e_new_creds.json`. Relay rewrites stale `receiver` fields to the live
device end, so app creds need no per-login updates.

## Module internals found in firmware RE (`/tmp/emaldo_iot_fw.bin`)

- Config: **`/root/config.toml`** (env `CONFIG` overrides), TOML keys include
  `mqtt_enabled`, `enable_pprof`, `mqtt_broker_url`.
- **MQTT mode exists**: Paho client + `mqttcred.Mode` + `/bmt/access/auth`
  (`MQTTAccessAuthReq{id,token}` → `{broker_url, token, expired_at}`).
  Gated by `mqtt_enabled` in the module's local config — currently off.
  If the config file can ever be written (learn RPC? config push channel?),
  MQTT with a local broker would replace e2e entirely.
- OTA: `/bmt/get-latest-ver-url/` (never seen hit by the module in weeks of
  captures; check daily). Firmware image signature status still unverified —
  `scripts/block_ota.sh` pins the module to our API until then.
- Module is Debian-based (systemd, wpa_supplicant, dhclient hooks, `/etc/hps/`,
  `HPS_USER_MUTABLE` markers) — a full local deamon path is plausible if we
  ever get shell.

## App-API (gm1) proxy + HA cutover (verified 2026-11-17)

`fake_api.py` also terminates the **app/HA-side** gm1 API on the same
vhost. The HA `emaldo` integration was repointed by patching
`emaldo_lib/const.py` (`API_HOST`/`DP_HOST`) inside a core-backup tar
(`inject_patch.py`) and restoring it — HAOS gives no direct config
shell; the HA Core backup API (`backup/generate`, `/api/backup/download`,
`/api/backup/upload`, WS `backup/restore`) is the file-access path:

- **Synthesized locally, always** (vendor state is untrustworthy or gone):
  `/home/e2e-login/`, `/bmt/e2e-user-login/`, `/bmt/search-bmt/` — served
  from `e2e_new_creds.json` + `e2e_static.json` (pinned identity; the
  vendor e2e-login *rotates* chat_secrets, which would break our pinned
  relay session), plus `/bmt/list-bmt/` fallback when the vendor record
  is empty (it auto-unbinds BMTs silent for ~20 min — observed
  2026-11-17 12:30; DP stats died the same way: `GetBmt: mongo: no
  documents`).
- **Proxied to the live vendor while it exists**, appended to
  `gm1_corpus.jsonl` (replay corpus: login, homes, stats, schedule, fcr);
  on vendor failure stats paths fall back to the newest corpus entry.
- `ha/custom_components/emaldo_local/` does the repointing install-side.
- After bankruptcy: set `EMALDO_OFFLINE=1`; everything needed for live
  battery control is local. Daily rollups must then be computed from
  `samples.jsonl` (`sample_loop.py`/`emaldo-sample`) instead of the
  vendor's DP stats.

## Bankruptcy runbook

1. `EMALDO_OFFLINE=1` in fake_api env (or drop the proxy: it fails over to
   static automatically after the first prod timeout). Stats/schedule
   endpoints then serve the last corpus entries; live data comes from the
   e2e relay + `samples.jsonl`. Chat rotation (`active_e2e`) is fully
   local — battery and app clients keep re-keying after bankruptcy with
   no vendor involvement.
2. Keep `e2e_static.json` + `e2e_new_creds.json` + reg token in
   `secrets.md`/vault — they are the only unforgeable material.
3. Relay + fake_api + bleredir are already vendor-independent; nothing else
   references emaldo.com at runtime.
4. Risk: if the vendor ever pushes a last-minute OTA that changes the API
   contract or bricks the learn path, takeover code must be current — run
   `scripts/block_ota.sh` (review first) and keep the raw captures.

## Capture inventory (keep until takeover is permanent)

`captures/` hourly pcap ring (.185 everything), `e2e_transcript_*.pcap`,
`fake_api.log` (every module API call, decrypted), `relay_live.log`,
`e2e_static.json` (last real server-issued identity), `docs/PROTOCOL.md`,
`docs/emaldo/` (working Python app client).
