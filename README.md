# Emaldo local cutover — Home Assistant custom component

Runtime **repoint** of the [Emaldo cloud integration](https://github.com/wertigpar/ha-emaldo)
at a **self-hosted takeover API** you run on your own network.

Why: Emaldo (the vendor behind the "Power Core" home battery) runs the
integration entirely through its cloud (`api.emaldo.com`, `dp.emaldo.com`
and an E2E UDP relay). If the vendor disappears, the integration — and
with it Home Assistant automation of your own battery — stops working.
There is no official local API.

This component patches the loaded integration's client library so every
REST call (login, device list, E2E credential exchange, stats, schedule)
goes to the takeover host you enter in the config flow. The integration's
own E2E crypto is untouched: your takeover server supplies the device
identity, so the encrypted control channel is re-established against
your relay instead of the vendor's.

## Prerequisites

You must have a **takeover stack** already running on your LAN. The
critical step is *capturing your module's identity while the vendor
cloud is still reachable*: the vendor issues the module's E2E identity
(`end_id` / secrets) and rotates the session keys, and none of that can
be obtained after the vendor shuts down. A takeover stack therefore has
at least:

- a local API answering the vendor's REST endpoints (gm1 app API + gm2
  module API), holding and rotating your captured session keys;
- an E2E UDP relay (vendor port 1050 + util 1051) speaking to the
  module with the captured identity;
- a way to point the module's API endpoint at your server (its config
  carries the API base URL; the module validates TLS certificates, so
  serve a valid cert for a hostname you control).

No public, supported takeover stack is published alongside this repo
(yet); treat this component as the Home Assistant side of a DIY setup.
You will also want your own copy of the protocol and library
observations for your integration's version — this component only
patches host constants, it does not implement the protocol.

## Install

1. HACS → Integrations → three-dot menu → Custom repositories →
   add this repo, category *Integration*.
2. Download "Emaldo local cutover", restart Home Assistant.
3. Settings → Devices & Services → Add Integration →
   "Emaldo local cutover". Enter your takeover API host
   (e.g. `emaldo.example.com` — HTTPS, valid certificate).
4. Reload the existing Emaldo config entry (or restart). Entities
   recover against your local stack.

## Notes

- The Emaldo client library resolves its hosts through module globals at
  request time; patching loaded modules covers live client instances.
  The loop re-applies every 30 s so integration reloads are covered.
- One config entry; delete and re-add to change the host.
- Nothing is sent to the vendor once repointed; the takeover server
  answers everything.
