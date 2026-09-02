"""Short live medicine briefs from the official RLS website.

Only three small sections are read for the Home Medicine Cabinet cards:
indications, dosage, and contraindications. The full instruction is never copied
into Home Assistant; users open the official RLS page for the complete text.
Results are cached only in RAM to avoid repeated requests to RLSnet.ru.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

from aiohttp import ClientError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

RLS_HOSTS = {"rlsnet.ru", "www.rlsnet.ru"}
CACHE_TTL = 6 * 60 * 60
FAIL_TTL = 2 * 60
MAX_HTML_BYTES = 12_000_000
MAX_SECTION_CHARS = 5000

_TARGETS = {
    "indications": (
        "показания",
        "показания к применению",
    ),
    "contraindications": (
        "противопоказания",
    ),
    "dosage": (
        "способ применения и дозы",
        "дозировка",
        "способ применения",
    ),
}

_SKIP_TAGS = {"script", "style", "noscript", "svg", "nav", "header", "footer", "button"}


def _normalise(value: str) -> str:
    return " ".join((value or "").lower().replace("ё", "е").split())


def _clean_text(value: str) -> str:
    text = " ".join((value or "").replace("\xa0", " ").split())
    noise = (
        "Внимание!",
        "Информация исключительно для работников здравоохранения.",
        "Являетесь ли Вы специалистом здравоохранения?",
    )
    for item in noise:
        text = text.replace(item, " ")
    text = " ".join(text.split())
    text = re.sub(r"\s+([.,;:])", r"\1", text)
    text = re.sub(r"\.{2,}", ".", text)
    return text.strip(" .·|\n\t")


def _short(value: str, limit: int) -> str:
    text = _clean_text(value)
    if len(text) <= limit:
        return text
    cut = text[: limit + 1]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,.;:-") + "…"


def _strength_pattern(strength: str) -> re.Pattern[str] | None:
    """Return a tolerant regexp for the first numeric strength, e.g. 20 мг."""
    norm = _normalise(strength)
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*(мг|мкг|г|мл|ме|ед|%)", norm, re.I)
    if not match:
        return None
    number = re.escape(match.group(1).replace(",", "."))
    unit = re.escape(match.group(2))
    # Source may use comma or dot as decimal separator.
    number = number.replace(r"\.", r"[.,]")
    return re.compile(rf"(?<!\d){number}\s*{unit}(?![а-яa-z])", re.I)


def _dose_excerpt(text: str, strength: str) -> str:
    """Prefer the part of dosage text that mentions the package strength.

    RLS pages can contain several dosage variants under one trade-name URL. If
    the fetched section contains other explicit strengths but not the current
    one, suppress the card dosage rather than show a potentially wrong dose.
    """
    clean = _clean_text(text)
    if not clean:
        return ""
    pattern = _strength_pattern(strength)
    if pattern is None:
        return _short(clean, 300)

    target = pattern.search(clean)
    if target:
        boundary = clean.rfind(". ", 0, target.start())
        start = boundary + 2 if boundary >= 0 else 0
        end = clean.find(". ", target.end())
        if end == -1:
            end = min(len(clean), target.end() + 260)
        else:
            end += 1
        excerpt = clean[start:end].strip()
        return _short(excerpt or clean, 300)

    # If the text contains explicit dosage units but not this strength, do not
    # surface it as "how to take" for this particular package.
    if re.search(r"\d+(?:[.,]\d+)?\s*(?:мг|мкг|г|мл|ме|ед)\b", clean, re.I):
        return ""
    return _short(clean, 300)



def _fallback_parse_rls_brief(html: str, strength: str = "") -> dict[str, str]:
    """Parse RLS sections even if the site changes heading markup.

    The official page is mostly server-rendered, but heading tags/classes may
    change. This fallback converts the page to visible text and cuts sections
    between known headings.
    """
    text = re.sub(r"(?is)<(script|style|noscript|svg|nav|header|footer)[^>]*>.*?</\1>", " ", html or "")
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(?:p|div|li|h[1-6]|tr)>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    from html import unescape
    text = unescape(text).replace("\xa0", " ")
    lines = [" ".join(line.split()) for line in text.splitlines()]
    lines = [line for line in lines if line]
    norm_lines = [_normalise(line) for line in lines]
    all_heads = set()
    for variants in _TARGETS.values():
        all_heads.update(variants)
    # Other common instruction headings delimit our target sections too.
    all_heads.update({
        "действующее вещество", "atx", "фармакологическая группа", "состав",
        "описание лекарственной формы", "фармакодинамика", "фармакокинетика",
        "применение при беременности и кормлении грудью", "побочные действия",
        "взаимодействие", "передозировка", "особые указания", "форма выпуска",
        "производитель", "условия отпуска из аптек", "условия хранения", "срок годности",
    })
    out: dict[str, str] = {}
    for key, variants in _TARGETS.items():
        start = None
        for i, line in enumerate(norm_lines):
            if any(line == v or line.startswith(v + " ") for v in variants):
                start = i + 1
                break
        if start is None:
            continue
        chunk = []
        for i in range(start, min(len(lines), start + 120)):
            nl = norm_lines[i]
            if any(nl == h or nl.startswith(h + " ") for h in all_heads):
                break
            if nl in {"внимание!", "информация исключительно для работников здравоохранения.",
                      "являетесь ли вы специалистом здравоохранения?", "нет"}:
                continue
            chunk.append(lines[i])
            if sum(map(len, chunk)) > MAX_SECTION_CHARS:
                break
        out[key] = _clean_text(" ".join(chunk))
    return {
        "brief_indications": _short(out.get("indications", ""), 280),
        "brief_dosage": _dose_excerpt(out.get("dosage", ""), strength),
        "brief_contraindications": _short(out.get("contraindications", ""), 240),
    }

class _RLSSectionParser(HTMLParser):
    """Small tolerant parser for RLS instruction headings and visible text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sections: dict[str, list[str]] = {key: [] for key in _TARGETS}
        self._skip_depth = 0
        self._heading_level = 0
        self._heading_parts: list[str] = []
        self._current: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if re.fullmatch(r"h[1-6]", tag):
            self._heading_level = int(tag[1])
            self._heading_parts = []
        elif self._current and tag in {"p", "li", "br", "tr"}:
            self.sections[self._current].append(". ")
        elif self._current and tag == "div":
            self.sections[self._current].append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if re.fullmatch(r"h[1-6]", tag) and self._heading_level:
            heading = _normalise(" ".join(self._heading_parts))
            matched = None
            for key, variants in _TARGETS.items():
                if any(heading == v or heading.startswith(v + " ") for v in variants):
                    matched = key
                    break
            # RLS top-level instruction sections are normally h2. A new h1/h2
            # always ends the previous section. h3+ is allowed inside it unless
            # it is itself one of our target headings.
            if self._heading_level <= 2:
                self._current = matched
            elif matched:
                self._current = matched
            self._heading_level = 0
            self._heading_parts = []

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = data.strip()
        if not text:
            return
        if self._heading_level:
            self._heading_parts.append(text)
            return
        if self._current:
            current_size = sum(len(part) for part in self.sections[self._current])
            if current_size < MAX_SECTION_CHARS:
                self.sections[self._current].append(text)


