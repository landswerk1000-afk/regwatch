"""Минфин России: пресс-центр и документы."""
from __future__ import annotations

import re

from .base import Source, make_item, links, absolutize

BASE = "https://minfin.gov.ru"
PAGES = ("/ru/press-center/", "/ru/document/")


def _collect(http, source):
    out, seen = [], set()
    for page in PAGES:
        r = http.get_or_none(BASE + page)
        if not r:
            continue
        for href, text in links(r.text, r"press-center/\?id_4=|/document/\?id_4=", 25, 400):
            url = absolutize(BASE + page, href)
            if url in seen:
                continue
            seen.add(url)
            m = re.search(r"id_4=(\d+)", href)
            out.append(make_item(
                source, m.group(1) if m else url, text, url=url,
                kind="news" if "press-center" in href else "published_act"))
    return out


SOURCES = [
    Source("minfin", "Минфин — пресс-центр и документы", "Минфин", "news", _collect),
]
