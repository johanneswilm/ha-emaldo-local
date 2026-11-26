"""Repoint the Emaldo cloud integration at your self-hosted takeover API.

This component patches the loaded Emaldo integration's client library so
all of its API and data-plane calls go to your own takeover server
instead of the vendor's cloud. Use it together with a self-hosted
takeover stack (a local API that answers the vendor REST endpoints and
an E2E UDP relay holding the module's pinned identity) — see the README.

Configure via the UI (Settings -> Devices & Services -> Add Integration
-> "Emaldo local cutover"): enter the HTTPS host of YOUR takeover API and,
if it serves a private CA, paste that CA certificate so the client trusts
it. Re-applies periodically so integration reloads are covered.
"""
from __future__ import annotations

import logging
import os
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval

DOMAIN = "emaldo_local"
VENDOR_API_HOST = "api.emaldo.com"
VENDOR_DP_HOST = "dp.emaldo.com"
# Where the combined trust bundle is written inside the HA config dir.
BUNDLE_FILE = "emaldo_local_ca_bundle.pem"

_LOGGER = logging.getLogger(__name__)


def _repoint(api_host: str) -> int:
    """Patch every loaded emaldo client module to the takeover host.

    The emaldo client library resolves hosts through module globals at
    request time, so patching the loaded modules is enough — including
    for already-live client instances. Only module-level ``API_HOST`` /
    ``DP_HOST`` constant pairs are touched: either the vendor pair, or a
    single takeover host already patched in by hand (both names equal),
    which is what makes moving from one takeover server to another work.
    """
    import sys

    n = 0
    for mod in list(sys.modules.values()):
        host = getattr(mod, "API_HOST", None)
        dp = getattr(mod, "DP_HOST", None)
        if not isinstance(host, str) or not isinstance(dp, str):
            continue
        if dp != host and host != VENDOR_API_HOST:
            continue
        if host not in (VENDOR_API_HOST, api_host) and host != dp:
            continue
        if host != api_host or dp != api_host:
            mod.API_HOST = api_host
            mod.DP_HOST = api_host
            n += 1
    return n


def _write_bundle(hass: HomeAssistant, ca_pem: str) -> str:
    """Write "public CAs + your CA" to one bundle file and return its path.

    The takeover API is usually served with a private CA. A client that
    verifies with only that CA would stop trusting every other site, and
    one that verifies with the system bundle would reject the takeover
    API, so the bundle is the union of both.
    """
    import certifi

    with open(certifi.where(), "r", encoding="utf-8") as f:
        public = f.read()
    text = public.rstrip("\n") + "\n" + ca_pem.strip() + "\n"
    path = hass.config.path(BUNDLE_FILE)
    tmp = path + ".new"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    return path


def _trust(bundle_path: str) -> int:
    """Point every loaded emaldo client's HTTP session at the bundle.

    ``requests`` does not read the system trust store, so installing the
    CA into the host is not enough; the session's ``verify`` has to name
    the bundle. Only sessions owned by an emaldo client object are
    touched, so no other integration changes what it trusts. Clients
    created later are picked up by the periodic pass.
    """
    import gc

    import requests

    n = 0
    for obj in gc.get_objects():
        module = getattr(type(obj), "__module__", "") or ""
        if not module.startswith(("custom_components.emaldo", "emaldo")):
            continue
        session = getattr(obj, "_http", None)
        if not isinstance(session, requests.Session):
            continue
        if session.verify != bundle_path:
            session.verify = bundle_path
            n += 1
    return n


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Start the repoint loop for this entry."""
    api_host: str = entry.data["api_host"]
    ca_pem: str = entry.data.get("ca_pem") or ""
    bundle_path = _write_bundle(hass, ca_pem) if ca_pem else None

    @callback
    def _apply(_now=None) -> None:
        n = _repoint(api_host)
        if n:
            _LOGGER.info("emaldo_local: repointed %d module(s) to %s",
                         n, api_host)
        if bundle_path:
            t = _trust(bundle_path)
            if t:
                _LOGGER.info("emaldo_local: pointed %d client session(s) at %s",
                             t, bundle_path)

    _apply()
    entry.async_on_unload(
        async_track_time_interval(hass, _apply, timedelta(seconds=30))
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry) -> bool:
    return True
