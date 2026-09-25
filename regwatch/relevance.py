"""Тематический фильтр: решает, относится ли документ к частным инвестициям,
насколько он важен и насколько срочен.

Русская морфология обрабатывается основами слов в topics.json
(«квалифицированн» ловит все падежи), поэтому внешний стеммер не нужен.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .util import norm_text, now_utc, parse_dt, squeeze

URGENCY_ORDER = {"critical": 3, "high": 2, "normal": 1, "low": 0}

# Свежесть: архивные редакции не должны вытеснять действующие документы
RECENCY_STEPS = ((30, 1.0), (120, 0.97), (365, 0.85), (730, 0.55), (10**6, 0.35))


def recency_factor(published_at) -> float:
    dt = parse_dt(published_at)
    if dt is None:
        return 0.92          # дата неизвестна — лёгкий дисконт, но не приговор
    age_days = (now_utc() - dt).days
    if age_days < 0:
        return 1.0
    for limit, factor in RECENCY_STEPS:
        if age_days <= limit:
            return factor
    return 0.35


@dataclass
class Verdict:
    relevance: float
    urgency: str
    topics: list = field(default_factory=list)
    matched: list = field(default_factory=list)
    rationale: str = ""

    @property
    def relevant(self) -> bool:
        return self.relevance > 0


class Relevance:
    def __init__(self, topics_path: str | Path):
        self.path = Path(topics_path)
        self._mtime = None
        self._load()

    @staticmethod
    def _compile(pattern: str):
        """Основа слова → регулярка, терпимая к русским окончаниям.

        «программ долгосрочн сбережен» поймает «программе долгосрочных сбережений».
        Граница слева не даёт « иис» совпасть внутри другого слова.
        """
        words = [w for w in re.split(r"[^\w]+", norm_text(pattern)) if w]
        if not words:
            return None
        parts = [re.escape(w) + r"[\wа-я]*" for w in words]
        return re.compile(r"(?<![\wа-я])" + r"[\s\-\u2010-\u2015«»\"'()\[\],.:;/]+".join(parts))

    def _load(self) -> None:
        cfg = json.loads(self.path.read_text(encoding="utf-8"))
        self._mtime = self.path.stat().st_mtime
        self.topics = cfg["topics"]
        self.authority_weight = cfg.get("authority_weight", {})
        self.kind_weight = cfg.get("kind_weight", {})
        self.urgency_rules = cfg.get("urgency_rules", [])
        self.noise = cfg.get("noise", {"penalty": 0.5, "patterns": []})
        self.hard_exclude = list(cfg.get("hard_exclude", []))
        for t in self.topics:
            t["_pats"] = [(p, self._compile(p)) for p in t["patterns"]]
            t["_pats"] = [(raw, rx) for raw, rx in t["_pats"] if rx]
        self._noise_pats = [(p, self._compile(p)) for p in self.noise.get("patterns", [])]
        self._noise_pats = [(raw, rx) for raw, rx in self._noise_pats if rx]
        self._exclude_rx = [rx for rx in (self._compile(p) for p in self.hard_exclude) if rx]
        for rule in self.urgency_rules:
            rule["_rx"] = [rx for rx in (self._compile(p) for p in rule.get("any", [])) if rx]

    def _maybe_reload(self) -> None:
        try:
            if self.path.stat().st_mtime != self._mtime:
                self._load()
        except OSError:
            pass

    def score(self, item: dict) -> Verdict:
        self._maybe_reload()
        haystack = norm_text(" ".join(filter(None, [
            item.get("title"), item.get("summary"), item.get("body"),
            item.get("stage"), " ".join(str(v) for v in (item.get("meta") or {}).values()),
        ])))
        if not haystack.strip():
            return Verdict(0.0, "low", rationale="пустой текст")

        if any(rx.search(haystack) for rx in self._exclude_rx):
            return Verdict(0.0, "low", rationale="служебная страница")

        hits, matched, weights = [], [], []
        for t in self.topics:
            found = [raw for raw, rx in t["_pats"] if rx.search(haystack)]
            if found:
                hits.append(t["name"])
                matched.extend(found[:3])
                weights.append(float(t["weight"]))

        if not weights:
            return Verdict(0.0, "low", rationale="нет тематических совпадений")

        top = max(weights)
        breadth = min(len(weights) / 4.0, 1.0)          # охват нескольких тем усиливает сигнал
        kind_w = float(self.kind_weight.get(item.get("kind", ""), 0.6))
        auth_w = float(self.authority_weight.get(item.get("authority", ""), 0.7))

        score = (0.60 * top + 0.20 * breadth + 0.20 * kind_w) * auth_w
        age_w = recency_factor(item.get("published_at"))
        score *= age_w

        noise_hits = [raw for raw, rx in self._noise_pats if rx.search(haystack)]
        if noise_hits and top < 0.95:
            score *= (1.0 - float(self.noise.get("penalty", 0.5)))

        score = max(0.0, min(1.0, round(score, 3)))
        urgency = self._urgency(haystack, score, item)
        rationale = "Темы: " + ", ".join(hits[:4])
        if age_w < 0.86:
            rationale += f"; архивный документ (вес ×{age_w:.2f})"
        if noise_hits:
            rationale += f"; шумовые маркеры: {', '.join(noise_hits[:2])}"
        return Verdict(score, urgency, hits, sorted(set(matched))[:8], rationale)

    @staticmethod
    def _stage_urgency(item: dict) -> str | None:
        """Срочность по коду стадии СОЗД — надёжнее, чем по словам в тексте.

        Коды структурны: 1.x — внесение, 2.x — предварительное рассмотрение,
        3.x — чтения в ГД, 8.x — опубликование. Чтение идёт сейчас и решает
        судьбу законопроекта; опубликование уже свершилось.
        """
        stage = (item.get("stage") or "").strip()
        m = re.match(r"^(\d+)(?:\.\d+)?\s", stage)
        if not m:
            return None
        level = {"1": "high", "3": "critical", "4": "critical",
                 "5": "critical", "8": "normal"}.get(m.group(1))
        if not level:
            return None
        # Первое чтение двухлетней давности — не срочность, а история.
        dt = parse_dt(item.get("published_at"))
        if dt and (now_utc() - dt).days > Relevance.STAGE_FRESH_DAYS:
            level = {"critical": "normal", "high": "normal"}.get(level, level)
        return level

    def _urgency(self, haystack: str, score: float, item: dict | None = None) -> str:
        # У документа с кодом стадии (СОЗД) код и есть истина. Словарные
        # правила его не перебивают: в описании стадии 2.2 встречается оборот
        # «внесенного в Государственную Думу», и по словам она ошибочно
        # выглядела срочнее, чем реальное внесение месяцем позже.
        if item is not None and re.match(r"^\d+(?:\.\d+)?\s", (item.get("stage") or "").strip()):
            if score < 0.45:
                return "low"
            return self._stage_urgency(item) or "normal"

        best = "low"
        for rule in self.urgency_rules:
            if score < float(rule.get("min_relevance", 0)):
                continue
            if any(rx.search(haystack) for rx in rule["_rx"]):
                if URGENCY_ORDER[rule["urgency"]] > URGENCY_ORDER[best]:
                    best = rule["urgency"]
        if best == "low":
            best = "normal" if score >= 0.45 else "low"
        return self._age_demote(best, item)

    # Стадия и событие устаревают с разной скоростью, и мерить их одной меркой
    # нельзя. Код стадии описывает, ГДЕ законопроект находится сейчас: «первое
    # чтение» верно и через три месяца, пока не случилось второе. Правило по
    # словам ловит то, что ПРОИЗОШЛО — «одобрен», «принят», «подписан», — и
    # такая новость стареет за недели. Из-за общего порога в полгода
    # постановление СФ от 24 июля пришло в сентябре с пометкой «срочно».
    EVENT_FRESH_DAYS = 30
    STAGE_FRESH_DAYS = 180

    @classmethod
    def _age_demote(cls, level: str, item: dict | None,
                    max_days: int | None = None) -> str:
        if item is None or level not in ("critical", "high"):
            return level
        dt = parse_dt(item.get("published_at"))
        if dt and (now_utc() - dt).days > (max_days or cls.EVENT_FRESH_DAYS):
            return "normal"
        return level
