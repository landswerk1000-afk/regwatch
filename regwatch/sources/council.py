"""Совет Федерации: новости и одобренные законы (поздние стадии по законопроектам ГД)."""
from __future__ import annotations

import re

from .base import Source, make_item, links, absolutize
from ..util import squeeze, strip_html

BASE = "http://council.gov.ru"


# Раздел /activity/legislation/ на портале умер — отдаёт «хост недоступен».
# Живой раздел с постановлениями об одобрении федеральных законов лежит
# в /activity/documents/. Лента новостей полезна отдельно, но это пресса:
# её вес в оценке ниже, и путать одно с другим нельзя.
PAGES = {
    "/events/news/": ("news", None),
    "/activity/documents/": ("law", "одобрен Советом Федерации"),
}


def _collect(http, source):
    out = []
    for page, (kind, stage) in PAGES.items():
        r = http.get_or_none(BASE + page)
        if not r:
            continue
        text = r.text
        for block in re.findall(r'<div[^>]+class="[^"]*hentry[^"]*"(.*?)(?=<div[^>]+class="[^"]*hentry|\Z)',
                                text, re.S)[:60]:
            m = re.search(r'<a[^>]+href="([^"]+)"[^>]*class="[^"]*(?:u-url|hentry__title)[^"]*"', block)
            if not m:
                m = re.search(r'<a[^>]+class="[^"]*(?:u-url|hentry__title)[^"]*"[^>]*href="([^"]+)"', block)
            if not m:
                m = re.search(r'<a[^>]+href="(/events/news/\d+/?)"', block)
            if not m:
                continue
            href = absolutize(BASE + page, m.group(1))
            title_m = re.search(r'class="[^"]*(?:p-name|hentry__title)[^"]*"[^>]*>(.*?)</', block, re.S)
            title = squeeze(strip_html(title_m.group(1)), 400) if title_m else ""
            if not title:
                anchors = links(block, r".", 15, 400)
                title = anchors[0][1] if anchors else ""
            if not title:
                continue
            date_m = re.search(r'datetime="([^"]+)"', block) or re.search(r'class="[^"]*dt-published[^"]*"[^>]*>(.*?)</', block, re.S)
            # У постановлений даты в разметке нет, зато она стоит первой
            # в заголовке: «24 июля 2026 г. № 362-СФ О Федеральном законе…».
            # Без неё документ выглядит свежим, и двухмесячное одобрение
            # улетает как срочное.
            published = squeeze(strip_html(date_m.group(1))) if date_m else None
            if not published:
                head = re.match(r"\s*(\d{1,2}\s+[а-яё]+\s+\d{4})\s*г?\.?\s*", title, re.I)
                if head:
                    published = head.group(1)
                    # Дата уходит в поле даты, и в заголовке ей больше не место:
                    # в отчёте она и так стоит строкой выше.
                    title = title[head.end():].lstrip()
            out.append(make_item(
                source, href, title, url=href,
                published=published,
                kind=kind, stage=stage))
    return out


SOURCES = [
    Source("sf_news", "Совет Федерации — новости и законы", "СФ", "news", _collect,
           note="Постановления об одобрении ФЗ — поздняя стадия конвейера ГД"),
]
