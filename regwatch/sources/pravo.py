"""Официальный интернет-портал правовой информации: опубликованные акты.

Ловит конец законодательного конвейера — подписанные ФЗ, указы, акты ЦБ.
Частично компенсирует недоступность СОЗД.
"""
from __future__ import annotations

from .base import Source, make_item

API = "http://publication.pravo.gov.ru/api/Documents"
VIEW = "http://publication.pravo.gov.ru/document/"


def _collect(http, source):
    out = []
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
    return out


SOURCES = [
    Source("pravo_publication", "pravo.gov.ru — официальное опубликование",
           "Правительство", "published_act", _collect,
           note="Подписанные ФЗ, указы, зарегистрированные акты ЦБ"),
]
