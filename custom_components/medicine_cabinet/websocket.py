"""WebSocket commands used by the medicine cabinet panel."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN
from .lookup import MedicineLookup
from .rls_web import RLSBriefFetcher
from .pharmacy_prices import PharmacyPriceFetcher
from .storage import DuplicateSerialError, MedicineCabinetStore, parse_gs1_code


def _store(hass: HomeAssistant) -> MedicineCabinetStore:
    entries = hass.data.get(DOMAIN, {}).get("entries", {})
    if not entries:
        raise RuntimeError("Medicine Cabinet is not configured")
    return next(iter(entries.values()))


def _custom_brief(package: dict[str, Any]) -> dict[str, Any] | None:
    """Return the user-edited short instruction, if enabled for a package."""
    if not package.get("brief_custom"):
        return None
    indications = str(package.get("brief_custom_indications") or "").strip()
    dosage = str(package.get("brief_custom_dosage") or "").strip()
    contraindications = str(package.get("brief_custom_contraindications") or "").strip()
    return {
        "brief_custom": True,
        "brief_available": bool(indications or dosage or contraindications),
        "brief_indications": indications,
        "brief_dosage": dosage,
        "brief_contraindications": contraindications,
        "brief_source": "Моя инструкция",
        "brief_live": False,
        "brief_error": "",
    }


async def _lookup_catalog(
    hass: HomeAssistant,
    gtin: str | None,
    force: bool = False,
    *,
    raw_code: str | None = None,
    barcode_format: str | None = None,
) -> tuple[dict[str, Any] | None, str, str]:
    """Return data from the read-only local medicine catalog."""
    # force is kept for frontend/API compatibility with v0.2.x. In v0.3 all
    # lookups are local and deterministic, so there is nothing to refresh online.
    result = await MedicineLookup(hass).async_lookup(
        gtin, raw_code=raw_code, barcode_format=barcode_format
    )
    if result.data:
        return result.data, "local", result.data.get("source") or result.source
    return None, "not_found", result.error or "Данные не найдены в локальном каталоге"


@callback
def async_setup(hass: HomeAssistant) -> None:
    """Register all WebSocket commands once."""
    if hass.data.setdefault(DOMAIN, {}).get("websocket_registered"):
        return
    for command in (
        websocket_get_state,
        websocket_get_live_briefs,
        websocket_get_prices,
        websocket_parse_code,
        websocket_lookup_product,
        websocket_get_instruction,
        websocket_get_analogs,
        websocket_add_package,
        websocket_update_package,
        websocket_consume,
        websocket_adjust,
        websocket_delete_package,
        websocket_update_settings,
        websocket_get_medication_manager,
        websocket_add_to_medication_manager,
        websocket_link_medication_manager,
        websocket_unlink_medication_manager,
    ):
        websocket_api.async_register_command(hass, command)
    hass.data[DOMAIN]["websocket_registered"] = True


@websocket_api.websocket_command({"type": "medicine_cabinet/get_state"})
@websocket_api.async_response
async def websocket_get_state(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    state = _store(hass).snapshot()
    # v0.4.1: package "owner" is presented as Patient in the UI.  Keep the
    # storage key for backwards compatibility, but populate the dropdown from
    # the patients already configured in Medication Manager.
    try:
        med_snapshot = _medication_manager_snapshot(hass)
        state["patients"] = [
            str(item.get("patient") or "").strip()
            for item in med_snapshot.get("patients", [])
            if str(item.get("patient") or "").strip()
        ]
    except Exception:
        state["patients"] = []
    lookup = MedicineLookup(hass)
    state["catalog_info"] = await lookup.async_catalog_info()
    brief_map = await lookup.async_briefs(state.get("packages", []))
    for package in state.get("packages", []):
        local = brief_map.get(package.get("id"), {})
        if local:
            local.setdefault("brief_source", "Локальная база")
            local.setdefault("brief_live", False)
            package.update(local)
        custom = _custom_brief(package)
        if custom:
            package.update(custom)
    connection.send_result(msg["id"], state)


@websocket_api.websocket_command({"type": "medicine_cabinet/get_live_briefs", vol.Optional("force", default=False): bool})
@websocket_api.async_response
async def websocket_get_live_briefs(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Refresh short card instructions from the official RLS website.

    This is intentionally separate from get_state so the panel can render the
    local/offline data immediately instead of waiting for internet requests.
    """
    state = _store(hass).snapshot()
    packages = state.get("packages", [])
    local_map = await MedicineLookup(hass).async_briefs(packages)
    for package in packages:
        local = local_map.get(package.get("id"), {})
        if local:
            package.update(local)

    custom_briefs: dict[str, dict[str, Any]] = {}
    remote_packages: list[dict[str, Any]] = []
    for package in packages:
        custom = _custom_brief(package)
        package_id = str(package.get("id") or "")
        if custom and package_id:
            custom_briefs[package_id] = custom
        else:
            remote_packages.append(package)

    web_briefs = await RLSBriefFetcher(hass).async_enrich_packages(
        remote_packages, force=bool(msg.get("force", False))
    )
    web_briefs.update(custom_briefs)
    connection.send_result(msg["id"], {"briefs": web_briefs})


