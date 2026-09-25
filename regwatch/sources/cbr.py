"""Банк России: проекты НПА, пресс-релизы, события, стратегические документы."""
from __future__ import annotations

import re

from .base import Source, make_item, parse_rss, links, absolutize
from ..util import squeeze, strip_html

BASE = "https://www.cbr.ru"


def _rss(feed: str, stage: str | None = None):
    def collect(http, source):
        r = http.get(f"{BASE}/rss/{feed}")
        out = []
        for e in parse_rss(r.text):
            link = e.get("link") or ""
            ext = e.get("guid") or link or e.get("title") or ""
            out.append(make_item(
                source, ext, e.get("title") or "", url=link,
                summary=e.get("summary"), published=e.get("published"), stage=stage))
        return out
    return collect


def _collect_project_na(http, source):
    """Проекты НПА Банка России.

    На странице заголовки обезличены («Проект указания Банка России»),
    а полная тема есть в RSS. Сшиваем по числовому id из якоря #a_NNNN,
    добавляя со страницы срок публичного обсуждения и адрес для замечаний.
    """
    titles: dict[str, dict] = {}
    rss = http.get_or_none(f"{BASE}/rss/project")
    if rss:
        for e in parse_rss(rss.text):
            link = e.get("link") or ""
            m = re.search(r"#a_(\d+)", link)
            if m:
                titles[m.group(1)] = {"title": e.get("title") or "",
                                      "published": e.get("published"),
                                      "link": link}

    r = http.get(f"{BASE}/project_na/")
    out, current = [], None
    for b in re.split(r'<div class="document-regular"', r.text)[1:]:
        name_m = re.search(r'class="[^"]*document-regular_name[^"]*"[^>]*>(.*?)</div>', b, re.S)
        if not name_m:
            continue
        name = squeeze(strip_html(name_m.group(1)), 400)
        if not name or name.startswith(("План подготовки", "Рекомендуемый образец",
                                        "Федеральный портал", "Личный кабинет", "Перечень инсайдерской")):
            continue
        com_m = re.search(r'class="[^"]*document-regular_comment[^"]*"[^>]*>(.*?)</div>', b, re.S)
        comment = squeeze(strip_html(com_m.group(1)), 600) if com_m else ""
        href_m = re.search(r'href="(/Queries/XsltBlock/File/[^"]+)"', b)
        href = absolutize(BASE, href_m.group(1)) if href_m else None
        doc_id = None
        if href_m:
            id_m = re.search(r"/File/\d+/(\d+)", href_m.group(1))
            doc_id = id_m.group(1) if id_m else None

        if name.startswith("Пояснительная записка"):
            if current is not None:
                dl = re.search(r"по\s+(\d{1,2}\s+\S+\s+\d{4})\s*год", comment)
                if dl:
                    current["meta"]["comments_until"] = dl.group(1)
                em = re.search(r"[\w.\-]+@cbr\.ru", comment)
                if em:
                    current["meta"]["contact"] = em.group(0)
                # Срок и адрес уже вынесены в отдельные поля — вычищаем их из
                # текста, иначе в отчёте одно и то же написано дважды.
                # Режем от слова «Комментарии» до конца: всё содержание этого
                # предложения уже разложено по полям. Обрывать по точке нельзя —
                # точка есть внутри адреса (cbr.ru), и оставался огрызок «ru».
                trimmed = re.sub(r"Комментарии\b.*$", "", comment, flags=re.I | re.S)
                trimmed = re.sub(r"\s{2,}", " ", trimmed).strip(" .;·")
                current["summary"] = squeeze(
                    " ".join(filter(None, [current.get("summary"), trimmed])), 1200) or None
                if href:
                    current["meta"]["explanatory_note"] = href
            continue

        enriched = titles.get(doc_id or "", {})
        full_title = enriched.get("title") or name
        current = make_item(
            source, doc_id or href or name, full_title,
            url=enriched.get("link") or href,
            summary=comment, published=enriched.get("published"),
            stage="публичное обсуждение", doc_file=href, cbr_id=doc_id)
        out.append(current)
    return out


def _collect_strategy(http, source):
    """ОНРФР и сопутствующие стратегические документы."""
    out = []
    for page in ("/about_br/publ/onfinmarket/", "/develop/"):
        r = http.get_or_none(BASE + page)
        if not r:
            continue
        for href, text in links(r.text, r"\.pdf$|/Content/Document/File/", 25, 400):
            if not re.search(r"основные направления|доклад|стратег|консультац",
                             text, re.I):
                continue
            url = absolutize(BASE + page, href)
            # ОНРФР «на 2027 год» публикуется осенью предыдущего года —
            # восстанавливаем дату, иначе архив за все годы выглядит свежим
            # «на 2027 год» и «на период 2019-2021 годов» — берём первый год
            year_m = re.search(r"на\s+(?:период\s+)?(\d{4})", text)
            published = None
            if year_m:
                published = f"{int(year_m.group(1)) - 1}-09-01"
            out.append(make_item(
                source, url, text, url=url, kind="strategy",
                summary="Стратегический документ Банка России",
                stage="опубликован", published=published,
                period=year_m.group(1) if year_m else None))
    return out


SOURCES = [
    Source("cbr_project", "ЦБ — проекты нормативных актов", "ЦБ", "draft_act",
           _collect_project_na, note="Публичное обсуждение, сроки и контакты"),
    Source("cbr_project_rss", "ЦБ — проекты НПА (RSS, резерв)", "ЦБ", "draft_act",
           _rss("project", "публичное обсуждение"), default_enabled=False,
           note="Резерв на случай смены вёрстки страницы project_na"),
    Source("cbr_press", "ЦБ — пресс-релизы", "ЦБ", "press", _rss("RssPress")),
    Source("cbr_event", "ЦБ — события и комментарии", "ЦБ", "event", _rss("eventrss")),
    Source("cbr_news", "ЦБ — новое на сайте", "ЦБ", "news", _rss("RssNews"),
           default_enabled=True, note="Шумный поток, спасает фильтр"),
    Source("cbr_strategy", "ЦБ — ОНРФР и стратегические документы", "ЦБ", "strategy",
           _collect_strategy),
]
