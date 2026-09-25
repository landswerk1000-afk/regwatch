"""Госдума (СОЗД) и regulation.gov.ru.

Оба портала отбрасывают TLS-рукопожатие с не-российских адресов, поэтому
источники выключены по умолчанию и требуют прокси с российским IP
(туннель через свою машину — tunnel.sh, либо `regwatch proxies`).

СОЗД ищет по полю b[Annotation] — «отрывок наименования». Таблица результатов
приходит от сервера готовой: JS нужен только для сортировки и экспорта,
поэтому обходимся разбором HTML.
"""
from __future__ import annotations

import re
from urllib.parse import quote

from .base import Source, make_item, links, absolutize
from ..util import squeeze, strip_html

SOZD = "https://sozd.duma.gov.ru"
REGULATION = "https://regulation.gov.ru"

# Отрывки наименований, по которым СОЗД ищет законопроекты.
# Формулировки подобраны так, чтобы попадать в реальные названия законов.
SOZD_QUERIES = (
    "квалифицированных инвесторов",
    "инвестиционных счет",
    "паевых инвестиционных фонд",
    "цифровых финансовых актив",
    "рынке ценных бумаг",
    "долгосрочных сбережений",
    "инвестиционных фонд",
)

_ROW = re.compile(r"<tr>(?:(?!</tr>).)*?data-law_number=.*?</tr>", re.S)
_NUM = re.compile(r'data-law_number="([^"]+)"')
_TITLE = re.compile(r'class="[^"]*o_txt[^"]*"[^>]*>\s*<div[^>]*>(.*?)</div>', re.S)
_CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_DATE = re.compile(r"\b(\d{2}\.\d{2}\.\d{4})\b")


def _clean(s: str | None) -> str:
    return squeeze(strip_html(s or ""), 400)


def _parse_rows(html_text: str, source, query: str) -> list:
    """Разбирает строки таблицы результатов СОЗД."""
    out = []
    for row in _ROW.findall(html_text):
        num_m = _NUM.search(row)
        if not num_m:
            continue
        number = num_m.group(1).strip()

        title_m = _TITLE.search(row)
        title = _clean(title_m.group(1)) if title_m else ""
        if not title:
            title = f"Законопроект {number}"

        cells = [_clean(c) for c in _CELL.findall(row)]
        # Колонки: №, карточка, дата внесения, инициатор, стадия, дата стадии
        tail = cells[2:] if len(cells) > 2 else []
        dates = [_DATE.search(c).group(1) for c in tail if _DATE.search(c)]
        submitted = dates[0] if dates else None
        # Дата последнего события важнее даты внесения: законопроект 2024 года,
        # по которому вчера было чтение, — новость, а не архив.
        stage_date = dates[-1] if len(dates) > 1 else None
        initiator = next((c for c in tail
                          if c and not _DATE.fullmatch(c or "") and "закон" not in c.lower()
                          and len(c) > 8 and not c[0].isdigit()), None)
        stage = next((c for c in tail if re.match(r"^\d+(\.\d+)?\s+\S", c or "")), None)
        archived = "В архиве" in row

        out.append(make_item(
            source, number, title,
            url=f"{SOZD}/bill/{number}",
            summary=initiator,
            published=stage_date or submitted,
            stage=squeeze(stage, 200) if stage else None,
            kind="draft_law",
            bill_number=number,
            initiator=initiator,
            introduced=submitted,
            stage_date=stage_date,
            archived="да" if archived else None,
            query=query))
    return out


def _collect_sozd(http, source):
    out, seen = [], set()
    for q in SOZD_QUERIES:
        url = (f"{SOZD}/oz?b%5BAnnotation%5D={quote(q)}&b%5Bsubmit%5D=1")
        r = http.get_or_none(url, timeout=60, retries=2)
        if not r:
            continue
        for item in _parse_rows(r.text, source, q):
            key = item["external_id"]
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
    return out


def _collect_regulation(http, source):
    out = []
    r = http.get_or_none(f"{REGULATION}/projects", timeout=60, retries=2)
    if not r:
        return out
    for href, text in links(r.text, r"/projects#npa=|/p/\d+|/Projects/List", 25, 500):
        url = absolutize(REGULATION, href)
        out.append(make_item(source, url, text, url=url, kind="draft_act",
                             stage="общественное обсуждение"))
    return out


SOURCES = [
    Source("sozd", "Госдума — СОЗД (законопроекты)", "ГД", "draft_law",
           _collect_sozd, default_enabled=False, requires_proxy=True,
           note="Нужен российский IP: портал режет TLS с зарубежных адресов"),
    Source("regulation", "regulation.gov.ru — проекты НПА ФОИВ", "Правительство", "draft_act",
           _collect_regulation, default_enabled=False, requires_proxy=True,
           note="Портал переехал на JS-приложение: страница отдаёт 673 КБ кода "
                "и 565 символов текста, ссылок на проекты в HTML нет. Сеть "
                "(прокси, редирект 308) починена — нужен разбор внутреннего API"),
]