def parse_rls_brief(html: str, strength: str = "") -> dict[str, str]:
    parser = _RLSSectionParser()
    parser.feed(html)
    raw = {key: _clean_text(" ".join(parts)) for key, parts in parser.sections.items()}
    result = {
        "brief_indications": _short(raw.get("indications", ""), 280),
        "brief_dosage": _dose_excerpt(raw.get("dosage", ""), strength),
        "brief_contraindications": _short(raw.get("contraindications", ""), 240),
    }
    if not all(result.values()):
        fallback = _fallback_parse_rls_brief(html, strength)
        for key, value in fallback.items():
            if not result.get(key) and value:
                result[key] = value
    return result


def is_specific_rls_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme == "https" and parsed.hostname in RLS_HOSTS and parsed.path.startswith("/drugs/")


class RLSBriefFetcher:
    """Fetch and RAM-cache short current instruction excerpts from RLSnet.ru."""

    def __init__(self, hass) -> None:
        self.hass = hass
        domain_data = hass.data.setdefault(DOMAIN, {})
        self._cache: dict[str, dict[str, Any]] = domain_data.setdefault("rls_web_cache", {})
        self._sem: asyncio.Semaphore = domain_data.setdefault("rls_web_semaphore", asyncio.Semaphore(3))

    async def async_enrich_packages(self, packages: list[dict[str, Any]], force: bool = False) -> dict[str, dict[str, Any]]:
        unique: dict[tuple[str, str], list[str]] = {}
        for package in packages:
            url = str(package.get("rls_url") or "")
            if not is_specific_rls_url(url):
                continue
            key = (url, str(package.get("strength") or ""))
            unique.setdefault(key, []).append(str(package.get("id") or ""))

        if not unique:
            return {}

        fetched = await asyncio.gather(
            *(self._get(url, strength, force=force) for url, strength in unique),
            return_exceptions=True,
        )
        result: dict[str, dict[str, Any]] = {}
        for ((url, strength), package_ids), item in zip(unique, fetched, strict=False):
            if isinstance(item, Exception) or not item:
                continue
            for package_id in package_ids:
                if package_id:
                    result[package_id] = dict(item)
        return result

    async def _get(self, url: str, strength: str, *, force: bool = False) -> dict[str, Any] | None:
        cache_key = f"{url}|{_normalise(strength)}"
        now = time.monotonic()
        cached = self._cache.get(cache_key)
        if (not force) and cached and now - float(cached.get("time", 0)) < float(cached.get("ttl", CACHE_TTL)):
            return cached.get("data")

        async with self._sem:
            # Re-check after waiting for the semaphore.
            cached = self._cache.get(cache_key)
            now = time.monotonic()
            if (not force) and cached and now - float(cached.get("time", 0)) < float(cached.get("ttl", CACHE_TTL)):
                return cached.get("data")
            try:
                session = async_get_clientsession(self.hass)
                headers = {
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Encoding": "gzip, deflate",
                    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.6",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                    "Referer": "https://www.rlsnet.ru/",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "same-origin",
                }

                async def _download(target: str) -> tuple[str, str]:
                    async with asyncio.timeout(18):
                        async with session.get(target, headers=headers, allow_redirects=True) as response:
                            if response.status != 200:
                                raise RuntimeError(f"RLS HTTP {response.status}")
                            body = await response.content.read(MAX_HTML_BYTES + 1)
                            if len(body) > MAX_HTML_BYTES:
                                raise RuntimeError("RLS page is unexpectedly large")
                            charset = response.charset or "utf-8"
                            return body.decode(charset, errors="replace"), str(response.url)

                try:
                    html, final_url = await _download(url)
                except RuntimeError as first_err:
                    # RLS may set anti-bot/session cookies on the home page. Warm
                    # the shared HA aiohttp session and retry the same public page.
                    if not re.search(r"RLS HTTP (401|403|429)", str(first_err)):
                        raise
                    try:
                        await _download("https://www.rlsnet.ru/")
                    except Exception:
                        pass
                    html, final_url = await _download(url)

                brief = parse_rls_brief(html, strength)
                if not any(brief.values()):
                    raise RuntimeError("RLS instruction sections not found")
                brief.update(
                    {
                        "brief_available": True,
                        "brief_source": "RLSnet.ru",
                        "brief_source_url": final_url,
                        "brief_live": True,
                    }
                )
                self._cache[cache_key] = {"time": time.monotonic(), "ttl": CACHE_TTL, "data": brief}
                return brief
            except (TimeoutError, OSError, UnicodeError, RuntimeError, ClientError) as err:
                _LOGGER.debug("Cannot refresh RLS brief %s: %s", url, err)
                failure = {
                    "brief_live": False,
                    "brief_error": str(err),
                    "brief_source_url": url,
                }
                self._cache[cache_key] = {"time": time.monotonic(), "ttl": FAIL_TTL, "data": failure}
                return failure
