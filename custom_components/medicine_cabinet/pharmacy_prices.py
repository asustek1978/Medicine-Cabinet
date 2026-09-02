"""Live medicine price lookup for Medicine Cabinet.

v0.3.7 uses the public RLS "Заказ в аптеках" table as the primary source
for partner-pharmacy prices. This avoids hammering pharmacy sites that often
return anti-bot pages/401 to Home Assistant. If a requested chain is absent in
RLS for the exact package, the integration falls back to the chain's public
search page on a best-effort basis.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote_plus, urljoin, urlparse

from aiohttp import ClientError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
CACHE_TTL = 10 * 60
FAIL_TTL = 2 * 60
MAX_HTML = 3_000_000
MAX_RLS_HTML = 12_000_000
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"


def _clean(s: str) -> str:
    s = unescape(re.sub(r"<[^>]+>", " ", s or ""))
    return " ".join(s.replace("\xa0", " ").split())


def _norm(s: str) -> str:
    s = _clean(s).lower().replace("ё", "е").replace("®", "")
    s = re.sub(r"[^0-9a-zа-я%]+", " ", s)
    return " ".join(s.split())


def _query(pkg: dict[str, Any]) -> str:
    size = pkg.get("package_size")
    try:
        size_s = str(int(float(size))) if float(size) > 0 else ""
    except (TypeError, ValueError):
        size_s = ""
    unit = str(pkg.get("unit") or "") if size_s else ""
    return " ".join(x for x in [str(pkg.get("name") or ""), str(pkg.get("strength") or ""), size_s, unit] if x).strip()


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href = ""
        self._parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            self._href = dict(attrs).get("href") or ""
            self._parts = []

    def handle_data(self, data):
        if self._href and data.strip():
            self._parts.append(data.strip())

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._href:
            self.links.append((self._href, " ".join(self._parts)))
            self._href = ""
            self._parts = []


def _score(text: str, pkg: dict[str, Any]) -> int:
    hay = _norm(text)
    if not hay:
        return 0
    score = 0
    name_tokens = [t for t in _norm(str(pkg.get("name") or "")).split() if len(t) >= 3]
    for token in name_tokens:
        if token in hay:
            score += 8
    strength = _norm(str(pkg.get("strength") or ""))
    if strength and strength in hay:
        score += 7
    try:
        size = str(int(float(pkg.get("package_size") or 0)))
    except (TypeError, ValueError):
        size = ""
    if size and re.search(rf"(?<!\d){re.escape(size)}(?!\d)", hay):
        score += 5
    return score


def _extract_price(text: str) -> str:
    patterns = [
        r"(?:Цена\s*(?:от)?\s*)?([0-9][0-9\s]{0,6}(?:[.,][0-9]{1,2})?)\s*[₽Рр](?:уб\.?|\.)?",
        r"(?:price|currentPrice|finalPrice)[\"'\s:]+([0-9]{1,7}(?:[.,][0-9]{1,2})?)",
    ]
    for p in patterns:
        m = re.search(p, text, re.I)
        if m:
            value = " ".join(m.group(1).split()).replace(".", ",")
            return f"{value} ₽"
    return ""


def _price_from_link_text(text: str) -> str:
    clean = _clean(text)
    m = re.fullmatch(r"\s*([0-9]{1,7}(?:[.,][0-9]{1,2})?)\s*(?:₽|р\.?|руб\.?)?\s*", clean, re.I)
    if not m:
        return ""
    return f"{m.group(1).replace('.', ',')} ₽"


def _extract_availability(text: str) -> str:
    plain = _clean(text)
    pats = [
        r"(?:Забрать сегодня|В наличии)\s*:?\s*([0-9\s]+)\s*аптек[а-я]*",
        r"В наличии в\s*([0-9\s]+)\s*аптек[а-я]*",
        r"([0-9\s]+)\s*аптек[а-я]*\s*(?:сегодня|в наличии)",
    ]
    for p in pats:
        m = re.search(p, plain, re.I)
        if m:
            n = "".join(m.group(1).split())
            return f"в наличии: {n} аптек" if n else "в наличии"
    if re.search(r"\bв наличии\b|\bзабрать сегодня\b", plain, re.I):
        return "в наличии"
    if re.search(r"нет в наличии|нет в аптеках", plain, re.I):
        return "нет в наличии"
    return ""


def _best_link(html: str, base_url: str, pkg: dict[str, Any], allowed_host: str) -> str:
    p = _LinkParser(); p.feed(html)
    best = (0, "")
    for href, txt in p.links:
        url = urljoin(base_url, href)
        parsed = urlparse(url)
        if parsed.hostname and not parsed.hostname.endswith(allowed_host):
            continue
        sc = _score(txt, pkg)
        if sc > best[0]:
            best = (sc, url)
    return best[1] if best[0] >= 8 else ""


@dataclass(frozen=True)
class Pharmacy:
    key: str
    name: str
    icon: str
    host: str
    search: str


PHARMACIES = (
    Pharmacy("gorzdrav", "Горздрав", "🏥", "gorzdrav.org", "https://gorzdrav.org/search/?text={q}"),
    Pharmacy("planeta", "Планета здоровья", "🪐", "planetazdorovo.ru", "https://planetazdorovo.ru/search/?q={q}"),
    Pharmacy("stolichki", "Столички", "💊", "stolichki.ru", "https://stolichki.ru/search?query={q}"),
    Pharmacy("farmlend", "Фармленд", "🌿", "farmlend.ru", "https://farmlend.ru/search/?q={q}"),
)

_HOST_TO_KEY = {
    "gorzdrav.org": "gorzdrav",
    "planetazdorovo.ru": "planeta",
    "stolichki.ru": "stolichki",
    "farmlend.ru": "farmlend",
}


class _RLSOrderParser(HTMLParser):
    """Collect rows and links from the RLS 'Заказ в аптеках' table."""
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict[str, Any]] = []
        self._heading = False
        self._heading_parts: list[str] = []
        self._in_order = False
        self._row_depth = 0
        self._row_text: list[str] = []
        self._row_links: list[tuple[str, str]] = []
        self._href = ""
        self._link_parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in {"h1", "h2", "h3"}:
            self._heading = True
            self._heading_parts = []
        if self._in_order and tag == "tr":
            self._row_depth += 1
            if self._row_depth == 1:
                self._row_text = []
                self._row_links = []
        if self._in_order and self._row_depth and tag == "a":
            self._href = dict(attrs).get("href") or ""
            self._link_parts = []

    def handle_data(self, data):
        text = data.strip()
        if not text:
            return
        if self._heading:
            self._heading_parts.append(text)
        if self._in_order and self._row_depth:
            self._row_text.append(text)
            if self._href:
                self._link_parts.append(text)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in {"h1", "h2", "h3"} and self._heading:
            heading = _norm(" ".join(self._heading_parts))
            if heading.startswith("заказ в аптеках"):
                self._in_order = True
            elif self._in_order and tag in {"h1", "h2"}:
                self._in_order = False
            self._heading = False
            self._heading_parts = []
        if self._in_order and self._row_depth and tag == "a" and self._href:
            self._row_links.append((self._href, " ".join(self._link_parts)))
            self._href = ""
            self._link_parts = []
        if self._in_order and tag == "tr" and self._row_depth:
            if self._row_depth == 1:
                self.rows.append({"text": " ".join(self._row_text), "links": list(self._row_links)})
            self._row_depth -= 1


def _rls_partner_prices(html: str, base_url: str, package: dict[str, Any]) -> dict[str, dict[str, str]]:
    parser = _RLSOrderParser()
    parser.feed(html or "")
    best: dict[str, tuple[int, dict[str, str]]] = {}
    for row in parser.rows:
        score = _score(row.get("text", ""), package)
        # Require trade-name + preferably dose or package size, to avoid using
        # another presentation from the same very large RLS price table.
        if score < 8:
            continue
        for href, anchor_text in row.get("links", []):
            url = urljoin(base_url, href)
            host = (urlparse(url).hostname or "").lower()
            key = next((v for h, v in _HOST_TO_KEY.items() if host == h or host.endswith("." + h)), None)
            if not key:
                continue
            price = _price_from_link_text(anchor_text)
            if not price:
                continue
            item = {
                "price": price,
                "availability": "наличие уточнить на сайте",
                "url": url,
                "via": "RLSnet.ru",
            }
            if key not in best or score > best[key][0]:
                best[key] = (score, item)
    return {key: value for key, (_, value) in best.items()}


class PharmacyPriceFetcher:
    def __init__(self, hass) -> None:
        self.hass = hass
        data = hass.data.setdefault(DOMAIN, {})
        self.cache = data.setdefault("pharmacy_price_cache", {})
        self.sem = data.setdefault("pharmacy_price_semaphore", asyncio.Semaphore(3))

    async def async_get_prices(self, package: dict[str, Any], city: str = "Москва", force: bool = False) -> dict[str, Any]:
        query = _query(package)
        rls_prices: dict[str, dict[str, str]] = {}
        rls_error = ""
        rls_url = str(package.get("rls_url") or "")
        if rls_url.startswith("https://") and "rlsnet.ru" in rls_url:
            try:
                html, final_url = await self._get(rls_url, max_bytes=MAX_RLS_HTML, warm_host=True)
                rls_prices = _rls_partner_prices(html, final_url, package)
            except Exception as err:  # best-effort source, direct providers still run
                rls_error = str(err)
                _LOGGER.debug("RLS price table unavailable: %s", err)

        results = await asyncio.gather(*(
            self._fetch(p, package, query, city, force, rls_prices.get(p.key))
            for p in PHARMACIES
        ))
        return {
            "query": query,
            "city": city,
            "results": results,
            "updated_at": time.time(),
            "rls_prices_found": len(rls_prices),
            "rls_error": rls_error,
        }

    async def _fetch(self, pharmacy: Pharmacy, package: dict[str, Any], query: str, city: str,
                     force: bool, rls_result: dict[str, str] | None = None) -> dict[str, Any]:
        search_url = pharmacy.search.format(q=quote_plus(query))
        key = f"{pharmacy.key}|{_norm(query)}|{_norm(city)}"
        now = time.monotonic()
        cached = self.cache.get(key)
        if not force and cached and now - cached["time"] < cached["ttl"]:
            return dict(cached["data"])

        result = {
            "key": pharmacy.key, "name": pharmacy.name, "icon": pharmacy.icon,
            "url": search_url, "price": "", "availability": "", "product_name": query,
            "status": "error", "error": "", "via": "",
        }

        # RLS exposes partner prices in its public "Заказ в аптеках" table.
        # Prefer that exact-package value when available; the destination link
        # still points to the pharmacy itself.
        if rls_result:
            result.update(rls_result)
            result.update({"status": "ok", "error": "", "product_name": query})
            self.cache[key] = {"time": time.monotonic(), "ttl": CACHE_TTL, "data": result}
            return dict(result)

        async with self.sem:
            try:
                html, final_url = await self._get(search_url, max_bytes=MAX_HTML, warm_host=(pharmacy.key == "planeta"))
                candidate = _best_link(html, final_url, package, pharmacy.host)
                body = html
                page_url = final_url
                if candidate and candidate != final_url:
                    try:
                        body, page_url = await self._get(candidate, max_bytes=MAX_HTML)
                    except Exception:
                        page_url = candidate
                price = _extract_price(body)
                availability = _extract_availability(body)
                plain = _clean(body)
                raw_name = re.sub(r"[®™]", "", str(package.get("name") or "")).strip()
                match = re.search(re.escape(raw_name), plain, re.I) if raw_name else None
                if match:
                    window = plain[max(0, match.start()-220):match.start()+1300]
                    price = _extract_price(window) or price
                    availability = _extract_availability(window) or availability
                elif not candidate:
                    price = ""
                    availability = ""
                result.update({
                    "url": page_url or search_url,
                    "price": price,
                    "availability": availability,
                    "status": "ok" if (price or availability) else "partial",
                    "via": "сайт аптеки" if (price or availability) else "",
                })
                if not price and not availability:
                    result["error"] = "Цена не найдена автоматически; открой сайт аптеки"
                ttl = CACHE_TTL
            except (TimeoutError, OSError, UnicodeError, RuntimeError, ClientError) as err:
                result["error"] = str(err)
                ttl = FAIL_TTL
                _LOGGER.debug("Price lookup failed for %s: %s", pharmacy.name, err)
            self.cache[key] = {"time": time.monotonic(), "ttl": ttl, "data": result}
            return dict(result)

    async def _get(self, url: str, *, max_bytes: int, warm_host: bool = False) -> tuple[str, str]:
        session = async_get_clientsession(self.hass)
        headers = {
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5",
            "Accept-Encoding": "gzip, deflate",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
        }

        async def request(target: str) -> tuple[str, str]:
            async with asyncio.timeout(16):
                async with session.get(target, headers=headers, allow_redirects=True) as r:
                    if r.status != 200:
                        raise RuntimeError(f"HTTP {r.status}")
                    body = await r.content.read(max_bytes + 1)
                    if len(body) > max_bytes:
                        raise RuntimeError("страница слишком большая")
                    charset = r.charset or "utf-8"
                    return body.decode(charset, errors="replace"), str(r.url)

        try:
            return await request(url)
        except RuntimeError as err:
            if not warm_host or not re.search(r"HTTP (401|403|429)", str(err)):
                raise
            parsed = urlparse(url)
            root = f"{parsed.scheme}://{parsed.netloc}/"
            try:
                await request(root)
            except Exception:
                pass
            headers["Referer"] = root
            headers["Sec-Fetch-Site"] = "same-origin"
            return await request(url)
