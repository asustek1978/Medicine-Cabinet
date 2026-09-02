"""Persistent storage for Home Medicine Cabinet."""

from __future__ import annotations

import asyncio
import calendar
from copy import deepcopy
from datetime import date, datetime
from typing import Any
from uuid import uuid4

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

from .const import DEFAULT_SETTINGS, SIGNAL_UPDATE, STORAGE_KEY, STORAGE_VERSION


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default




def _norm_text(value: Any) -> str:
    text = str(value or "").casefold().replace("ё", "е")
    text = text.replace("®", "").replace("™", "")
    text = " ".join(text.split())
    return text.strip(" .,-_;:/\\()[]{}")


def _norm_gtin(value: Any) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _norm_serial(value: Any) -> str:
    """Normalize a package serial for duplicate detection.

    Spaces and GS/FNC1 control separators are ignored because manual input and
    scanner output can represent them differently. Case is ignored for manual
    entry convenience. The original serial is still stored unchanged.
    """
    text = str(value or "").strip().replace("\x1d", "")
    return "".join(text.split()).casefold()


def _normalize_locations(values: Any, package_values: Any = ()) -> list[str]:
    """Return a stable unique storage-location list with built-in defaults."""
    ordered: list[str] = []
    seen: set[str] = set()
    sources = [DEFAULT_SETTINGS.get("storage_locations", []), values or [], package_values or []]
    for source in sources:
        if not isinstance(source, (list, tuple, set)):
            continue
        for value in source:
            name = " ".join(str(value or "").split()).strip()
            key = name.casefold()
            if not name or key in seen:
                continue
            seen.add(key)
            ordered.append(name[:80])
    return ordered


class DuplicateSerialError(ValueError):
    """Raised when the same physical package serial is already registered."""

    def __init__(self, package: dict[str, Any]) -> None:
        self.package = deepcopy(package)
        super().__init__("Упаковка с таким серийным номером уже добавлена")


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _expiry_from_yymmdd(value: str) -> str | None:
    """Convert GS1 AI 17 YYMMDD into ISO date.

    A day of 00 is interpreted as the last day of the month, which is commonly
    used for expiry-only month precision.
    """
    if len(value) != 6 or not value.isdigit():
        return None
    yy, mm, dd = int(value[:2]), int(value[2:4]), int(value[4:6])
    year = 2000 + yy if yy < 80 else 1900 + yy
    if not 1 <= mm <= 12:
        return None
    if dd == 0:
        dd = calendar.monthrange(year, mm)[1]
    try:
        return date(year, mm, dd).isoformat()
    except ValueError:
        return None


def parse_gs1_code(raw_code: str) -> dict[str, Any]:
    """Parse useful GS1 fields from a barcode result.

    Russian medicine marking Data Matrix codes do not necessarily contain an
    expiry date. When AI (17) is present we return it as the actual expiry of
    the scanned package. The catalog shelf-life value is deliberately not used
    as a substitute for that date.
    """
    raw = (raw_code or "").strip()

    # Barcode readers can return a GS1 DataMatrix symbology identifier.
    if raw.startswith("]d2") or raw.startswith("]Q3"):
        raw = raw[3:]

    # Make manual/debug copies of the FNC1 separator parse the same way as the
    # real ASCII Group Separator returned by barcode scanners.
    raw = (
        raw.replace("\\u001d", "\x1d")
        .replace("\\x1d", "\x1d")
        .replace("<GS>", "\x1d")
        .replace("<FNC1>", "\x1d")
    )

    result: dict[str, Any] = {
        "raw_code": raw_code,
        "gtin": None,
        "serial": None,
        "lot": None,
        "expiry": None,
        "expiry_source": "",
        "production_date": None,
        "best_before": None,
    }

    def set_date(ai: str, value: str) -> None:
        parsed = _expiry_from_yymmdd(value[:6])
        if not parsed:
            return
        if ai == "17":
            result["expiry"] = parsed
            result["expiry_source"] = "datamatrix_ai17"
        elif ai == "11":
            result["production_date"] = parsed
        elif ai == "15":
            result["best_before"] = parsed

    # Human-readable AI form: (01)....(21)....
    if "(01)" in raw:
        import re

        matches = re.findall(r"\((\d{2,4})\)(.*?)(?=\(\d{2,4}\)|$)", raw)
        fields = {ai: value.strip() for ai, value in matches}
        result["gtin"] = fields.get("01")
        result["serial"] = fields.get("21")
        result["lot"] = fields.get("10")
        for ai in ("17", "11", "15"):
            if fields.get(ai):
                set_date(ai, fields[ai])
        return result

    # FNC1 separators are usually returned as ASCII GS (29). Each segment may
    # still contain several fixed-length AIs before a variable-length field.
    segments = raw.split("\x1d")

    def parse_segment(segment: str) -> None:
        i = 0
        while i + 2 <= len(segment):
            ai = segment[i : i + 2]

            if ai == "01" and i + 16 <= len(segment):
                candidate = segment[i + 2 : i + 16]
                if candidate.isdigit():
                    result["gtin"] = result["gtin"] or candidate
                    i += 16
                    continue

            if ai in ("11", "15", "17") and i + 8 <= len(segment):
                value = segment[i + 2 : i + 8]
                if value.isdigit():
                    set_date(ai, value)
                    i += 8
                    continue

            # Variable-length AIs run until the next FNC1/GS separator, i.e.
            # the end of the current segment.
            if ai in ("21", "10", "91", "92"):
                value = segment[i + 2 :]
                if ai == "21":
                    result["serial"] = result["serial"] or value
                elif ai == "10":
                    result["lot"] = result["lot"] or value
                return

            i += 1

    for segment in segments:
        if segment:
            parse_segment(segment)

    # Plain EAN/GTIN fallback.
    digits = "".join(ch for ch in raw if ch.isdigit())
    if result["gtin"] is None and len(digits) in (8, 12, 13, 14):
        result["gtin"] = digits

    return result


