"""Реестр источников. Каждый источник самодостаточен и может падать независимо."""
from __future__ import annotations

from .base import Source, SourceResult
from . import cbr, pravo, council, minfin, news, duma, duma_api

ALL_SOURCES: list[Source] = [
    *cbr.SOURCES,
    *pravo.SOURCES,
    *council.SOURCES,
    *minfin.SOURCES,
    *duma_api.SOURCES,
    *duma.SOURCES,
    *news.SOURCES,
]


def by_id(source_id: str) -> Source | None:
    return next((s for s in ALL_SOURCES if s.id == source_id), None)


def enabled(config) -> list[Source]:
    out = []
    for s in ALL_SOURCES:
        flag = config.sources.get(s.id, {})
        if flag.get("enabled", s.default_enabled):
            out.append(s)
    return out
