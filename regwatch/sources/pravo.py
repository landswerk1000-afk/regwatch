"""Официальный интернет-портал правовой информации: опубликованные акты.

Ловит конец законодательного конвейера — подписанные ФЗ, указы, акты ЦБ.
Частично компенсирует недоступность СОЗД.

Портал публикует акты всех уровней сразу, и регионы дают подавляющий объём:
из 869 собранных записей 714 оказались краевыми, областными и республиканскими
приказами о тарифах, соцподдержке и карантине. Мониторинг ведётся
по федеральному регулированию, поэтому они отсеиваются здесь же, при сборе, —
а не оценкой в ноль, как раньше. Разница существенная: неотобранное не попадает
в базу, которая после каждого прогона уезжает в репозиторий.
"""
from __future__ import annotations

import logging
import re

from .base import Source, make_item

log = logging.getLogger("regwatch")

API = "http://publication.pravo.gov.ru/api/Documents"
VIEW = "http://publication.pravo.gov.ru/document/"

# Отбираем по издателю, а не по признакам региональности. Регионов
# восемьдесят с лишним, и названий их органов — сотни: «Кабинет министров
# Чувашской Республики», «Управление Алтайского края по государственному
# регулированию цен», «Сообщение о принятии решения Томского областного
# суда». Перечислить их все нельзя, а федеральных издателей — можно,
# это закрытый и узнаваемый список.
#
# Проверено на 869 собранных записях: 234 различных издателя не прошли отбор,
# и все до одного региональные или местные. Ни один документ с оценкой выше
# порога отчёта при этом не потерян.
ФЕДЕРАЛЬНЫЕ = (
    "российской федерации",   # министерства, суды, указы Президента
    "федеральн",              # федеральные законы, агентства, службы
    " россии",                # ведомственная краткая форма: «Минфина России»
    "банка россии",
    "центрального банка",
    "президента",
    "правительства рф",
)


def _federal(title: str) -> bool:
    """Федеральный ли акт. Решаем по тексту до «от <дата>» — это издатель.

    «Приказ Министерства финансов Республики Алтай от 18.09.2026 № ...»:
    всё до даты — тип документа и его издатель, дальше идут номер и название.
    Название трогать нельзя: федеральный акт может упоминать любой регион.
    """
    issuer = re.split(r"\s+от\s+\d{1,2}[.\s]", title or "", maxsplit=1)[0].lower()
    return any(k in issuer for k in ФЕДЕРАЛЬНЫЕ)


def _collect(http, source):
    out, dropped = [], 0
    for index in (1, 2):
        r = http.get_or_none(f"{API}?pageSize=100&index={index}",
                             headers={"Accept": "application/json"}, retries=4)
        if not r:
            continue
        try:
            data = r.json()
        except Exception:
            continue
        for it in data.get("items", []):
            eo = it.get("eoNumber") or ""
            name = it.get("complexName") or it.get("name") or ""
            if not name:
                continue
            if not _federal(name):
                dropped += 1
                continue
            out.append(make_item(
                source, eo, name,
                url=VIEW + eo if eo else None,
                summary=it.get("documentTypeName"),
                published=it.get("publishDateShort") or it.get("documentDate"),
                stage="опубликован",
                kind="published_act",
                authority_name=it.get("signatoryAuthorityName"),
                doc_type=it.get("documentTypeName"),
                number=it.get("number")))
        if not data.get("itemsTotalCount") or index * 100 >= data.get("itemsTotalCount", 0):
            break
    # Число в журнале — единственный способ заметить, что отбор стал слишком
    # строгим: если однажды он отсеет всё, это будет видно сразу.
    if dropped:
        log.info("портал опубликования: региональных и местных отсеяно %d, "
                 "федеральных собрано %d", dropped, len(out))
    return out


SOURCES = [
    Source("pravo_publication", "pravo.gov.ru — официальное опубликование",
           "Правительство", "published_act", _collect,
           note="Подписанные ФЗ, указы, зарегистрированные акты ЦБ"),
]
