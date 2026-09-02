#!/usr/bin/env python3
"""Build medicine_catalog.sqlite from a locally installed RLS Encyclopedia.

This tool reads only the fields needed by Home Medicine Cabinet v0.3.4.
Images/BLOB fields are intentionally not selected or copied.

Default source paths match RLS Encyclopedia 2026 on Windows:
  C:\\ProgramData\\ENC2026\\DB\\rls.sqlite
  C:\\ProgramData\\ENC2026\\DB\\rls_config.db
"""

from __future__ import annotations

import argparse
import html
import os
import re
import sqlite3
import sys
import tempfile
from collections import defaultdict
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable

SCHEMA_VERSION = "2"


class _TextExtractor(HTMLParser):
    BLOCKS = {
        "p", "div", "li", "tr", "table", "section", "article", "h1", "h2", "h3",
        "h4", "h5", "h6", "dt", "dd", "ul", "ol",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_text(value: str | None) -> str:
    if not value:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(str(value))
        parser.close()
        text = "".join(parser.parts)
    except Exception:
        text = re.sub(r"(?s)<[^>]+>", " ", str(value))
    text = html.unescape(text).replace("\xa0", " ")
    lines: list[str] = []
    for raw in text.splitlines():
        line = re.sub(r"[ \t\r\f\v]+", " ", raw).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def ro_connect(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise FileNotFoundError(path)
    # Path.as_uri is robust for Windows drive letters and Unicode paths.
    uri = path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def verify_table(conn: sqlite3.Connection, table: str) -> None:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not row:
        raise RuntimeError(f"В базе отсутствует таблица {table}")


def normalize_gtins(value: str | None) -> list[str]:
    if not value:
        return []
    out: list[str] = []
    for token in re.split(r"[,;\s]+", str(value)):
        digits = "".join(ch for ch in token if ch.isdigit())
        if len(digits) in (8, 12, 13, 14) and digits not in out:
            out.append(digits)
    return out


def classify_medicine(atc_code: str = "", active_ingredient: str = "", name: str = "") -> str:
    atc = (atc_code or "").upper().replace(" ", "")
    text = f"{active_ingredient} {name}".lower().replace("ё", "е")
    if atc.startswith("A01"): return "Зубы и рот"
    if atc.startswith(("A02", "A03", "A04", "A05", "A06", "A07", "A09")): return "Желудок, кишечник, печень"
    if atc.startswith("A10"): return "Диабет"
    if atc.startswith(("A11", "A12")): return "Витамины и минералы"
    if atc.startswith("B"): return "Кровь"
    if atc.startswith("C"): return "Сердце и давление"
    if atc.startswith("D"): return "Кожа"
    if atc.startswith("G"): return "Мочеполовая система"
    if atc.startswith("H"): return "Гормоны и эндокринология"
    if atc.startswith("J01"): return "Антибиотики и инфекции"
    if atc.startswith("J05"): return "Противовирусные"
    if atc.startswith(("J06", "L03")): return "Иммунная система"
    if atc.startswith("M"): return "Суставы и мышцы"
    if atc.startswith("N02"): return "Боль и температура"
    if atc.startswith("N"): return "Нервная система"
    if atc.startswith("P"): return "Антибиотики и инфекции"
    if atc.startswith("R06"): return "Аллергия"
    if atc.startswith("R"): return "Простуда и дыхание"
    if atc.startswith("S01"): return "Глаза"
    if atc.startswith("S02"): return "Ухо, горло, нос"
    words = [
        ("Желудок, кишечник, печень", ("омепраз", "рабепраз", "пантопраз", "фамотидин", "смекта", "панкреат", "мезим", "энтерол", "лоперамид", "эссенциал", "урсосан")),
        ("Боль и температура", ("ибупроф", "парацетам", "нимесул", "кеторол", "анальгин", "ацетилсалиц")),
        ("Аллергия", ("лоратадин", "цетириз", "дезлората", "супраст", "тавегил")),
        ("Сердце и давление", ("бисопрол", "амлодип", "лозарт", "валсарт", "эналапр", "каптопр", "аторваст", "розуваст")),
        ("Простуда и дыхание", ("ацетилцисте", "амброксол", "бромгекс", "сальбут")),
        ("Антибиотики и инфекции", ("амоксиц", "азитром", "кларитром", "цефтри", "доксицик")),
        ("Противовирусные", ("умифенов", "осельтам", "ациклов", "валациклов")),
        ("Витамины и минералы", ("витамин", "магний", "кальций", "цинк", "омега")),
    ]
    for category, keys in words:
        if any(k in text for k in keys):
            return category
    return "Другое"


def guess_unit(form: str, packing: str) -> str:
    text = f"{form} {packing}".lower().replace("ё", "е")
    if "таблет" in text: return "табл."
    if "капсул" in text: return "капс."
    if "суппоз" in text or "свеч" in text: return "супп."
    if "ампул" in text: return "амп."
    if "пакет" in text or "саше" in text: return "пак."
    if "доз" in text: return "доз."
    return "шт."


def parse_package_size(packing: str, form: str) -> tuple[float, str]:
    text = packing or ""
    m = re.search(r"№\s*(\d+(?:[.,]\d+)?)", text)
    if m:
        return float(m.group(1).replace(",", ".")), guess_unit(form, text)
    # Volume/mass fallback for bottles, tubes and solutions.
    m = re.search(r"(?<!\d)(\d+(?:[.,]\d+)?)\s*(мл|ml|г|g)(?!\w)", text, re.I)
    if m:
        return float(m.group(1).replace(",", ".")), m.group(2).lower().replace("ml", "мл").replace("g", "г")
    return 0.0, guess_unit(form, text)


def prescription_from_text(text: str, strength: str) -> str:
    low = (text or "").lower().replace("ё", "е")
    if not low:
        return ""
    strength_low = (strength or "").lower().replace("ё", "е").strip()
    chunks = [c.strip() for c in re.split(r"[\n.;]+", low) if c.strip()]
    if strength_low:
        for chunk in chunks:
            if strength_low in chunk:
                if "без рецепт" in chunk:
                    return "Без рецепта"
                if "по рецепт" in chunk:
                    return "По рецепту"
    if "без рецепт" in low and "по рецепт" not in low:
        return "Без рецепта"
    if "по рецепт" in low and "без рецепт" not in low:
        return "По рецепту"
    return ""


def positive_desc(rel: tuple[int, int, int] | None) -> tuple[int, str]:
    if not rel:
        return 0, ""
    desc_id, instruction_id, aphs_desc_id = rel
    if instruction_id > 0:
        return instruction_id, "instruction"
    if desc_id > 0:
        return desc_id, "description"
    if aphs_desc_id > 0:
        return aphs_desc_id, "active_substance_description"
    return 0, ""


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA journal_mode=DELETE;
        PRAGMA synchronous=OFF;
        PRAGMA temp_store=MEMORY;

        CREATE TABLE meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE medicines (
            id INTEGER PRIMARY KEY,
            rls_drug_id INTEGER,
            rls_tradename_id INTEGER,
            rls_slug TEXT,
            name TEXT NOT NULL,
            strength TEXT,
            form TEXT,
            manufacturer TEXT,
            active_ingredient TEXT,
            atc_code TEXT,
            atc_name TEXT,
            pharm_group TEXT,
            category TEXT,
            package_size REAL NOT NULL DEFAULT 0,
            unit TEXT NOT NULL DEFAULT 'шт.',
            packing_name TEXT,
            shelf_life TEXT,
            shelf_life_months REAL NOT NULL DEFAULT 0,
            storage_conditions TEXT,
            prescription TEXT,
            source_description_id INTEGER NOT NULL DEFAULT 0,
            source_kind TEXT,
            instruction_available INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE reference_medicines (
            id INTEGER PRIMARY KEY,
            rls_tradename_id INTEGER,
            rls_slug TEXT,
            name TEXT NOT NULL,
            strength TEXT,
            form TEXT,
            manufacturer TEXT,
            active_ingredient TEXT,
            atc_code TEXT,
            atc_name TEXT,
            pharm_group TEXT,
            category TEXT
        );
        CREATE INDEX idx_reference_name ON reference_medicines(name);
        CREATE INDEX idx_reference_tradename ON reference_medicines(rls_tradename_id);
        CREATE INDEX idx_reference_active ON reference_medicines(active_ingredient);
        CREATE INDEX idx_reference_atc ON reference_medicines(atc_code);
        CREATE INDEX idx_reference_pharm_group ON reference_medicines(pharm_group);

        CREATE TABLE barcodes (
            gtin TEXT NOT NULL,
            medicine_id INTEGER NOT NULL,
            PRIMARY KEY (gtin, medicine_id),
            FOREIGN KEY (medicine_id) REFERENCES medicines(id)
        );
        CREATE INDEX idx_barcodes_gtin ON barcodes(gtin);
        CREATE INDEX idx_medicines_name ON medicines(name);
        CREATE INDEX idx_medicines_tradename ON medicines(rls_tradename_id);
        CREATE INDEX idx_medicines_active ON medicines(active_ingredient);
        CREATE INDEX idx_medicines_atc ON medicines(atc_code);
        CREATE INDEX idx_medicines_pharm_group ON medicines(pharm_group);

        CREATE TABLE instructions (
            id INTEGER PRIMARY KEY,
            object_type INTEGER,
            actdate TEXT,
            composition TEXT,
            dosage_form_description TEXT,
            pharmacodynamics TEXT,
            pharmacokinetics TEXT,
            indications TEXT,
            contraindications TEXT,
            dosage TEXT,
            side_effects TEXT,
            interactions TEXT,
            overdose TEXT,
            special_instructions TEXT,
            package_info TEXT,
            pregnancy TEXT,
            manufacturer_info TEXT,
            dispensing_conditions TEXT
        );
        """
    )


def chunks(values: list[int], size: int = 800) -> Iterable[list[int]]:
    for i in range(0, len(values), size):
        yield values[i : i + size]


def main() -> int:
    parser = argparse.ArgumentParser(description="Конвертер локальной базы РЛС для Home Medicine Cabinet v0.3.4")
    parser.add_argument("--rls", default=r"C:\ProgramData\ENC2026\DB\rls.sqlite", help="Путь к rls.sqlite")
    parser.add_argument("--config", default=r"C:\ProgramData\ENC2026\DB\rls_config.db", help="Путь к rls_config.db")
    parser.add_argument("--out", default="medicine_catalog.sqlite", help="Итоговый каталог")
    args = parser.parse_args()

    rls_path = Path(args.rls)
    cfg_path = Path(args.config)
    out_path = Path(args.out).resolve()

    print("Home Medicine Cabinet — RLS converter")
    print(f"RLS:      {rls_path}")
    print(f"Config:   {cfg_path}")
    print(f"Output:   {out_path}")
    print("Images:   skipped")

    try:
        rls = ro_connect(rls_path)
        cfg = ro_connect(cfg_path)
    except Exception as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 2

    try:
        for table in ("packings", "drugs", "tradenames", "dosageforms", "aphs", "firms", "expirations", "storageconditions", "drugs_atc", "atc", "drugs_pharmgroups", "pharmgroups", "descriptions"):
            verify_table(rls, table)
        for table in ("packingdescriptions", "aphsdescriptions"):
            verify_table(cfg, table)

        packing_desc: dict[int, tuple[int, int, int]] = {}
        for row in cfg.execute("SELECT id, desc_id, instruction_id, aphs_desc_id FROM packingdescriptions"):
            packing_desc[int(row["id"])] = (
                int(row["desc_id"] or 0),
                int(row["instruction_id"] or 0),
                int(row["aphs_desc_id"] or 0),
            )
        aphs_desc = {
            int(row["id"]): int(row["desc_id"] or 0)
            for row in cfg.execute("SELECT id, desc_id FROM aphsdescriptions")
        }

        atc_map: dict[int, tuple[str, str]] = {}
        for row in rls.execute(
            """
            SELECT da.drug_id,
                   GROUP_CONCAT(DISTINCT a.code) AS codes,
                   GROUP_CONCAT(DISTINCT a.name) AS names
            FROM drugs_atc da
            JOIN atc a ON a.id=da.atc_id
            GROUP BY da.drug_id
            """
        ):
            atc_map[int(row["drug_id"])] = (row["codes"] or "", row["names"] or "")

        pharm_map: dict[int, str] = {}
        for row in rls.execute(
            """
            SELECT dp.drug_id, GROUP_CONCAT(DISTINCT pg.name) AS names
            FROM drugs_pharmgroups dp
            JOIN pharmgroups pg ON pg.id=dp.pharmgroup_id
            GROUP BY dp.drug_id
            """
        ):
            pharm_map[int(row["drug_id"])] = row["names"] or ""

        rows = list(
            rls.execute(
                """
                SELECT
                    p.id AS packing_id, p.drug_id, p.ean_code, p.name AS packing_name,
                    d.tradename_id, d.aphs_id, d.dosage,
                    t.name AS trade_name, t.slug AS rls_slug,
                    df.name AS dosage_form,
                    a.name AS active_substance,
                    f.name AS manufacturer,
                    e.name AS shelf_life, e.inmonth AS shelf_life_months,
                    sc.name AS storage_conditions
                FROM packings p
                JOIN drugs d ON d.id=p.drug_id
                LEFT JOIN tradenames t ON t.id=d.tradename_id
                LEFT JOIN dosageforms df ON df.id=d.dosageform_id
                LEFT JOIN aphs a ON a.id=d.aphs_id
                LEFT JOIN firms f ON f.id=d.firm_id
                LEFT JOIN expirations e ON e.id=p.expiration_id
                LEFT JOIN storageconditions sc ON sc.id=p.storagecondition_id
                WHERE p.ean_code IS NOT NULL AND TRIM(p.ean_code) <> ''
                ORDER BY p.id
                """
            )
        )
        print(f"Packings with barcode: {len(rows)}")

        # Best reusable description for each trade name. This is the fallback
        # discovered during v0.3 research (e.g. Razo 20 mg can reuse a common
        # Razo description attached to another package of the same trade name).
        fallback_by_tradename: dict[int, tuple[tuple[int, int, int], int]] = {}
        quality_by_tradename: dict[int, int] = defaultdict(int)
        # Consider every RLS packing here, not only packings that have a barcode.
        # The shared description may be attached to a sibling packing without EAN.
        for row in rls.execute(
            "SELECT p.id AS packing_id, d.tradename_id "
            "FROM packings p JOIN drugs d ON d.id=p.drug_id"
        ):
            tid = int(row["tradename_id"] or 0)
            rel = packing_desc.get(int(row["packing_id"]))
            if not tid or not rel:
                continue
            desc_id, kind = positive_desc(rel)
            if not desc_id:
                continue
            quality = 3 if kind == "instruction" else 2 if kind == "description" else 1
            if quality > quality_by_tradename[tid]:
                quality_by_tradename[tid] = quality
                fallback_by_tradename[tid] = (rel, int(row["packing_id"]))

        prepared: list[dict] = []
        used_desc_ids: set[int] = set()
        for row in rows:
            item = dict(row)
            pid = int(item["packing_id"])
            tid = int(item["tradename_id"] or 0)
            aid = int(item["aphs_id"] or 0)
            relation = packing_desc.get(pid)
            desc_id, kind = positive_desc(relation)
            source_kind = f"direct_{kind}" if desc_id else ""

            if not desc_id and tid in fallback_by_tradename:
                fallback_rel, fallback_pid = fallback_by_tradename[tid]
                desc_id, kind = positive_desc(fallback_rel)
                source_kind = f"tradename_fallback_{kind}:{fallback_pid}"

            if not desc_id and aid and aphs_desc.get(aid, 0) > 0:
                desc_id = aphs_desc[aid]
                source_kind = "aphs_fallback_description"

            item["source_description_id"] = desc_id
            item["source_kind"] = source_kind
            if desc_id:
                used_desc_ids.add(desc_id)
            prepared.append(item)

        descriptions: dict[int, sqlite3.Row] = {}
        desc_columns = "object_type, object_id, actdate, f2, f3, f6, f7, f9, f10, f12, f13, f14, f16, f18, f19, f20, f22, f26"
        ids = sorted(used_desc_ids)
        for batch in chunks(ids):
            qs = ",".join("?" for _ in batch)
            for row in rls.execute(f"SELECT {desc_columns} FROM descriptions WHERE object_id IN ({qs})", batch):
                descriptions[int(row["object_id"])] = row
        print(f"Instruction/description records used: {len(descriptions)}")

        out_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=out_path.stem + "_", suffix=".tmp", dir=out_path.parent)
        os.close(fd)
        temp_path = Path(temp_name)
        temp_path.unlink(missing_ok=True)

        out = sqlite3.connect(temp_path)
        try:
            create_schema(out)
            out.executemany(
                "INSERT INTO meta(key,value) VALUES (?,?)",
                [
                    ("schema_version", SCHEMA_VERSION),
                    ("generated_at", datetime.now().astimezone().isoformat(timespec="seconds")),
                    ("source", "RLS Encyclopedia local database"),
                    ("images_included", "0"),
                ],
            )

            # Compact reference index of all RLS drug rows. This is intentionally
            # independent of EAN/GTIN so the analog list is not limited to products
            # that happen to have a barcode in packings.ean_code.
            reference_count = 0
            for drow in rls.execute(
                """
                SELECT d.id AS drug_id, d.tradename_id, d.dosage,
                       t.name AS trade_name, t.slug AS rls_slug,
                       df.name AS dosage_form, a.name AS active_substance,
                       f.name AS manufacturer
                FROM drugs d
                LEFT JOIN tradenames t ON t.id=d.tradename_id
                LEFT JOIN dosageforms df ON df.id=d.dosageform_id
                LEFT JOIN aphs a ON a.id=d.aphs_id
                LEFT JOIN firms f ON f.id=d.firm_id
                WHERE t.name IS NOT NULL AND TRIM(t.name) <> ''
                ORDER BY d.id
                """
            ):
                drug_id = int(drow["drug_id"] or 0)
                atc_code, atc_name = atc_map.get(drug_id, ("", ""))
                active = (drow["active_substance"] or "").strip()
                name = (drow["trade_name"] or "").strip()
                out.execute(
                    """
                    INSERT INTO reference_medicines(
                        id, rls_tradename_id, rls_slug, name, strength, form, manufacturer,
                        active_ingredient, atc_code, atc_name, pharm_group, category
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        drug_id, int(drow["tradename_id"] or 0),
                        (drow["rls_slug"] or "").strip().strip("/"), name,
                        (drow["dosage"] or "").strip(), (drow["dosage_form"] or "").strip(),
                        (drow["manufacturer"] or "").strip(), active, atc_code, atc_name,
                        pharm_map.get(drug_id, ""), classify_medicine(atc_code, active, name),
                    ),
                )
                reference_count += 1

            # Store each source description once, even if dozens of packings use it.
            for did, drow in descriptions.items():
                texts = {f: html_to_text(drow[f]) for f in ("f2", "f3", "f6", "f7", "f9", "f10", "f12", "f13", "f14", "f16", "f18", "f19", "f20", "f22", "f26")}
                out.execute(
                    """
                    INSERT INTO instructions(
                        id, object_type, actdate, composition, dosage_form_description,
                        pharmacodynamics, pharmacokinetics, indications, contraindications,
                        dosage, side_effects, interactions, overdose, special_instructions,
                        package_info, pregnancy, manufacturer_info, dispensing_conditions
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        did, int(drow["object_type"] or 0), drow["actdate"] or "",
                        texts["f2"], texts["f3"], texts["f6"], texts["f7"], texts["f9"],
                        texts["f10"], texts["f12"], texts["f13"], texts["f14"], texts["f16"],
                        texts["f18"], texts["f19"], texts["f20"], texts["f22"], texts["f26"],
                    ),
                )

            barcode_count = 0
            duplicate_pairs = 0
            for item in prepared:
                pid = int(item["packing_id"])
                drug_id = int(item["drug_id"] or 0)
                tid = int(item["tradename_id"] or 0)
                name = (item["trade_name"] or "").strip() or "Неизвестный препарат"
                strength = (item["dosage"] or "").strip()
                form = (item["dosage_form"] or "").strip()
                active = (item["active_substance"] or "").strip()
                atc_code, atc_name = atc_map.get(drug_id, ("", ""))
                pharm_group = pharm_map.get(drug_id, "")
                category = classify_medicine(atc_code, active, name)
                package_size, unit = parse_package_size(item["packing_name"] or "", form)
                desc_id = int(item["source_description_id"] or 0)
                drow = descriptions.get(desc_id)
                dispensing = html_to_text(drow["f26"]) if drow else ""
                prescription = prescription_from_text(dispensing, strength)

                out.execute(
                    """
                    INSERT INTO medicines(
                        id, rls_drug_id, rls_tradename_id, rls_slug, name, strength, form,
                        manufacturer, active_ingredient, atc_code, atc_name, pharm_group,
                        category, package_size, unit, packing_name, shelf_life,
                        shelf_life_months, storage_conditions, prescription,
                        source_description_id, source_kind, instruction_available
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        pid, drug_id, tid, (item.get("rls_slug") or "").strip().strip("/"), name, strength, form,
                        (item["manufacturer"] or "").strip(), active, atc_code, atc_name,
                        pharm_group, category, package_size, unit,
                        (item["packing_name"] or "").strip(), (item["shelf_life"] or "").strip(),
                        float(item["shelf_life_months"] or 0), (item["storage_conditions"] or "").strip(),
                        prescription, desc_id, item["source_kind"] or "", 1 if desc_id in descriptions else 0,
                    ),
                )
                for gtin in normalize_gtins(item["ean_code"]):
                    try:
                        out.execute("INSERT INTO barcodes(gtin,medicine_id) VALUES (?,?)", (gtin, pid))
                        barcode_count += 1
                    except sqlite3.IntegrityError:
                        duplicate_pairs += 1

            trade_name_count = out.execute(
                "SELECT COUNT(DISTINCT CASE WHEN rls_tradename_id > 0 THEN rls_tradename_id ELSE name END) FROM reference_medicines"
            ).fetchone()[0]
            official_instruction_count = out.execute(
                "SELECT COUNT(DISTINCT source_description_id) FROM medicines "
                "WHERE instruction_available=1 AND source_description_id>0 AND source_kind LIKE '%instruction%'"
            ).fetchone()[0]
            description_count = out.execute(
                "SELECT COUNT(DISTINCT source_description_id) FROM medicines "
                "WHERE instruction_available=1 AND source_description_id>0 "
                "AND (source_kind LIKE '%description%' OR source_kind LIKE '%aphs%') "
                "AND source_kind NOT LIKE '%instruction%'"
            ).fetchone()[0]
            out.execute("INSERT INTO meta(key,value) VALUES ('medicine_count',?)", (str(len(prepared)),))
            out.execute("INSERT INTO meta(key,value) VALUES ('reference_drug_count',?)", (str(reference_count),))
            out.execute("INSERT INTO meta(key,value) VALUES ('trade_name_count',?)", (str(trade_name_count),))
            out.execute("INSERT INTO meta(key,value) VALUES ('barcode_count',?)", (str(barcode_count),))
            out.execute("INSERT INTO meta(key,value) VALUES ('text_record_count',?)", (str(len(descriptions)),))
            out.execute("INSERT INTO meta(key,value) VALUES ('description_count',?)", (str(description_count),))
            out.execute("INSERT INTO meta(key,value) VALUES ('instruction_count',?)", (str(official_instruction_count),))
            out.commit()
            out.execute("VACUUM")
            out.commit()
        finally:
            out.close()

        if out_path.exists():
            out_path.unlink()
        temp_path.replace(out_path)
        size_mb = out_path.stat().st_size / (1024 * 1024)
        print("\nDone")
        print(f"Trade names:  {trade_name_count}")
        print(f"Drug index:   {reference_count}")
        print(f"Packings:     {len(prepared)}")
        print(f"Barcodes:     {barcode_count}")
        print(f"Descriptions: {description_count}")
        print(f"Instructions: {official_instruction_count}")
        print(f"Text records: {len(descriptions)}")
        if duplicate_pairs:
            print(f"Duplicate barcode pairs skipped: {duplicate_pairs}")
        print(f"Size:         {size_mb:.1f} MB")
        print(f"File:         {out_path}")
        print("\nCopy this file to Home Assistant:")
        print("  /config/medicine_cabinet/medicine_catalog.sqlite")
        return 0
    except Exception as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 3
    finally:
        rls.close()
        cfg.close()


if __name__ == "__main__":
    raise SystemExit(main())
