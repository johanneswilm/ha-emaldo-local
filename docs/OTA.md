# OTA update contract — taking over the module's update channel

Verified live against a Power Core 2.0 (module fw v2.35.3, 2026-11). The
IoT module's self-update is a plain **HTTP-fetched manifest + md5-only
integrity check** — whoever answers the module's version check controls
what runs on the device. No signatures, no TLS pinning beyond the usual
server-cert validation for the API host (the *file* URL may be plain HTTP).

## 1. How the module checks for updates

`POST /bmt/get-latest-ver-url/<reg_token>` (gm2, same envelope as the rest
of the module API — see `docs/PROTOCOL.md` §"Module-side API"). Request
body `{"types":["iot"],"id":"<bmt-id>","model":"<model>"}`.

Rsp (plaintext JSON inside the gm2 envelope):

```json
{"versions": [{"latest_version": "v2.35.4",
               "min_version": "v2.35.3",
               "url": "http://<your-host>:<port>/firmware.bin",
               "md5": "<md5 of the file>",
               "type": "iot",
               "force": true}],
 "count": 1}
```

- Fields observed in the firmware's parse structs: `latest_version`,
  `min_version`, `url`, `md5`, `type`, `force`, plus `count`/`versions`/
  `items`/`reason`/`time` at the rsp level. `force` is a boolean; its
  exact semantics are not fully mapped (see §4).
- The version gate uses hashicorp/go-version: `latest_version` must be
  **greater** than the running version; `min_version` content appears
  unenforced (downgrades accepted whatever you state — you author it).
- The artifact is a **bare static Go ELF** (the whole userspace app, ~31 MB
  for v2.35.x). No container, no A/B partition, no signature block.

## 2. Observed downloader behavior (v2.35.3)

1. Offered-md5 == the module's own running binary md5 → **silently
   skipped** (downloads nothing / installs nothing). To push a reinstall
   you must change the bytes (e.g. flip a character in a display-only
   `.rodata` string — same size, same code, new md5).
2. Otherwise: downloads the file (plain HTTP GET works; chunked retry
   machinery exists — `download chunk %d`, `md5sum %s`), verifies md5,
   **stages** the package.
3. Staged ≠ installed — **the apply is a ~60-minute in-session timer**
   (verified live 2026-11-18): download+md5+stage at T, binary swap at
   ~T+60 min, then the app **restarts itself** — no whole-module reboot,
   no `insert-upgrade-log`. From the outside the only traces are seconds
   of silence and a re-registration (`update-eid-v2`), easy to miss.
   While an update is pending the check loop re-polls the manifest every
   ~5-10 min, and the BLE `check_update` command triggers an immediate
   re-check. `"force": true` is not required. Firmware strings consistent
   with this: `check update waiting duration after configured`,
   `ResetStatusForRetry`, force-update retry counters.
4. The staged package does **not** survive a reboot — every boot
   re-downloads and re-stages (so the ~60 min timer restarts).
5. The module re-checks at boot and (observed) at ~23:59 local daily.

## 3. Update-trigger commands

- BLE GATT JSON dispatcher (fixed AES key, see `docs/PROTOCOL.md`):
  `check_update` is a real command → `{"cmd":"check_update","found":true}`
  when an update is offered; it does not apply it. Probed-and-rejected
  (`status:0`): `start_update`, `apply_update`, `update_now`,
  `confirm_update`, `force_update`, `begin_update`, `ota_start`,
  `do_update`, `update`, `ota`, `upgrade`, `install`, `apply`.
- The app-side trigger is `update_chips` (msct opcode `0xA103`, see
  `docs/APP_COMMANDS.md`). The module accepts msct commands on its e2e
  channel (its own logs contain `recv 0xa103 cmd ...`), but the official
  app's firmware screen asks the **vendor cloud**, not the battery — so a
  locally-staged update is invisible to the app and it will keep saying
  "up to date".

## 4. Safe self-test recipe (prove you own the channel, zero risk)

1. Serve the **exact vendor binary** from a local HTTP server with a
   manifest whose `md5` matches it and `latest_version` one patch above
   the running one. Expected: nothing happens (§2.1 — the skip behavior
   itself is a useful signal).
2. Serve a **bit-tweaked copy** (two chars flipped in an error string,
   size unchanged; document your diff). Expected: download + stage, then
   **install ~60 min later**. For an externally visible result, flip the
   *version string* instead (`v2.35.3`→`v2.35.5`, one char, same length):
   after the swap, `BLE get_version` answers `v2.35.5`. Watch for short
   silences and an `update-eid-v2` re-registration — not a reboot.
3. Proof probe: serve the **original vendor bytes** under a higher
   version. A stock module skips them (offered md5 == its own binary); a
   module running your build downloads them — clean, externally visible
   proof that your binary is live. Identity (`bmt_id`/`eid` in
   `update-eid-v2`) stays unchanged across swaps: `/root` (config,
   identity dotfiles, databases) survives a same-partition binary swap.
   This is the safety property the custom-firmware track relies on.
4. Rollback = manifest pointing back at a copy of the vendor binary.

## 5. Implications

- The update channel is a **single point of ownership**: once the module's
  API traffic is redirected to your server, you decide what binary runs,
  forever, with vendor-era rollback always available.
- Only the OS-level "RootfsUpgrade" path (apt-based; `[RootfsUpgrade]`
  strings in the firmware) could wipe `/root` — and you author its
  manifests too, so simply never offer it.
- For a **community firmware** distribution channel, see
  `docs/CUSTOM_FIRMWARE.md` §"Installation story".
