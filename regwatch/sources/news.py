"""Деловые СМИ — обходное покрытие Государственной Думы.

СОЗД недоступен с не-российских IP, поэтому стадии чтений и внесение
законопроектов отслеживаются по профильным лентам. Вес источника понижен
(authority = СМИ), чтобы новости не забивали первоисточники.
"""
from __future__ import annotations

from .base import Source, make_item, parse_rss

FEEDS = {
    "interfax": ("https://www.interfax.ru/rss.asp", "Интерфакс"),
    "frankmedia": ("https://frankmedia.ru/feed", "Frank Media"),
    "kommersant": ("https://www.kommersant.ru/RSS/news.xml", "Коммерсантъ"),
}


def _feed(key: str):
    url, label = FEEDS[key]

    def collect(http, source):
        r = http.get(url)
        out = []
        for e in parse_rss(r.text):
            link = e.get("link") or ""
            out.append(make_item(
                source, e.get("guid") or link, e.get("title") or "", url=link,
                summary=e.get("summary"), published=e.get("published"),
                outlet=label))
        return out
    return collect


SOURCES = [
    Source(f"news_{k}", f"СМИ — {v[1]}", "СМИ", "news", _feed(k),
           default_enabled=True, note="Прокси-покрытие стадий ГД")
    for k, v in FEEDS.items()
]
