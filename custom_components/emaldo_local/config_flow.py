"""Config flow: the takeover host, and the CA its API is served with."""
from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import config_validation as cv

from . import DOMAIN


class EmaldoLocalFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Ask for the takeover API host, and the CA it is served with."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is not None:
            ca = (user_input.get("ca_pem") or "").strip()
            if ca and "BEGIN CERTIFICATE" not in ca:
                return self.async_show_form(
                    step_id="user",
                    data_schema=vol.Schema(
                        {
                            vol.Required("api_host"): cv.string,
                            vol.Optional("ca_pem"): cv.string,
                        }
                    ),
                    description_placeholders=self._placeholders(),
                    errors={"base": "not_a_certificate"},
                )
            return self.async_create_entry(
                title=f"Emaldo local API ({user_input['api_host']})",
                data={"api_host": user_input["api_host"], "ca_pem": ca},
            )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("api_host"): cv.string,
                    vol.Optional("ca_pem"): cv.string,
                }
            ),
            description_placeholders=self._placeholders(),
        )

    @staticmethod
    def _placeholders() -> dict:
        return {
            "example": "emaldo.example.com or 192.168.1.20",
            "ca": "https://<your-api-host>:4443/ca.pem",
        }


