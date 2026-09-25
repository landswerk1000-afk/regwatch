from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from ..util import item_id, iso, parse_dt, squeeze, strip_html


@dataclass
class SourceResult:
    items: list = field(default_factory=list)
    ok: bool = True
    error: str | None = None


@dataclass
class Source:
    id: str
    title: str
    authority: str          # ЦБ | ГД | Минфин | СФ | Правительство | СМИ
    kind: str               # draft_act | draft_law | strategy | published_act | press | news ...
    collect: Callable       # (http, source) -> list[dict]
    default_enabled: bool = True
    requires_proxy: bool = False
    note: str = ""

    def run(self, http) -> SourceResult:
        before = http.counters() if hasattr(http, "counters") else None
        try:
            items = self.collect(http, self) or []
        except Exception as e:  # источник не должен ронять весь прогон
            return SourceResult(items=[], ok=False, error=f"{type(e).__name__}: {e}")

        # Пустой результат сам по себе законен — у источника может не быть
        # новостей. Но если при этом НИ ОДИН запрос не прошёл, это не тишина,
        # а недоступность, и рапортовать «ok» нельзя: именно так Совет
        # Федерации месяцами выпадал из мониторинга незамеченным.
        if before is not None and not items:
            ok_n = http.counters()[0] - before[0]
            fail_n = http.counters()[1] - before[1]
            if fail_n and not ok_n:
                return SourceResult(
                    items=[], ok=False,
                    error=f"ни один запрос не прошёл (неудач: {fail_n})")
        return SourceResult(items=items, ok=True)


def make_item(source: Source, external_id: str, title: str, url: str | None = None,
              summary: str | None = None, published=None, stage: str | None = None,
              kind: str | None = None, body: str | None = None, **meta) -> dict:
    return {
        "id": item_id(source.id, external_id),
        "source": source.id,
        "authority": source.authority,
        "kind": kind or source.kind,
        "external_id": external_id,
        "url": url,
        "title": squeeze(strip_html(title), 400) or "(без заголовка)",
        "summary": squeeze(strip_html(summary), 1200) or None,
        "body": squeeze(strip_html(body), 4000) if body else None,
        "stage": stage,
        "published_at": iso(parse_dt(published)),
        "meta": {k: v for k, v in meta.items() if v not in (None, "", [])},
    }


# ---------- мини-парсеры (без внешних зависимостей) ----------

_ITEM_RX = re.compile(r"<item>(.*?)</item>", re.S | re.I)
_ENTRY_RX = re.compile(r"<entry>(.*?)</entry>", re.S | re.I)


def _tag(block: str, name: str) -> str | None:
    m = re.search(rf"<{name}[^>]*>(.*?)</{name}>", block, re.S | re.I)
    if not m:
        return None
    val = m.group(1)
    val = re.sub(r"^\s*<!\[CDATA\[|\]\]>\s*$", "", val.strip(), flags=re.S)
    return val.strip()


def parse_rss(xml: str) -> list[dict]:
    """RSS 2.0 / Atom → список словарей с ключами title, link, summary, published."""
    out = []
    blocks = _ITEM_RX.findall(xml)
    if blocks:
        for b in blocks:
            out.append({
                "title": _tag(b, "title"),
                "link": _tag(b, "link"),
                "summary": _tag(b, "description"),
                "published": _tag(b, "pubDate") or _tag(b, "dc:date"),
                "guid": _tag(b, "guid"),
            })
        return out
    for b in _ENTRY_RX.findall(xml):
        link = None
        m = re.search(r'<link[^>]+href="([^"]+)"', b, re.I)
        if m:
            link = m.group(1)
        out.append({
            "title": _tag(b, "title"), "link": link,
            "summary": _tag(b, "summary") or _tag(b, "content"),
            "published": _tag(b, "updated") or _tag(b, "published"),
            "guid": _tag(b, "id"),
        })
    return out


def links(html_text: str, href_pattern: str, min_len: int = 12,
          max_len: int = 400) -> list[tuple]:
    """Пары (href, текст) по регулярке на href. Текст очищен от тегов."""
    rx = re.compile(href_pattern)
    out, seen = [], set()
    for href, inner in re.findall(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                                  html_text, re.S | re.I):
        if not rx.search(href):
            continue
        text = squeeze(strip_html(inner))
        if not (min_len <= len(text) <= max_len):
            continue
        key = (href, text)
        if key in seen:
            continue
        seen.add(key)
        out.append((href, text))
    return out


def absolutize(base: str, href: str) -> str:
    from urllib.parse import urljoin
    return urljoin(base, href)
