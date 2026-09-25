"""Официальное API Государственной Думы. ВЫКЛЮЧЕНО: данные устарели.

Положение дел на сентябрь 2026:
  * `api.duma.gov.ru` доступен по HTTP напрямую, без прокси, — в отличие от
    СОЗД, который по HTTPS с зарубежных адресов не отвечает;
  * НО объявлением от 01.01.2026 Дума временно отключила методы «Поиск по
    законопроектам», «Программа законодательной деятельности» и «Результаты
    законодательной деятельности», отослав к СОЗД;
  * поиск при этом продолжает отвечать 200 и отдавать данные, замершие на
    14.02.2025. Это опаснее честной ошибки: агент молча уверял бы, что в
    Думе ничего не происходит.

Поэтому источник выключен, а `_collect` проверяет свежесть и отказывается
отдавать протухшее. Когда Дума закончит работы, источник оживёт сам —
править код не придётся. Законопроекты пока берём из СОЗД (`duma.py`).

Ключи (бесплатно, http://api.duma.gov.ru/key-request):
  DUMA_API_TOKEN  — API-ключ
  DUMA_APP_TOKEN  — APP-ключ (обязателен для серверных приложений)
Лимит — 50 000 обращений в сутки. Соединение идёт по HTTP, ключи передаются
открытым текстом: они низкоценные, только чтение открытых данных, но
не используйте их больше нигде.
"""
from __future__ import annotations

import os
from urllib.parse import quote, urlencode

from .base import Source, make_item
from ..util import now_utc, parse_dt, squeeze

BASE = "http://api.duma.gov.ru/api"

# Формулировки, по которым ищем в наименованиях законопроектов.
QUERIES = (
    "квалифицированных инвесторов",
    "индивидуальный инвестиционный счет",
    "паевых инвестиционных фондов",
    "цифровых финансовых активов",
    "рынке ценных бумаг",
    "долгосрочных сбережений",
    "инвестиционных фондах",
    "негосударственных пенсионных фондах",
)


def tokens() -> tuple:
    return (os.environ.get("DUMA_API_TOKEN", "").strip(),
            os.environ.get("DUMA_APP_TOKEN", "").strip())


def configured() -> bool:
    api, app = tokens()
    return bool(api and app)


def _url(request: str, **params) -> str:
    api, app = tokens()
    params["app_token"] = app
    return f"{BASE}/{quote(api)}/{request}.json?{urlencode(params)}"


def _name_of(value) -> str | None:
    """Поля API приходят то строкой, то объектом {id, name}, то списком."""
    if not value:
        return None
    if isinstance(value, dict):
        return value.get("name") or value.get("title")
    if isinstance(value, list):
        # API повторяет одного и того же депутата по разным записям (isCurrent),
        # поэтому схлопываем, сохраняя порядок
        names, seen = [], set()
        for v in value:
            n = _name_of(v)
            if n and n not in seen:
                seen.add(n)
                names.append(n)
        if len(names) > 4:
            names = names[:4] + [f"и ещё {len(names) - 4}"]
        return ", ".join(names) or None
    return str(value)


def _stage_of(law: dict) -> str | None:
    """Стадия рассмотрения: то, ради чего мониторинг и затевается."""
    ev = law.get("lastEvent") or {}
    parts = [_name_of(ev.get("stage")), _name_of(ev.get("phase")),
             squeeze(ev.get("solution"), 120)]
    text = " · ".join(p for p in parts if p)
    return squeeze(text, 220) or None


def _initiator(law: dict) -> str | None:
    subj = law.get("subject") or {}
    bits = []
    for key in ("departments", "deputies"):
        name = _name_of(subj.get(key))
        if name:
            bits.append(name)
    return squeeze(" / ".join(bits), 300) or None


# Если самое свежее событие в ответе старше этого срока, API считается
# несостоятельным для мониторинга: молча отдавать позапрошлогодние данные
# опаснее, чем честно сказать, что источник не работает.
MAX_STALENESS_DAYS = 120


def _collect(http, source):
    if not configured():
        raise RuntimeError(
            "нет ключей: задайте DUMA_API_TOKEN и DUMA_APP_TOKEN в ~/.regwatch.env "
            "(бесплатно на http://api.duma.gov.ru/key-request)")

    out, seen = [], set()
    newest = None
    for q in QUERIES:
        url = _url("search", name=q, sort="last_event_date")
        r = http.get_or_none(url, timeout=45, retries=2)
        if not r:
            continue
        try:
            data = r.json()
        except Exception:
            # API отдаёт человекочитаемую ошибку вместо JSON при плохом ключе
            raise RuntimeError(f"ответ не JSON (проверьте ключи): {r.text[:120]}")

        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(f"API вернуло ошибку: {str(data['error'])[:140]}")

        laws = data.get("laws") if isinstance(data, dict) else None
        if laws is None:
            laws = data if isinstance(data, list) else []

        for law in laws:
            if not isinstance(law, dict):
                continue
            number = str(law.get("number") or law.get("id") or "").strip()
            name = squeeze(law.get("name") or law.get("comments"), 500)
            if not number or not name:
                continue
            if number in seen:
                continue
            seen.add(number)

            ev = law.get("lastEvent") or {}
            # `comments` содержит уточнение «в части ...» — самое содержательное
            # в записи: именно оно говорит, что конкретно меняет законопроект.
            summary = " · ".join(x for x in (squeeze(law.get("comments"), 700),
                                             _initiator(law)) if x) or None
            # Для мониторинга свежесть определяет последнее событие по
            # законопроекту, а не дата внесения: закон 2024 года, по которому
            # вчера было чтение, — новость, а не архив.
            out.append(make_item(
                source, number, name,
                url=law.get("url") or f"https://sozd.duma.gov.ru/bill/{number}",
                summary=summary,
                published=ev.get("date") or law.get("introductionDate"),
                stage=_stage_of(law),
                kind="draft_law",
                bill_number=number,
                committee=_name_of(law.get("committees", {}).get("responsible")
                                   if isinstance(law.get("committees"), dict) else None),
                introduced=law.get("introductionDate"),
                last_event_date=ev.get("date"),
                query=q))

            ev_date = parse_dt(ev.get("date"))
            if ev_date and (newest is None or ev_date > newest):
                newest = ev_date

    # Проверка свежести. На 01.01.2026 Дума объявила метод «Поиск по
    # законопроектам» временно отключённым, и база замерла на 14.02.2025.
    # Когда работы закончат, источник оживёт сам — правок не потребуется.
    if out and newest is not None:
        age = (now_utc() - newest).days
        if age > MAX_STALENESS_DAYS:
            raise RuntimeError(
                f"API отдаёт устаревшие данные: последнее событие {newest:%d.%m.%Y} "
                f"({age} дн. назад). Дума объявила метод поиска временно отключённым — "
                f"см. api.duma.gov.ru/news. Законопроекты берём из СОЗД.")
    return out


SOURCES = [
    Source("duma_api", "Госдума — официальное API (законопроекты)", "ГД", "draft_law",
           _collect, default_enabled=False, requires_proxy=False,
           note="Метод поиска отключён Думой с 01.01.2026; оживёт сам, когда починят"),
]
