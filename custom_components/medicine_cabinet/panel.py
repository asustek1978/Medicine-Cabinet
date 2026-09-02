"""Sidebar panel registration."""

from __future__ import annotations

from pathlib import Path

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant

from .const import (
    DOMAIN,
    PANEL_ICON,
    PANEL_STATIC_URL,
    PANEL_TITLE,
    PANEL_URL_PATH,
    PANEL_WEB_COMPONENT,
    VERSION,
)


async def async_register_panel(hass: HomeAssistant) -> None:
    """Register the dedicated medicine cabinet sidebar panel."""
    if PANEL_URL_PATH in hass.data.get("frontend_panels", {}):
        return

    frontend_path = Path(__file__).parent / "frontend"
    domain_data = hass.data.setdefault(DOMAIN, {})
    if not domain_data.get("panel_static_registered"):
        await hass.http.async_register_static_paths(
            [StaticPathConfig(PANEL_STATIC_URL, str(frontend_path), cache_headers=False)]
        )
        domain_data["panel_static_registered"] = True

    await panel_custom.async_register_panel(
        hass,
        webcomponent_name=PANEL_WEB_COMPONENT,
        frontend_url_path=PANEL_URL_PATH,
        module_url=f"{PANEL_STATIC_URL}/medicine-cabinet-panel.js?v={VERSION}",
        sidebar_title=PANEL_TITLE,
        sidebar_icon=PANEL_ICON,
        require_admin=False,
        config={"domain": DOMAIN, "version": VERSION},
        config_panel_domain=DOMAIN,
    )


def async_unregister_panel(hass: HomeAssistant) -> None:
    """Remove panel from sidebar."""
    if PANEL_URL_PATH in hass.data.get("frontend_panels", {}):
        frontend.async_remove_panel(hass, PANEL_URL_PATH)
