"""Home Medicine Cabinet integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.event import async_track_time_change

from .const import DOMAIN, EVENT_ALERT
from .panel import async_register_panel, async_unregister_panel
from .storage import MedicineCabinetStore
from .websocket import async_setup as async_setup_websocket

PLATFORMS = ["sensor"]


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up the integration namespace."""
    hass.data.setdefault(DOMAIN, {"entries": {}})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Home Medicine Cabinet from a config entry."""
    domain_data = hass.data.setdefault(DOMAIN, {"entries": {}})
    domain_data.setdefault("entries", {})

    store = MedicineCabinetStore(hass)
    await store.async_load()
    domain_data["entries"][entry.entry_id] = store

    async_setup_websocket(hass)
    await async_register_panel(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if not domain_data.get("services_registered"):
        _register_services(hass)
        domain_data["services_registered"] = True

    if not domain_data.get("alert_scheduler"):
        async def _scheduled_alert(now) -> None:
            await _daily_alert_check(hass, now)

        domain_data["alert_scheduler"] = async_track_time_change(
            hass,
            _scheduled_alert,
            hour=9,
            minute=0,
            second=0,
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False

    domain_data = hass.data.get(DOMAIN, {})
    domain_data.get("entries", {}).pop(entry.entry_id, None)

    if not domain_data.get("entries"):
        async_unregister_panel(hass)
        unsubscribe = domain_data.pop("alert_scheduler", None)
        if unsubscribe:
            unsubscribe()

    return True


def _get_store(hass: HomeAssistant) -> MedicineCabinetStore:
    entries = hass.data[DOMAIN]["entries"]
    return next(iter(entries.values()))


def _register_services(hass: HomeAssistant) -> None:
    async def handle_consume(call: ServiceCall) -> None:
        await _get_store(hass).async_consume(
            call.data["package_id"], call.data["amount"], call.data.get("reason", "Принято")
        )

    async def handle_adjust(call: ServiceCall) -> None:
        await _get_store(hass).async_adjust(
            call.data["package_id"], call.data["remaining"], call.data.get("reason", "Корректировка")
        )

    async def handle_consume_linked(call: ServiceCall) -> None:
        await _get_store(hass).async_consume_linked(
            package_id=call.data.get("package_id", ""),
            gtin=call.data.get("gtin", ""),
            name=call.data.get("name", ""),
            strength=call.data.get("strength", ""),
            owner=call.data.get("owner", ""),
            amount=call.data["amount"],
            reason=call.data.get("reason", "Medication Manager"),
        )

    hass.services.async_register(
        DOMAIN,
        "consume",
        handle_consume,
        schema=vol.Schema(
            {
                vol.Required("package_id"): str,
                vol.Required("amount"): vol.Coerce(float),
                vol.Optional("reason", default="Принято"): str,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        "adjust",
        handle_adjust,
        schema=vol.Schema(
            {
                vol.Required("package_id"): str,
                vol.Required("remaining"): vol.Coerce(float),
                vol.Optional("reason", default="Корректировка"): str,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        "consume_linked",
        handle_consume_linked,
        schema=vol.Schema(
            {
                vol.Optional("package_id", default=""): str,
                vol.Optional("gtin", default=""): str,
                vol.Optional("name", default=""): str,
                vol.Optional("strength", default=""): str,
                vol.Optional("owner", default=""): str,
                vol.Required("amount"): vol.Coerce(float),
                vol.Optional("reason", default="Medication Manager"): str,
            }
        ),
    )


async def _daily_alert_check(hass: HomeAssistant, now) -> None:
    """Create one daily HA notification and an event when attention is needed."""
    store = _get_store(hass)
    if not store.data["settings"].get("notifications_enabled", True):
        return

    state = store.snapshot()
    attention = [p for p in state["packages"] if p["expired"] or p["expiring"] or p["low_stock"] or p["empty"]]
    if not attention:
        return

    hass.bus.async_fire(EVENT_ALERT, {"summary": state["summary"], "packages": attention})

    lines = []
    for item in attention[:12]:
        if item["expired"]:
            reason = "просрочено"
        elif item["empty"]:
            reason = "закончилось"
        elif item["low_stock"]:
            reason = "заканчивается"
        else:
            reason = f"срок через {item['days_to_expiry']} дн."
        lines.append(f"• {item['name']} {item.get('strength', '')} — {reason}".strip())

    persistent_notification.async_create(
        hass,
        "\n".join(lines),
        title="Домашняя аптечка требует внимания",
        notification_id="medicine_cabinet_daily_alert",
    )