class MedicineCabinetStore:
    """Manage catalog, package stock and history."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._lock = asyncio.Lock()
        self.data: dict[str, Any] = {}

    async def async_load(self) -> None:
        stored = await self._store.async_load()
        self.data = stored or {
            "catalog": {},
            "packages": {},
            "history": [],
            "settings": deepcopy(DEFAULT_SETTINGS),
        }
        self.data.setdefault("catalog", {})
        self.data.setdefault("packages", {})
        self.data.setdefault("history", [])
        settings = deepcopy(DEFAULT_SETTINGS)
        settings.update(self.data.get("settings", {}))
        package_locations = [
            package.get("location")
            for package in self.data.get("packages", {}).values()
            if str(package.get("location") or "").strip()
        ]
        settings["storage_locations"] = _normalize_locations(
            settings.get("storage_locations", []), package_locations
        )
        self.data["settings"] = settings

    async def _async_save(self) -> None:
        await self._store.async_save(self.data)
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)
        self.hass.bus.async_fire("medicine_cabinet_stock_changed", {})

    def _status_for(self, package: dict[str, Any]) -> dict[str, Any]:
        expiry = _parse_date(package.get("expiry"))
        today = date.today()
        warning_days = int(self.data["settings"].get("expiry_warning_days", 30))
        days_to_expiry: int | None = None
        if expiry:
            days_to_expiry = (expiry - today).days

        remaining = _safe_float(package.get("remaining"), 0)
        threshold = _safe_float(package.get("low_stock_threshold"), 0)

        expired = days_to_expiry is not None and days_to_expiry < 0
        expiring = days_to_expiry is not None and 0 <= days_to_expiry <= warning_days
        empty = remaining <= 0
        low_stock = not empty and threshold > 0 and remaining <= threshold
        shopping = empty or low_stock

        if expired:
            status = "expired"
        elif expiring:
            status = "expiring"
        elif empty:
            status = "empty"
        elif low_stock:
            status = "low_stock"
        else:
            status = "ok"

        return {
            "status": status,
            "expired": expired,
            "expiring": expiring,
            "empty": empty,
            "low_stock": low_stock,
            "shopping": shopping,
            "days_to_expiry": days_to_expiry,
        }

    def snapshot(self) -> dict[str, Any]:
        packages = []
        for package in self.data["packages"].values():
            item = deepcopy(package)
            item.update(self._status_for(item))
            packages.append(item)
        packages.sort(key=lambda p: (p.get("name") or "").lower())

        summary = {
            "total": len(packages),
            "expired": sum(1 for p in packages if p["expired"]),
            "expiring": sum(1 for p in packages if p["expiring"]),
            "low_stock": sum(1 for p in packages if p["low_stock"]),
            "empty": sum(1 for p in packages if p["empty"]),
            "shopping": sum(1 for p in packages if p["shopping"]),
        }
        return {
            "packages": packages,
            "history": list(reversed(self.data["history"][-500:])),
            "settings": deepcopy(self.data["settings"]),
            "summary": summary,
        }


    def find_duplicate_serial(
        self, serial: Any, gtin: Any = "", exclude_id: str = ""
    ) -> dict[str, Any] | None:
        """Find an already registered physical package by serial.

        If GTIN is present on both records, the serial is compared within that
        GTIN. This prevents false positives when two unrelated products happen
        to reuse the same short manufacturer serial.
        """
        wanted_serial = _norm_serial(serial)
        if not wanted_serial:
            return None
        wanted_gtin = _norm_gtin(gtin)
        for package in self.data.get("packages", {}).values():
            if exclude_id and str(package.get("id") or "") == str(exclude_id):
                continue
            if _norm_serial(package.get("serial")) != wanted_serial:
                continue
            package_gtin = _norm_gtin(package.get("gtin"))
            if wanted_gtin and package_gtin and wanted_gtin != package_gtin:
                continue
            return deepcopy(package)
        return None

    async def async_add_package(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            gtin = (payload.get("gtin") or "").strip() or None
            serial = str(payload.get("serial") or "").strip()
            duplicate = self.find_duplicate_serial(serial, gtin)
            if duplicate is not None:
                raise DuplicateSerialError(duplicate)

            package_id = uuid4().hex
            now = _now_iso()
            package = {
                "id": package_id,
                "gtin": gtin,
                "raw_code": payload.get("raw_code") or "",
                "expiry_source": payload.get("expiry_source") or "",
                "production_date": payload.get("production_date") or "",
                "best_before": payload.get("best_before") or "",
                "serial": serial,
                "lot": payload.get("lot") or "",
                "name": (payload.get("name") or "Неизвестный препарат").strip(),
                "strength": (payload.get("strength") or "").strip(),
                "form": (payload.get("form") or "").strip(),
                "manufacturer": (payload.get("manufacturer") or "").strip(),
                "active_ingredient": (payload.get("active_ingredient") or "").strip(),
                "atc_code": (payload.get("atc_code") or "").strip(),
                "atc_name": (payload.get("atc_name") or "").strip(),
                "pharm_group": (payload.get("pharm_group") or "").strip(),
                "category": (payload.get("category") or "Другое").strip(),
                "packing_name": (payload.get("packing_name") or "").strip(),
                "shelf_life": (payload.get("shelf_life") or "").strip(),
                "shelf_life_months": _safe_float(payload.get("shelf_life_months"), 0),
                "storage_conditions": (payload.get("storage_conditions") or "").strip(),
                "prescription": (payload.get("prescription") or "").strip(),
                "instruction_available": bool(payload.get("instruction_available", False)),
                "brief_custom": bool(payload.get("brief_custom", False)),
                "brief_custom_indications": (payload.get("brief_custom_indications") or "").strip(),
                "brief_custom_dosage": (payload.get("brief_custom_dosage") or "").strip(),
                "brief_custom_contraindications": (payload.get("brief_custom_contraindications") or "").strip(),
                "source": (payload.get("source") or "").strip(),
                "source_url": (payload.get("source_url") or "").strip(),
                "package_size": _safe_float(payload.get("package_size"), 0),
                "unit": (payload.get("unit") or "шт.").strip(),
                "remaining": _safe_float(payload.get("remaining"), _safe_float(payload.get("package_size"), 0)),
                "low_stock_threshold": _safe_float(payload.get("low_stock_threshold"), 0),
                "expiry": payload.get("expiry") or "",
                "owner": (payload.get("owner") or "Общее").strip(),
                "location": (payload.get("location") or "Основная аптечка").strip(),
                "instruction_url": (payload.get("instruction_url") or "").strip(),
                "notes": (payload.get("notes") or "").strip(),
                "created_at": now,
                "updated_at": now,
            }
            self.data["packages"][package_id] = package

            self._add_history("added", package, package["remaining"], "Добавлена упаковка")
            await self._async_save()
            result = deepcopy(package)
            result.update(self._status_for(result))
            return result

    async def async_update_package(self, package_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            package = self.data["packages"].get(package_id)
            if package is None:
                raise KeyError(package_id)

            candidate_serial = payload.get("serial", package.get("serial", ""))
            candidate_gtin = payload.get("gtin", package.get("gtin", ""))
            duplicate = self.find_duplicate_serial(candidate_serial, candidate_gtin, exclude_id=package_id)
            if duplicate is not None:
                raise DuplicateSerialError(duplicate)

            editable = {
                "gtin", "raw_code", "expiry_source", "production_date", "best_before", "serial", "lot", "name", "strength", "form",
                "manufacturer", "active_ingredient", "atc_code", "atc_name", "pharm_group", "category",
                "packing_name", "shelf_life", "shelf_life_months", "storage_conditions", "prescription",
                "instruction_available", "brief_custom", "brief_custom_indications", "brief_custom_dosage", "brief_custom_contraindications",
                "source", "source_url", "package_size", "unit", "remaining", "low_stock_threshold",
                "expiry", "owner", "location", "instruction_url", "notes",
            }
            numeric = {"package_size", "remaining", "low_stock_threshold", "shelf_life_months"}
            boolean = {"instruction_available", "brief_custom"}
            for key in editable:
                if key not in payload:
                    continue
                if key in numeric:
                    package[key] = _safe_float(payload[key], 0)
                elif key in boolean:
                    package[key] = bool(payload[key])
                else:
                    package[key] = payload[key] or ""
            package["updated_at"] = _now_iso()

            self._add_history("updated", package, None, "Изменены данные упаковки")
            await self._async_save()
            result = deepcopy(package)
            result.update(self._status_for(result))
            return result

    async def async_consume(self, package_id: str, amount: float, reason: str = "Принято") -> dict[str, Any]:
        async with self._lock:
            package = self.data["packages"].get(package_id)
            if package is None:
                raise KeyError(package_id)
            amount = max(0.0, _safe_float(amount, 0))
            before = _safe_float(package.get("remaining"), 0)
            actual = min(before, amount)
            package["remaining"] = max(0.0, before - actual)
            package["updated_at"] = _now_iso()
            self._add_history("consumed", package, -actual, reason)
            await self._async_save()
            result = deepcopy(package)
            result.update(self._status_for(result))
            return result

    async def async_adjust(self, package_id: str, remaining: float, reason: str = "Корректировка") -> dict[str, Any]:
        async with self._lock:
            package = self.data["packages"].get(package_id)
            if package is None:
                raise KeyError(package_id)
            before = _safe_float(package.get("remaining"), 0)
            after = max(0.0, _safe_float(remaining, 0))
            package["remaining"] = after
            package["updated_at"] = _now_iso()
            self._add_history("adjusted", package, after - before, reason)
            await self._async_save()
            result = deepcopy(package)
            result.update(self._status_for(result))
            return result

    async def async_delete_package(self, package_id: str) -> None:
        async with self._lock:
            package = self.data["packages"].pop(package_id, None)
            if package is None:
                raise KeyError(package_id)
            self._add_history("deleted", package, None, "Упаковка удалена")
            await self._async_save()

    def _linked_candidates(
        self,
        *,
        package_id: str = "",
        gtin: str = "",
        name: str = "",
        strength: str = "",
        owner: str = "",
        include_empty: bool = True,
        include_expired: bool = False,
    ) -> list[dict[str, Any]]:
        """Return physical packages matching one Medication Manager medicine.

        Identification prefers GTIN, then normalized name + strength. The
        package_id is an anchor, not a hard lock: after the first box is empty
        the bridge continues with another matching box. Patient-owned and
        common packages are eligible. Expired packages are skipped by default.
        """
        anchor = self.data.get("packages", {}).get(str(package_id or ""))
        wanted_gtin = _norm_gtin(gtin) or _norm_gtin((anchor or {}).get("gtin"))
        wanted_name = _norm_text(name) or _norm_text((anchor or {}).get("name"))
        wanted_strength = _norm_text(strength) or _norm_text((anchor or {}).get("strength"))
        wanted_owner = _norm_text(owner)
        common = {"", "общее", "общий", "common"}
        today = date.today()

        result: list[dict[str, Any]] = []
        for package in self.data.get("packages", {}).values():
            pkg_gtin = _norm_gtin(package.get("gtin"))
            pkg_name = _norm_text(package.get("name"))
            pkg_strength = _norm_text(package.get("strength"))

            if wanted_gtin:
                identity_match = bool(pkg_gtin and pkg_gtin == wanted_gtin)
            else:
                identity_match = bool(wanted_name and pkg_name == wanted_name)
                if identity_match and wanted_strength:
                    identity_match = pkg_strength == wanted_strength
            if not identity_match:
                continue

            pkg_owner = _norm_text(package.get("owner"))
            if wanted_owner and pkg_owner not in common and pkg_owner != wanted_owner:
                continue

            remaining = max(0.0, _safe_float(package.get("remaining"), 0))
            if not include_empty and remaining <= 0:
                continue

            expiry = _parse_date(str(package.get("expiry") or ""))
            if not include_expired and expiry is not None and expiry < today:
                continue
            result.append(package)

        def sort_key(package: dict[str, Any]):
            expiry = _parse_date(str(package.get("expiry") or ""))
            # FEFO: dated packages first, nearest expiry first. No-date boxes last.
            return (expiry is None, expiry or date.max, str(package.get("created_at") or ""), str(package.get("id") or ""))

        result.sort(key=sort_key)
        return result

    def stock_for_medication(
        self,
        *,
        package_id: str = "",
        gtin: str = "",
        name: str = "",
        strength: str = "",
        owner: str = "",
    ) -> dict[str, Any]:
        """Return live aggregate stock for Medication Manager."""
        candidates = self._linked_candidates(
            package_id=package_id, gtin=gtin, name=name, strength=strength, owner=owner,
            include_empty=False, include_expired=False,
        )
        remaining = sum(max(0.0, _safe_float(p.get("remaining"), 0)) for p in candidates)
        expiries = [_parse_date(str(p.get("expiry") or "")) for p in candidates]
        expiries = [x for x in expiries if x is not None]
        locations = sorted({str(p.get("location") or "").strip() for p in candidates if str(p.get("location") or "").strip()})
        return {
            "remaining": remaining,
            "packages": len(candidates),
            "next_expiry": min(expiries).isoformat() if expiries else "",
            "location": ", ".join(locations),
            "package_ids": [str(p.get("id") or "") for p in candidates],
        }

    async def async_consume_linked(
        self,
        *,
        amount: float,
        package_id: str = "",
        gtin: str = "",
        name: str = "",
        strength: str = "",
        owner: str = "",
        reason: str = "Medication Manager",
    ) -> dict[str, Any]:
        """Consume stock FEFO across matching physical packages."""
        requested = max(0.0, _safe_float(amount, 0))
        async with self._lock:
            candidates = self._linked_candidates(
                package_id=package_id, gtin=gtin, name=name, strength=strength, owner=owner,
                include_empty=False, include_expired=False,
            )
            left = requested
            consumed = 0.0
            rows: list[dict[str, Any]] = []
            for package in candidates:
                if left <= 0:
                    break
                before = max(0.0, _safe_float(package.get("remaining"), 0))
                take = min(before, left)
                if take <= 0:
                    continue
                package["remaining"] = max(0.0, before - take)
                package["updated_at"] = _now_iso()
                consumed += take
                left -= take
                self._add_history("consumed", package, -take, reason)
                rows.append({
                    "package_id": package.get("id"),
                    "name": package.get("name"),
                    "expiry": package.get("expiry"),
                    "amount": take,
                    "remaining": package.get("remaining"),
                })
            if consumed > 0:
                await self._async_save()
            stock = self.stock_for_medication(
                package_id=package_id, gtin=gtin, name=name, strength=strength, owner=owner
            )
            return {
                "requested": requested,
                "consumed": consumed,
                "shortage": max(0.0, requested - consumed),
                "stock": stock,
                "packages": rows,
            }

    async def async_update_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            allowed = {"expiry_warning_days", "notifications_enabled", "notification_hour", "price_city"}
            for key in allowed:
                if key in settings:
                    self.data["settings"][key] = settings[key]

            if "storage_locations" in settings:
                package_locations = [
                    package.get("location")
                    for package in self.data.get("packages", {}).values()
                    if str(package.get("location") or "").strip()
                ]
                self.data["settings"]["storage_locations"] = _normalize_locations(
                    settings.get("storage_locations"), package_locations
                )

            await self._async_save()
            return deepcopy(self.data["settings"])

    def _add_history(self, action: str, package: dict[str, Any], delta: float | None, note: str) -> None:
        self.data["history"].append(
            {
                "id": uuid4().hex,
                "timestamp": _now_iso(),
                "action": action,
                "package_id": package.get("id"),
                "name": package.get("name", ""),
                "strength": package.get("strength", ""),
                "delta": delta,
                "unit": package.get("unit", "шт."),
                "note": note,
            }
        )
        if len(self.data["history"]) > 2000:
            self.data["history"] = self.data["history"][-2000:]
