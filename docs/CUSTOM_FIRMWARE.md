# Custom local firmware — design notes

Goal: a **community replacement** for the vendor IoT-module app (a single
static Go binary) that removes all cloud dependency and exposes a plain
local web UI/API for configuration (MQTT broker URL and credentials first)
and live data. Generic and redistributable — any owner should be able to
install and configure it without the original vendor infrastructure.

Reverse-engineering basis: `docs/PROTOCOL.md` (e2e + REST + BLE),
`docs/OTA.md` (update channel), `docs/APP_COMMANDS.md` (msct commands),
plus the subsystem inventory below.

## 1. What the vendor binary does (recreated map)

Go 1.24 static ARM ELF (~31 MB), one userspace process; the battery keeps
running while it is down. Major subsystems (from its symbol table):

| area | content |
|------|---------|
| `hermes` | hardware abstraction: battery/inverter/cabinet/EV-gun over serial (`/dev/ttymxc2`, `/dev/mcu1`) + sidecar |
| `themis` | multi-battery cluster: master/slave election, settings applicator |
| `frr`/`fcrctrl`/`fcr` | grid-balancing (FCR/mFRR) schedules, live balancing, simulated grid frequency |
| `powerctrl` | charge/discharge policies, peak shaving, anti-reflux, manual selling |
| `routers` | command surfaces: cloud REST (`cloudr`), user REST (`usersr`), `ww33r` device vocabulary, `netconfr`, `inspector` (DB get/set + log levels) |
| `ota`/`checkupdate` | downloader, staging, retry machinery |
| `mqttcred` | MQTT credential provider: vendor-API source (`/bmt/access/auth`) or middleware source (`mqtt: fetch broker url from {router_url}{path}`) |
| `settings`/`schedules`/`segments` | day plans, prices, time-of-use |
| `display`/`lcdsdk`, sound, backlight | local LCD UI on `/dev/fb0` |

Platform: Debian-style rootfs (systemd, apt-based "RootfsUpgrade" channel
— the only thing that could wipe `/root`; never offer it via your own
manifests), ppp/4G modem, chrony/ntp. Identity + config live in `/root`
(`config.toml`, `filedb`, `.device_id`, `.regtoken`) and survive binary
swaps (verified: identical device id re-registered after power-cycle
reboots with a staged update).

Config vocabulary (TOML keys recovered from the binary): `mqtt_enabled`,
`mqtt_broker_url`, `mqtt_username`, `mqtt_password`, `enable_pprof`,
`no_ble_crypto`, `no_sync_cloud`, `no_data_collection`,
`no_upgrade_on_initial`, `custom_check_update_url`,
`custom_check_update_policy`, `exhibition_mode`, `user_ota_preference`,
plus ~100 more. None are settable via BLE or the app API — they are the
device-local configuration surface a custom firmware owns outright.
`enable_pprof=true` would open a pprof/inspector HTTP server on
`0.0.0.0:16061` on the LAN (gated by that key).

## 2. Replacement architecture

Single static Go binary (same cross-compile target as vendor: linux/arm,
go1.24), installed via the OTA manifest mechanism (see `docs/OTA.md`).

```
hermesd    serial client (tty/mcu): telemetry + control; codec ported
           from docs/PROTOCOL.md + pcap captures
webd       :80 HTTP: first-boot wizard, config UI, REST API, live
           dashboard (SSE)
mqttd      publisher + subscriber, TLS optional, Home Assistant MQTT
           discovery
bled       BLE GATT JSON dispatcher (same fixed-key AES as vendor):
           provisioning/recovery commands incl. set_mqtt_broker
configd    atomic config store + backups + export/import
ota-self   signed (ed25519) self-update — we do better than md5-only
(opt)      legacy cloud/e2e bridge for transition period
```

## 3. Configuration UX (the headline requirement)

- **First boot**: WiFi AP mode (`Emaldo-XXXX`) or DHCP + mDNS
  (`emaldo.local`); captive portal wizard: MQTT broker URL
  (`mqtt://`/`mqtts://host:port`), credentials, topic prefix, HA discovery
  toggle, admin password.
- **Normal operation**: `http://<device-ip>/` — settings (broker, TLS,
  charge policy, timezone), live view (power flow, SOC, per-pack cells,
  inverter state, event log), REST API (`/api/v1/{config,state,control,
  events}`, token-auth).
- **BLE** mirrors the config keys (JSON `{"cmd":"set_mqtt_broker",...}`)
  for headless recovery; `get_config` returns secrets-masked config.

## 4. MQTT data model (HA-first)

- `<prefix>/telemetry` (1–5 s): power flow, SOC, aggregates
- `<prefix>/battery/N/{soc,voltage,current,temp,cells}`
- `<prefix>/inverter/N/{power,freq,faults}`
- `<prefix>/event` (LWT + alarms); `<prefix>/cmd` subscribed: charge/
  discharge limits, mode (auto/charge-only/discharge-only/standby), reboot
- Home Assistant MQTT discovery announced per entity.

## 5. Security & generic install story

- Admin token set at wizard; optional TLS (self-signed with fingerprint
  display, or user certs). No vendor secrets needed at runtime — the
  replacement speaks the hardware protocols directly.
- Install: redirect the module's API traffic once (BLE `set_http_host` /
  `set_data_host` commands work on stock firmware with the published
  fixed BLE key), serve a manifest offering the new binary (version bump
  required, bytes must differ from stock — see `docs/OTA.md` §2.1), and
  keep a stock-binary manifest on hot standby for rollback.
- Post-install the firmware is fully local; the vendor cloud is not
  required for any operation.

## 6. Phasing

- **FW-1** (read-only, cannot break control paths): serial telemetry →
  MQTT + web live view + config UI.
- **FW-2**: control — charge policies, manual selling, day schedules
  (96-slot override format already RE'd, see `docs/PROTOCOL.md` §8).
- **FW-3**: polish — LCD (fb0), sound, multi-device cluster emulation,
  FCR hooks (off by default), app-compat e2e bridge.

## 7. Open RE work items for FW-1

1. hermes serial frame codec (port from `docs/PROTOCOL.md` idioms; the
   e2e/msct option vocabulary in `docs/APP_COMMANDS.md` §1 is the same
   TLV style).
2. Modbus register map for power/SOC/temperature reads (`INVT register
   schema` strings in the binary; confirm against serial captures).
3. MCU hello/boot handshake (`master hello`, `bst enter boot mode`
   strings).
4. The exact `{router_url}{path}` path used by the MQTT middleware cred
   source (arm a logging HTTP endpoint as `mqtt_broker_router_url` and
   watch what the stock module requests — reveals the API to emulate or
   replace).