@websocket_api.websocket_command(
    {
        "type": "medicine_cabinet/get_prices",
        vol.Required("package_id"): str,
        vol.Optional("force", default=False): bool,
    }
)
@websocket_api.async_response
async def websocket_get_prices(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Fetch live public prices/availability from configured pharmacy networks."""
    state = _store(hass).snapshot()
    package = next((p for p in state.get("packages", []) if p.get("id") == msg["package_id"]), None)
    if not package:
        connection.send_error(msg["id"], "not_found", "Упаковка не найдена")
        return
    city = str(state.get("settings", {}).get("price_city") or "Москва")
    data = await PharmacyPriceFetcher(hass).async_get_prices(
        package, city=city, force=bool(msg.get("force", False))
    )
    connection.send_result(msg["id"], data)


@websocket_api.websocket_command(
    {
        "type": "medicine_cabinet/parse_code",
        vol.Required("raw_code"): str,
        vol.Optional("format", default=""): str,
    }
)
@websocket_api.async_response
async def websocket_parse_code(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    parsed = parse_gs1_code(msg["raw_code"])
    parsed["barcode_format"] = msg.get("format") or ""
    catalog, status, source = await _lookup_catalog(
        hass,
        parsed.get("gtin"),
        force=False,
        raw_code=msg["raw_code"],
        barcode_format=msg.get("format") or "",
    )
    parsed["catalog"] = catalog
    parsed["lookup_status"] = status
    parsed["lookup_source"] = source

    duplicate = _store(hass).find_duplicate_serial(parsed.get("serial"), parsed.get("gtin"))
    if duplicate:
        parsed["duplicate_package"] = {
            "id": duplicate.get("id") or "",
            "name": duplicate.get("name") or "",
            "strength": duplicate.get("strength") or "",
            "serial": duplicate.get("serial") or "",
            "gtin": duplicate.get("gtin") or "",
            "location": duplicate.get("location") or "",
            "remaining": duplicate.get("remaining"),
            "unit": duplicate.get("unit") or "шт.",
        }
    else:
        parsed["duplicate_package"] = None

    connection.send_result(msg["id"], parsed)


@websocket_api.websocket_command(
    {
        "type": "medicine_cabinet/lookup_product",
        vol.Required("gtin"): str,
        vol.Optional("force", default=True): bool,
        vol.Optional("raw_code", default=""): str,
        vol.Optional("format", default=""): str,
    }
)
@websocket_api.async_response
async def websocket_lookup_product(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    catalog, status, source = await _lookup_catalog(
        hass,
        msg["gtin"],
        force=msg["force"],
        raw_code=msg.get("raw_code") or None,
        barcode_format=msg.get("format") or None,
    )
    connection.send_result(
        msg["id"],
        {"catalog": catalog, "lookup_status": status, "lookup_source": source},
    )


@websocket_api.websocket_command(
    {
        "type": "medicine_cabinet/get_instruction",
        vol.Required("gtin"): str,
    }
)
@websocket_api.async_response
async def websocket_get_instruction(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    result = await MedicineLookup(hass).async_instruction(msg["gtin"])
    if result.data:
        connection.send_result(msg["id"], {"instruction": result.data})
    else:
        connection.send_result(msg["id"], {"instruction": None, "error": result.error})


@websocket_api.websocket_command(
    {
        "type": "medicine_cabinet/get_analogs",
        vol.Required("gtin"): str,
    }
)
@websocket_api.async_response
async def websocket_get_analogs(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    result = await MedicineLookup(hass).async_analogs(msg["gtin"])
    if result.data:
        connection.send_result(msg["id"], {"analogs": result.data})
    else:
        connection.send_result(msg["id"], {"analogs": None, "error": result.error})


@websocket_api.websocket_command(
    {"type": "medicine_cabinet/add_package", vol.Required("package"): dict}
)
@websocket_api.async_response
async def websocket_add_package(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    try:
        result = await _store(hass).async_add_package(msg["package"])
        connection.send_result(msg["id"], result)
    except DuplicateSerialError as err:
        package = err.package
        details = " · ".join(
            part for part in (
                f"{package.get('name') or 'Препарат'} {package.get('strength') or ''}".strip(),
                str(package.get("location") or "").strip(),
            ) if part
        )
        connection.send_error(
            msg["id"],
            "duplicate_serial",
            f"Эта упаковка уже добавлена по серийному номеру{': ' + details if details else ''}",
        )


@websocket_api.websocket_command(
    {
        "type": "medicine_cabinet/update_package",
        vol.Required("package_id"): str,
        vol.Required("package"): dict,
    }
)
@websocket_api.async_response
async def websocket_update_package(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    try:
        result = await _store(hass).async_update_package(msg["package_id"], msg["package"])
        connection.send_result(msg["id"], result)
    except KeyError:
        connection.send_error(msg["id"], "not_found", "Упаковка не найдена")
    except DuplicateSerialError as err:
        package = err.package
        details = " · ".join(
            part for part in (
                f"{package.get('name') or 'Препарат'} {package.get('strength') or ''}".strip(),
                str(package.get("location") or "").strip(),
            ) if part
        )
        connection.send_error(
            msg["id"],
            "duplicate_serial",
            f"Этот серийный номер уже используется другой упаковкой{': ' + details if details else ''}",
        )


@websocket_api.websocket_command(
    {
        "type": "medicine_cabinet/consume",
        vol.Required("package_id"): str,
        vol.Required("amount"): vol.Coerce(float),
        vol.Optional("reason", default="Принято"): str,
    }
)
@websocket_api.async_response
async def websocket_consume(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    try:
        result = await _store(hass).async_consume(
            msg["package_id"], msg["amount"], msg["reason"]
        )
        connection.send_result(msg["id"], result)
    except KeyError:
        connection.send_error(msg["id"], "not_found", "Упаковка не найдена")


@websocket_api.websocket_command(
    {
        "type": "medicine_cabinet/adjust",
        vol.Required("package_id"): str,
        vol.Required("remaining"): vol.Coerce(float),
        vol.Optional("reason", default="Корректировка"): str,
    }
)
@websocket_api.async_response
async def websocket_adjust(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    try:
        result = await _store(hass).async_adjust(
            msg["package_id"], msg["remaining"], msg["reason"]
        )
        connection.send_result(msg["id"], result)
    except KeyError:
        connection.send_error(msg["id"], "not_found", "Упаковка не найдена")


@websocket_api.websocket_command(
    {"type": "medicine_cabinet/delete_package", vol.Required("package_id"): str}
)
@websocket_api.async_response
async def websocket_delete_package(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    try:
        await _store(hass).async_delete_package(msg["package_id"])
        connection.send_result(msg["id"], {"success": True})
    except KeyError:
        connection.send_error(msg["id"], "not_found", "Упаковка не найдена")


@websocket_api.websocket_command(
    {"type": "medicine_cabinet/update_settings", vol.Required("settings"): dict}
)
@websocket_api.async_response
async def websocket_update_settings(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    result = await _store(hass).async_update_settings(msg["settings"])
    connection.send_result(msg["id"], result)

def _medication_manager_snapshot(hass: HomeAssistant, package: dict[str, Any] | None = None) -> dict[str, Any]:
    managers = hass.data.get("medication_manager", {})
    if not isinstance(managers, dict):
        managers = {}
    patients = []
    for entry_id, manager in managers.items():
        if not hasattr(manager, "medications") or not hasattr(manager, "patient_name"):
            continue
        meds = []
        for med in manager.medications:
            try:
                effective_supply = float(manager.effective_supply(med)) if hasattr(manager, "effective_supply") else float(med.supply)
            except Exception:
                effective_supply = float(getattr(med, "supply", 0) or 0)
            score = 0
            if package:
                pname = str(package.get("name") or "").casefold().replace("ё", "е").replace("®", "").strip()
                mname = str(getattr(med, "name", "") or "").casefold().replace("ё", "е").replace("®", "").strip()
                pstrength = str(package.get("strength") or "").casefold().strip()
                mstrength = str(getattr(med, "dosage", "") or "").casefold().strip()
                if pname and mname and pname == mname:
                    score += 70
                elif pname and mname and (pname in mname or mname in pname):
                    score += 45
                if pstrength and mstrength and pstrength == mstrength:
                    score += 30
            meds.append({
                "id": med.id,
                "name": med.name,
                "dosage": med.dosage,
                "form": getattr(med, "form", ""),
                "active_ingredient": getattr(med, "active_ingredient", ""),
                "times": list(med.times or []),
                "time_mode": med.time_mode,
                "supply": effective_supply,
                "units_per_dose": med.units_per_dose,
                "active": med.active,
                "paused": med.paused,
                "cabinet_linked": bool(getattr(med, "cabinet_linked", False)),
                "cabinet_package_id": str(getattr(med, "cabinet_package_id", "") or ""),
                "match_score": score,
            })
        meds.sort(key=lambda x: (-x["match_score"], x["name"].casefold(), x["dosage"].casefold()))
        patients.append({"entry_id": str(entry_id), "patient": str(manager.patient_name), "medications": meds})
    patients.sort(key=lambda x: x["patient"].casefold())
    return {
        "available": bool(patients) and hass.services.has_service("medication_manager", "add_medication"),
        "bridge_version": 1,
        "patients": patients,
    }


@websocket_api.websocket_command({
    "type": "medicine_cabinet/get_medication_manager",
    vol.Optional("package_id", default=""): str,
})
@websocket_api.async_response
async def websocket_get_medication_manager(hass, connection, msg):
    package = _store(hass).data.get("packages", {}).get(msg.get("package_id", ""))
    connection.send_result(msg["id"], _medication_manager_snapshot(hass, package))


@websocket_api.websocket_command({
    "type": "medicine_cabinet/add_to_medication_manager",
    vol.Required("package_id"): str,
    vol.Required("patient"): str,
    vol.Required("times"): [str],
    vol.Optional("time_mode", default="fixed"): vol.In(["fixed", "schedule"]),
    vol.Optional("units_per_dose", default=1.0): vol.Coerce(float),
    vol.Optional("interval_days", default=1): vol.Coerce(int),
    vol.Optional("permanent", default=True): bool,
    vol.Optional("duration_days", default=30): vol.Coerce(int),
    vol.Optional("low_supply_days", default=7): vol.Coerce(int),
})
@websocket_api.async_response
async def websocket_add_to_medication_manager(hass, connection, msg):
    package = _store(hass).data.get("packages", {}).get(msg["package_id"])
    if package is None:
        connection.send_error(msg["id"], "not_found", "Упаковка не найдена")
        return
    if not hass.services.has_service("medication_manager", "add_medication"):
        connection.send_error(msg["id"], "not_available", "Обнови Medication Manager до версии с поддержкой Домашней аптечки")
        return
    times = [str(x).strip() for x in msg.get("times", []) if str(x).strip()]
    if not times:
        connection.send_error(msg["id"], "invalid_times", "Укажи хотя бы одно время приёма")
        return
    data = {
        "patient": msg["patient"],
        "name": package.get("name") or "Лекарство",
        "dosage": package.get("strength") or "",
        "form": package.get("form") or "",
        "active_ingredient": package.get("active_ingredient") or "",
        "note": "Добавлено из Домашней аптечки",
        "start_date": "",
        "permanent": bool(msg.get("permanent", True)),
        "duration_days": max(1, int(msg.get("duration_days", 30))),
        "interval_days": max(1, int(msg.get("interval_days", 1))),
        "times": times,
        "time_mode": msg.get("time_mode", "fixed"),
        "supply": max(0.0, float(package.get("remaining") or 0)),
        "units_per_dose": max(0.01, float(msg.get("units_per_dose", 1.0))),
        "low_supply_days": max(0, int(msg.get("low_supply_days", 7))),
        "active": True,
        "paused": False,
        "cabinet_linked": True,
        "cabinet_package_id": package.get("id") or "",
        "cabinet_gtin": package.get("gtin") or "",
        "cabinet_name": package.get("name") or "",
        "cabinet_strength": package.get("strength") or "",
    }
    await hass.services.async_call("medication_manager", "add_medication", data, blocking=True)
    connection.send_result(msg["id"], _medication_manager_snapshot(hass, package))


@websocket_api.websocket_command({
    "type": "medicine_cabinet/link_medication_manager",
    vol.Required("package_id"): str,
    vol.Required("patient"): str,
    vol.Required("medication_id"): str,
})
@websocket_api.async_response
async def websocket_link_medication_manager(hass, connection, msg):
    package = _store(hass).data.get("packages", {}).get(msg["package_id"])
    if package is None:
        connection.send_error(msg["id"], "not_found", "Упаковка не найдена")
        return
    if not hass.services.has_service("medication_manager", "link_cabinet"):
        connection.send_error(msg["id"], "not_available", "Обнови Medication Manager до версии с поддержкой Домашней аптечки")
        return
    await hass.services.async_call("medication_manager", "link_cabinet", {
        "patient": msg["patient"],
        "medication_id": msg["medication_id"],
        "package_id": package.get("id") or "",
        "gtin": package.get("gtin") or "",
        "name": package.get("name") or "",
        "strength": package.get("strength") or "",
    }, blocking=True)
    connection.send_result(msg["id"], _medication_manager_snapshot(hass, package))


@websocket_api.websocket_command({
    "type": "medicine_cabinet/unlink_medication_manager",
    vol.Required("package_id"): str,
    vol.Required("patient"): str,
    vol.Required("medication_id"): str,
})
@websocket_api.async_response
async def websocket_unlink_medication_manager(hass, connection, msg):
    if not hass.services.has_service("medication_manager", "unlink_cabinet"):
        connection.send_error(msg["id"], "not_available", "Medication Manager не поддерживает отвязку")
        return
    await hass.services.async_call("medication_manager", "unlink_cabinet", {
        "patient": msg["patient"], "medication_id": msg["medication_id"]
    }, blocking=True)
    package = _store(hass).data.get("packages", {}).get(msg["package_id"])
    connection.send_result(msg["id"], _medication_manager_snapshot(hass, package))

