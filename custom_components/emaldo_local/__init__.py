"""Repoint the Emaldo cloud integration at your self-hosted takeover API.

This component patches the loaded Emaldo integration's client library so
all of its API and data-plane calls go to your own takeover server
instead of the vendor's cloud. Use it together with a self-hosted
takeover stack (a local API that answers the vendor REST endpoints and
an E2E UDP relay holding the module's pinned identity) — see the README.

Configure via the UI (Settings -> Devices & Services -> Add Integration
-> "Emaldo local cutover") and enter the HTTPS host of YOUR takeover
API. Re-applies periodically so integration reloads are covered.
"""
from __future__ import annotations

import logging
from datetime import timedelta

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_time_interval

DOMAIN = "emaldo_local"
VENDOR_API_HOST = "api.emaldo.com"
VENDOR_DP_HOST = "dp.emaldo.com"

_LOGGER = logging.getLogger(__name__)


def _repoint(api_host: str) -> int:
    """Patch every loaded emaldo client module to the takeover host.

    The emaldo client library resolves hosts through module globals at
    request time, so patching the loaded modules is enough — including
    for already-live client instances. Only module-level ``API_HOST`` /
    ``DP_HOST`` constant pairs are touched.
    """
    import sys

    n = 0
    for mod in list(sys.modules.values()):
        if getattr(mod, "API_HOST", None) in (VENDOR_API_HOST, api_host) and \
           getattr(mod, "DP_HOST", None) in (VENDOR_DP_HOST, api_host):
            if mod.API_HOST != api_host or mod.DP_HOST != api_host:
                mod.API_HOST = api_host
                mod.DP_HOST = api_host
                n += 1
    return n


class EmaldoLocalFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Ask for the takeover API host; nothing else to configure."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is not None:
            return self.async_create_entry(
                title=f"Emaldo local API ({user_input['api_host']})",
                data={"api_host": user_input["api_host"]},
            )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("api_host"): cv.string,
                }
            ),
            description_placeholders={
                "example": "emaldo.example.com",
            },
        )


async def async_setup_entry(hass: HomeAssistant, entry) -> bool:
    """Start the repoint loop for this entry."""
    api_host: str = entry.data["api_host"]

    @callback
    def _apply(_now=None) -> None:
        n = _repoint(api_host)
        if n:
            _LOGGER.info("emaldo_local: repointed %d module(s) to %s",
                         n, api_host)

    _apply()
    entry.async_on_unload(
        async_track_time_interval(hass, _apply, timedelta(seconds=30))
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry) -> bool:
    return True
