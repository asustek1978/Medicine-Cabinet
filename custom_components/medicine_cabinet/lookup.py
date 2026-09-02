"""Local medicine catalog lookup for Home Medicine Cabinet v0.3.

The catalog is generated on the user's computer from locally installed RLS
Encyclopedia databases. Home Assistant never modifies the catalog and never
automatically contacts a public medicine web site for metadata lookup. The frontend may open official RLS pages only after an explicit user click.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

CATALOG_DIR = "medicine_cabinet"
CATALOG_FILE = "medicine_catalog.sqlite"
SUPPORTED_CATALOG_SCHEMA_VERSIONS = {"1", "2"}
RLS_BASE_URL = "https://www.rlsnet.ru"

CATEGORY_OTHER = "Другое"
CATEGORIES = [
    "Боль и температура",
    "Желудок, кишечник, печень",
    "Сердце и давление",
    "Простуда и дыхание",
    "Аллергия",
    "Антибиотики и инфекции",
    "Противовирусные",
    "Нервная система",
    "Суставы и мышцы",
    "Кожа",
    "Мочеполовая система",
    "Диабет",
    "Витамины и минералы",
    "Гормоны и эндокринология",
    "Глаза",
    "Ухо, горло, нос",
    "Зубы и рот",
    "Кровь",
    "Иммунная система",
    CATEGORY_OTHER,
]


def normalize_gtin(value: str | None) -> str:
    """Return digits only."""
    return "".join(ch for ch in (value or "") if ch.isdigit())


def _short_text(value: str | None, limit: int = 180) -> str:
    """Return a compact one-line excerpt for medicine cards."""
    text = " ".join((value or "").split())
    if len(text) <= limit:
        return text
    cut = text[: limit + 1]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,.;:-") + "…"


def _norm_text(value: str | None) -> str:
    return " ".join((value or "").lower().replace("ё", "е").split())


def _safe_dosage_brief(value: str | None, strength: str | None, limit: int = 190) -> str:
    """Avoid showing a dosage excerpt that clearly belongs to another strength."""
    text = " ".join((value or "").split())
    if not text:
        return ""
    strength_norm = _norm_text(strength)
    target = re.search(r"(\d+(?:[.,]\d+)?)\s*(мг|мкг|г|мл|ме|ед|%)", strength_norm, re.I)
    if target:
        number = re.escape(target.group(1)).replace(r"\,", r"[.,]").replace(r"\.", r"[.,]")
        unit = re.escape(target.group(2))
        pattern = re.compile(rf"(?<!\d){number}\s*{unit}(?![а-яa-z])", re.I)
        if pattern.search(text):
            return _short_text(text, limit)
        if re.search(r"\d+(?:[.,]\d+)?\s*(?:мг|мкг|г|мл|ме|ед)\b", text, re.I):
            return ""
    return _short_text(text, limit)


def _rls_links(slug: str | None, tradename_id: int | str | None) -> tuple[str, str, bool]:
    """Build official RLS drug/analogs links when the catalog has the RLS slug."""
    clean_slug = (slug or "").strip().strip("/")
    try:
        tid = int(tradename_id or 0)
    except (TypeError, ValueError):
        tid = 0
    if clean_slug and tid > 0:
        return (
            f"{RLS_BASE_URL}/drugs/{clean_slug}-{tid}",
            f"{RLS_BASE_URL}/{clean_slug}-{tid}/analogs",
            True,
        )
    return (f"{RLS_BASE_URL}/", f"{RLS_BASE_URL}/analogs", False)


def classify_medicine(atc_code: str = "", active_ingredient: str = "", name: str = "") -> str:
    """Map ATC / medicine text into a friendly home-cabinet category."""
    atc = (atc_code or "").upper().replace(" ", "")
    text = f"{active_ingredient} {name}".lower().replace("ё", "е")

    if atc.startswith("A01"):
        return "Зубы и рот"
    if atc.startswith(("A02", "A03", "A04", "A05", "A06", "A07", "A09")):
        return "Желудок, кишечник, печень"
    if atc.startswith("A10"):
        return "Диабет"
    if atc.startswith(("A11", "A12")):
        return "Витамины и минералы"
    if atc.startswith("B"):
        return "Кровь"
    if atc.startswith("C"):
        return "Сердце и давление"
    if atc.startswith("D"):
        return "Кожа"
    if atc.startswith("G"):
        return "Мочеполовая система"
    if atc.startswith("H"):
        return "Гормоны и эндокринология"
    if atc.startswith("J01"):
        return "Антибиотики и инфекции"
    if atc.startswith("J05"):
        return "Противовирусные"
    if atc.startswith(("J06", "L03")):
        return "Иммунная система"
    if atc.startswith("M"):
        return "Суставы и мышцы"
    if atc.startswith("N02"):
        return "Боль и температура"
    if atc.startswith("N"):
        return "Нервная система"
    if atc.startswith("P"):
        return "Антибиотики и инфекции"
    if atc.startswith("R06"):
        return "Аллергия"
    if atc.startswith("R"):
        return "Простуда и дыхание"
    if atc.startswith("S01"):
        return "Глаза"
    if atc.startswith("S02"):
        return "Ухо, горло, нос"

    keyword_groups: list[tuple[str, tuple[str, ...]]] = [
        ("Желудок, кишечник, печень", ("омепраз", "рабепраз", "пантопраз", "фамотидин", "смекта", "панкреат", "мезим", "энтерол", "лоперамид", "эссенциал", "урсосан", "желуд", "кишеч", "печен")),
        ("Боль и температура", ("ибупроф", "парацетам", "нимесул", "кеторол", "анальгин", "ацетилсалиц", "обезбол", "температур")),
        ("Аллергия", ("лоратадин", "цетириз", "дезлората", "супраст", "тавегил", "аллерг")),
        ("Сердце и давление", ("бисопрол", "амлодип", "лозарт", "валсарт", "эналапр", "каптопр", "аторваст", "розуваст", "давлен", "сердц")),
        ("Простуда и дыхание", ("ацетилцисте", "амброксол", "бромгекс", "сальбут", "кашл", "насморк", "простуд")),
        ("Антибиотики и инфекции", ("амоксиц", "азитром", "кларитром", "цефтри", "доксицик", "антибиот")),
        ("Противовирусные", ("умифенов", "осельтам", "ациклов", "валациклов", "противовирус")),
        ("Витамины и минералы", ("витамин", "магний", "кальций", "цинк", "омега")),
        ("Суставы и мышцы", ("диклофенак", "мелоксикам", "хондро", "сустав")),
        ("Кожа", ("клотримаз", "тербинаф", "крем", "мазь", "дермат")),
        ("Диабет", ("метформ", "гликлаз", "инсулин", "диабет")),
        ("Глаза", ("офталь", "глаз", "тимолол")),
        ("Ухо, горло, нос", ("ксиломет", "оксимет", "горло", "назаль", "ушн")),
        ("Нервная система", ("фенибут", "сертралин", "амитрип", "габапент", "нерв", "успоко")),
    ]
    for category, words in keyword_groups:
        if any(word in text for word in words):
            return category
    return CATEGORY_OTHER


@dataclass(slots=True)
class LookupResult:
    """Result returned to the WebSocket layer."""

    data: dict[str, Any] | None = None
    error: str | None = None
    source: str = "Локальный каталог"


class MedicineLookup:
    """Read-only access to medicine_catalog.sqlite."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.path = Path(hass.config.path(CATALOG_DIR, CATALOG_FILE))

    async def async_lookup(
        self,
        gtin: str | None,
        *,
        raw_code: str | None = None,
        barcode_format: str | None = None,
    ) -> LookupResult:
        """Look up a medicine by exact barcode/GTIN in the local catalog."""
        digits = normalize_gtin(gtin)
        if not digits:
            return LookupResult(error="GTIN не распознан")
        return await self.hass.async_add_executor_job(self._lookup_sync, digits)

    async def async_instruction(self, gtin: str | None) -> LookupResult:
        """Return local instruction sections for a GTIN."""
        digits = normalize_gtin(gtin)
        if not digits:
            return LookupResult(error="GTIN не распознан")
        return await self.hass.async_add_executor_job(self._instruction_sync, digits)

    async def async_analogs(self, gtin: str | None) -> LookupResult:
        """Return local analog candidates plus the official RLS analogs link."""
        digits = normalize_gtin(gtin)
        if not digits:
            return LookupResult(error="GTIN не распознан")
        return await self.hass.async_add_executor_job(self._analogs_sync, digits)

    async def async_catalog_info(self) -> dict[str, Any]:
        """Return catalog installation state and live row counters."""
        if not self.path.exists():
            return {"path": str(self.path), "installed": False}
        try:
            info = await self.hass.async_add_executor_job(self._catalog_info_sync)
            info.update({"path": str(self.path), "installed": True})
            return info
        except (sqlite3.Error, RuntimeError, FileNotFoundError) as err:
            _LOGGER.exception("Cannot read local catalog statistics")
            return {"path": str(self.path), "installed": True, "error": str(err)}

    async def async_briefs(self, packages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Return compact instruction excerpts for home packages."""
        if not packages or not self.path.exists():
            return {}
        safe = [
            {
                "id": str(item.get("id") or ""),
                "gtin": normalize_gtin(item.get("gtin")),
                "name": str(item.get("name") or ""),
                "strength": str(item.get("strength") or ""),
            }
            for item in packages
            if item.get("id")
        ]
        try:
            return await self.hass.async_add_executor_job(self._briefs_sync, safe)
        except (sqlite3.Error, RuntimeError, FileNotFoundError):
            _LOGGER.exception("Cannot read compact medicine instructions")
            return {}

    def _connect(self) -> sqlite3.Connection:
        if not self.path.exists():
            raise FileNotFoundError(str(self.path))
        conn = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        schema = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if not schema or str(schema[0]) not in SUPPORTED_CATALOG_SCHEMA_VERSIONS:
            conn.close()
            raise RuntimeError("Неподдерживаемая версия medicine_catalog.sqlite")
        return conn

    @staticmethod
    def _has_table(conn: sqlite3.Connection, table: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone() is not None

    @staticmethod
    def _has_rls_slug(conn: sqlite3.Connection) -> bool:
        return any(str(row[1]) == "rls_slug" for row in conn.execute("PRAGMA table_info(medicines)"))

    @classmethod
    def _slug_expr(cls, conn: sqlite3.Connection, alias: str = "m") -> str:
        return f"{alias}.rls_slug" if cls._has_rls_slug(conn) else "''"

    def _catalog_info_sync(self) -> dict[str, Any]:
        with closing(self._connect()) as conn:
            meta = {str(row["key"]): str(row["value"]) for row in conn.execute("SELECT key,value FROM meta")}
            medicine_rows = int(conn.execute("SELECT COUNT(*) FROM medicines").fetchone()[0])
            if self._has_table(conn, "reference_medicines"):
                reference_drugs = int(conn.execute("SELECT COUNT(*) FROM reference_medicines").fetchone()[0])
                trade_names = int(conn.execute(
                    "SELECT COUNT(DISTINCT CASE WHEN rls_tradename_id > 0 THEN rls_tradename_id ELSE name END) FROM reference_medicines"
                ).fetchone()[0])
            else:
                reference_drugs = medicine_rows
                trade_names = int(conn.execute(
                    "SELECT COUNT(DISTINCT CASE WHEN rls_tradename_id > 0 THEN rls_tradename_id ELSE name END) FROM medicines"
                ).fetchone()[0])
            barcode_count = int(conn.execute("SELECT COUNT(DISTINCT gtin) FROM barcodes").fetchone()[0])
            text_records = int(conn.execute("SELECT COUNT(*) FROM instructions").fetchone()[0])
            official_instructions = int(conn.execute(
                "SELECT COUNT(DISTINCT source_description_id) FROM medicines "
                "WHERE instruction_available=1 AND source_description_id>0 AND source_kind LIKE '%instruction%'"
            ).fetchone()[0])
            descriptions = int(conn.execute(
                "SELECT COUNT(DISTINCT source_description_id) FROM medicines "
                "WHERE instruction_available=1 AND source_description_id>0 "
                "AND (source_kind LIKE '%description%' OR source_kind LIKE '%aphs%') "
                "AND source_kind NOT LIKE '%instruction%'"
            ).fetchone()[0])
            with_instruction = int(conn.execute(
                "SELECT COUNT(*) FROM medicines WHERE instruction_available=1"
            ).fetchone()[0])
            return {
                "schema_version": meta.get("schema_version", ""),
                "generated_at": meta.get("generated_at", ""),
                "source": meta.get("source", ""),
                "medicines": trade_names,
                "medicine_rows": medicine_rows,
                "reference_drugs": reference_drugs,
                "barcodes": barcode_count,
                "descriptions": descriptions,
                "instructions": official_instructions,
                "text_records": text_records,
                "medicine_rows_with_text": with_instruction,
            }

    def _briefs_sync(self, packages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        if not packages:
            return result
        with closing(self._connect()) as conn:
            slug_expr = self._slug_expr(conn)
            gtins = sorted({item["gtin"] for item in packages if item.get("gtin")})
            by_gtin: dict[str, sqlite3.Row] = {}
            if gtins:
                for start in range(0, len(gtins), 500):
                    batch = gtins[start:start+500]
                    marks = ",".join("?" for _ in batch)
                    rows = conn.execute(
                        f"""
                        SELECT b.gtin, m.name, m.strength, m.instruction_available,
                               m.rls_tradename_id, {slug_expr} AS rls_slug,
                               i.indications, i.dosage, i.contraindications
                        FROM barcodes b
                        JOIN medicines m ON m.id=b.medicine_id
                        LEFT JOIN instructions i ON i.id=m.source_description_id
                        WHERE b.gtin IN ({marks})
                        """,
                        batch,
                    )
                    for row in rows:
                        by_gtin.setdefault(str(row["gtin"]), row)

            name_cache: dict[tuple[str, str], sqlite3.Row | None] = {}
            for item in packages:
                row = by_gtin.get(item.get("gtin") or "")
                if row is None and item.get("name"):
                    key = (_norm_text(item.get("name")), _norm_text(item.get("strength")))
                    if key not in name_cache:
                        name, strength = item.get("name") or "", item.get("strength") or ""
                        if strength:
                            row2 = conn.execute(
                                f"""
                                SELECT b.gtin, m.name, m.strength, m.instruction_available,
                                       m.rls_tradename_id, {slug_expr} AS rls_slug,
                                       i.indications, i.dosage, i.contraindications
                                FROM medicines m
                                LEFT JOIN barcodes b ON b.medicine_id=m.id
                                LEFT JOIN instructions i ON i.id=m.source_description_id
                                WHERE lower(m.name)=lower(?) AND lower(COALESCE(m.strength,''))=lower(?)
                                ORDER BY m.instruction_available DESC, m.id
                                LIMIT 1
                                """,
                                (name, strength),
                            ).fetchone()
                        else:
                            row2 = conn.execute(
                                f"""
                                SELECT b.gtin, m.name, m.strength, m.instruction_available,
                                       m.rls_tradename_id, {slug_expr} AS rls_slug,
                                       i.indications, i.dosage, i.contraindications
                                FROM medicines m
                                LEFT JOIN barcodes b ON b.medicine_id=m.id
                                LEFT JOIN instructions i ON i.id=m.source_description_id
                                WHERE lower(m.name)=lower(?)
                                ORDER BY m.instruction_available DESC, m.id
                                LIMIT 1
                                """,
                                (name,),
                            ).fetchone()
                        name_cache[key] = row2
                    row = name_cache[key]
                if row is None:
                    continue
                indications = _short_text(row["indications"], 190)
                dosage = _safe_dosage_brief(row["dosage"], item.get("strength") or row["strength"], 190)
                contraindications = _short_text(row["contraindications"], 170)
                rls_url, rls_analogs_url, rls_specific = _rls_links(
                    row["rls_slug"], row["rls_tradename_id"]
                )
                result[item["id"]] = {
                    "instruction_available": bool(row["instruction_available"]),
                    "instruction_gtin": str(row["gtin"] or item.get("gtin") or ""),
                    "brief_indications": indications,
                    "brief_dosage": dosage,
                    "brief_contraindications": contraindications,
                    "brief_available": bool(indications or dosage or contraindications),
                    "rls_url": rls_url,
                    "rls_analogs_url": rls_analogs_url,
                    "rls_specific_url": rls_specific,
                }
        return result

    def _lookup_sync(self, gtin: str) -> LookupResult:
        try:
            with closing(self._connect()) as conn:
                slug_expr = self._slug_expr(conn)
                row = conn.execute(
                    f"""
                    SELECT
                        m.id, m.rls_tradename_id, {slug_expr} AS rls_slug,
                        m.name, m.strength, m.form, m.manufacturer,
                        m.active_ingredient, m.atc_code, m.atc_name,
                        m.pharm_group, m.category, m.package_size, m.unit,
                        m.packing_name, m.shelf_life, m.shelf_life_months,
                        m.storage_conditions, m.prescription,
                        m.instruction_available, m.source_description_id, m.source_kind
                    FROM barcodes b
                    JOIN medicines m ON m.id = b.medicine_id
                    WHERE b.gtin = ?
                    LIMIT 1
                    """,
                    (gtin,),
                ).fetchone()
                if row is None:
                    return LookupResult(error=f"GTIN {gtin} отсутствует в локальном каталоге")

                data = dict(row)
                rls_url, rls_analogs_url, rls_specific = _rls_links(
                    data.get("rls_slug"), data.get("rls_tradename_id")
                )
                data.update(
                    {
                        "gtin": gtin,
                        "source": "Локальный каталог RLS",
                        "source_url": rls_url if rls_specific else "",
                        "instruction_url": rls_url if rls_specific else "",
                        "rls_url": rls_url,
                        "rls_analogs_url": rls_analogs_url,
                        "rls_specific_url": rls_specific,
                    }
                )
                if not data.get("category"):
                    data["category"] = classify_medicine(
                        data.get("atc_code", ""),
                        data.get("active_ingredient", ""),
                        data.get("name", ""),
                    )
                return LookupResult(data=data)
        except FileNotFoundError:
            return LookupResult(
                error=(
                    "Каталог не установлен. Скопируй medicine_catalog.sqlite в "
                    "/config/medicine_cabinet/"
                )
            )
        except (sqlite3.Error, RuntimeError) as err:
            _LOGGER.exception("Cannot read medicine catalog")
            return LookupResult(error=f"Ошибка локального каталога: {err}")

    def _instruction_sync(self, gtin: str) -> LookupResult:
        try:
            with closing(self._connect()) as conn:
                slug_expr = self._slug_expr(conn)
                row = conn.execute(
                    f"""
                    SELECT
                        m.name, m.strength, m.active_ingredient,
                        m.rls_tradename_id, {slug_expr} AS rls_slug,
                        m.source_kind, m.source_description_id,
                        i.composition, i.dosage_form_description,
                        i.pharmacodynamics, i.pharmacokinetics,
                        i.indications, i.contraindications, i.dosage,
                        i.side_effects, i.interactions, i.overdose,
                        i.special_instructions, i.package_info, i.pregnancy,
                        i.manufacturer_info, i.dispensing_conditions
                    FROM barcodes b
                    JOIN medicines m ON m.id = b.medicine_id
                    LEFT JOIN instructions i ON i.id = m.source_description_id
                    WHERE b.gtin = ?
                    LIMIT 1
                    """,
                    (gtin,),
                ).fetchone()
                if row is None:
                    return LookupResult(error="Препарат отсутствует в локальном каталоге")
                data = dict(row)
                rls_url, rls_analogs_url, rls_specific = _rls_links(
                    data.get("rls_slug"), data.get("rls_tradename_id")
                )
                data.update(
                    {
                        "gtin": gtin,
                        "source": "Локальная база RLS",
                        "rls_url": rls_url,
                        "rls_analogs_url": rls_analogs_url,
                        "rls_specific_url": rls_specific,
                    }
                )
                sections = (
                    "composition", "dosage_form_description", "pharmacodynamics",
                    "pharmacokinetics", "indications", "contraindications", "dosage",
                    "side_effects", "interactions", "overdose", "special_instructions",
                    "package_info", "pregnancy", "manufacturer_info", "dispensing_conditions",
                )
                if not any((data.get(key) or "").strip() for key in sections):
                    # Keep official RLS links even when the local catalog has no text.
                    data["local_instruction_missing"] = True
                return LookupResult(data=data)
        except FileNotFoundError:
            return LookupResult(error="medicine_catalog.sqlite не установлен")
        except (sqlite3.Error, RuntimeError) as err:
            _LOGGER.exception("Cannot read local medicine instruction")
            return LookupResult(error=f"Ошибка локальной инструкции: {err}")

    def _analogs_sync(self, gtin: str) -> LookupResult:
        """Find local candidates by active ingredient and ATC.

        This is a reference list, not a recommendation to replace a medicine.
        Dose, dosage form, route, contraindications and prescription status can differ.
        """
        try:
            with closing(self._connect()) as conn:
                slug_expr = self._slug_expr(conn)
                target = conn.execute(
                    f"""
                    SELECT m.id, m.rls_tradename_id, {slug_expr} AS rls_slug,
                           m.name, m.strength, m.form, m.manufacturer,
                           m.active_ingredient, m.atc_code, m.atc_name, m.pharm_group
                    FROM barcodes b
                    JOIN medicines m ON m.id=b.medicine_id
                    WHERE b.gtin=?
                    LIMIT 1
                    """,
                    (gtin,),
                ).fetchone()
                if target is None:
                    return LookupResult(error="Препарат отсутствует в локальном каталоге")

                target_data = dict(target)
                rls_url, rls_analogs_url, rls_specific = _rls_links(
                    target_data.get("rls_slug"), target_data.get("rls_tradename_id")
                )
                target_tid = int(target_data.get("rls_tradename_id") or 0)
                target_name = _norm_text(target_data.get("name"))
                target_strength = _norm_text(target_data.get("strength"))
                target_form = _norm_text(target_data.get("form"))
                target_active = _norm_text(target_data.get("active_ingredient"))
                target_codes = {
                    part.strip().upper().replace(" ", "")
                    for part in str(target_data.get("atc_code") or "").split(",")
                    if part.strip()
                }

                analog_table = "reference_medicines" if self._has_table(conn, "reference_medicines") else "medicines"
                analog_slug_expr = (
                    "m.rls_slug"
                    if analog_table == "reference_medicines"
                    else self._slug_expr(conn)
                )
                prescription_expr = "'' AS prescription" if analog_table == "reference_medicines" else "m.prescription"
                select_cols = f"""
                    m.rls_tradename_id, {analog_slug_expr} AS rls_slug,
                    m.name, m.strength, m.form, m.manufacturer,
                    m.active_ingredient, m.atc_code, m.atc_name, m.pharm_group,
                    {prescription_expr}
                """

                active_rows: list[sqlite3.Row] = []
                if target_active:
                    active_rows = list(
                        conn.execute(
                            f"SELECT {select_cols} FROM {analog_table} m "
                            "WHERE lower(trim(COALESCE(m.active_ingredient,'')))=lower(trim(?)) "
                            "ORDER BY m.name LIMIT 1200",
                            (target_data.get("active_ingredient") or "",),
                        )
                    )

                atc_rows: list[sqlite3.Row] = []
                if target_codes:
                    # Pull candidates broadly, then require an exact ATC token in Python.
                    clauses = " OR ".join("upper(replace(COALESCE(m.atc_code,''),' ','')) LIKE ?" for _ in target_codes)
                    params = [f"%{code}%" for code in sorted(target_codes)]
                    atc_rows = list(
                        conn.execute(
                            f"SELECT {select_cols} FROM {analog_table} m WHERE {clauses} ORDER BY m.name LIMIT 1800",
                            params,
                        )
                    )

                def candidate_key(row: sqlite3.Row) -> str:
                    tid = int(row["rls_tradename_id"] or 0)
                    return f"id:{tid}" if tid else f"name:{_norm_text(row['name'])}"

                def is_target(row: sqlite3.Row) -> bool:
                    tid = int(row["rls_tradename_id"] or 0)
                    if target_tid and tid == target_tid:
                        return True
                    return _norm_text(row["name"]) == target_name

                def row_to_item(row: sqlite3.Row) -> dict[str, Any]:
                    url, analog_url, specific = _rls_links(row["rls_slug"], row["rls_tradename_id"])
                    return {
                        "name": row["name"] or "",
                        "strength": row["strength"] or "",
                        "form": row["form"] or "",
                        "manufacturer": row["manufacturer"] or "",
                        "active_ingredient": row["active_ingredient"] or "",
                        "atc_code": row["atc_code"] or "",
                        "atc_name": row["atc_name"] or "",
                        "pharm_group": row["pharm_group"] or "",
                        "prescription": row["prescription"] or "",
                        "same_strength": bool(target_strength and _norm_text(row["strength"]) == target_strength),
                        "same_form": bool(target_form and _norm_text(row["form"]) == target_form),
                        "rls_url": url,
                        "rls_analogs_url": analog_url,
                        "rls_specific_url": specific,
                    }

                def dedupe_rank(rows: list[sqlite3.Row], *, exact_atc: bool = False, limit: int = 16) -> list[dict[str, Any]]:
                    best: dict[str, sqlite3.Row] = {}
                    best_score: dict[str, int] = {}
                    for row in rows:
                        if is_target(row):
                            continue
                        if exact_atc:
                            row_codes = {
                                part.strip().upper().replace(" ", "")
                                for part in str(row["atc_code"] or "").split(",")
                                if part.strip()
                            }
                            if not target_codes.intersection(row_codes):
                                continue
                        key = candidate_key(row)
                        score = 0
                        if target_strength and _norm_text(row["strength"]) == target_strength:
                            score += 4
                        if target_form and _norm_text(row["form"]) == target_form:
                            score += 3
                        if target_codes:
                            row_codes = {
                                part.strip().upper().replace(" ", "")
                                for part in str(row["atc_code"] or "").split(",")
                                if part.strip()
                            }
                            if target_codes.intersection(row_codes):
                                score += 2
                        if key not in best or score > best_score[key]:
                            best[key] = row
                            best_score[key] = score
                    ordered = sorted(
                        best.items(),
                        key=lambda kv: (-best_score[kv[0]], _norm_text(kv[1]["name"])),
                    )
                    return [row_to_item(row) for _, row in ordered[:limit]]

                by_active = dedupe_rank(active_rows, limit=16)
                by_atc = dedupe_rank(atc_rows, exact_atc=True, limit=16)
                active_keys = {_norm_text(item["name"]) for item in by_active}
                by_atc = [item for item in by_atc if _norm_text(item["name"]) not in active_keys]

                return LookupResult(
                    data={
                        "gtin": gtin,
                        "name": target_data.get("name") or "",
                        "strength": target_data.get("strength") or "",
                        "form": target_data.get("form") or "",
                        "active_ingredient": target_data.get("active_ingredient") or "",
                        "atc_code": target_data.get("atc_code") or "",
                        "atc_name": target_data.get("atc_name") or "",
                        "pharm_group": target_data.get("pharm_group") or "",
                        "by_active_ingredient": by_active,
                        "by_atc": by_atc,
                        "rls_url": rls_url,
                        "rls_analogs_url": rls_analogs_url,
                        "rls_specific_url": rls_specific,
                        "source": "Локальный каталог + ссылки на RLSnet.ru",
                    }
                )
        except FileNotFoundError:
            return LookupResult(error="medicine_catalog.sqlite не установлен")
        except (sqlite3.Error, RuntimeError) as err:
            _LOGGER.exception("Cannot calculate medicine analogs")
            return LookupResult(error=f"Ошибка поиска аналогов: {err}")

