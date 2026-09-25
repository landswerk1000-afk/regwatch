"""Мелкие утилиты: время, хеши, нормализация текста."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timezone, timedelta

MSK = timezone(timedelta(hours=3))


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_msk() -> datetime:
    return datetime.now(MSK)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def parse_dt(value) -> datetime | None:
    """Терпимый разбор дат из RSS/HTML/JSON российских госсайтов."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=MSK)
    s = str(value).strip()
    if not s:
        return None

    from email.utils import parsedate_to_datetime
    try:
        dt = parsedate_to_datetime(s)
        if dt:
            return dt if dt.tzinfo else dt.replace(tzinfo=MSK)
    except Exception:
        pass

    # ISO и российские форматы. Разбираем и строку целиком, и её начало:
    # у госсайтов часто приезжает "2026-09-23T00:00:00" с лишним хвостом.
    candidates = [s]
    if "T" in s:
        candidates.append(s.split(".")[0])
    candidates.append(s[:19])
    candidates.append(s[:10])
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y",
                "%d/%m/%Y"):
        for cand in candidates:
            try:
                dt = datetime.strptime(cand.strip(), fmt)
                return dt if dt.tzinfo else dt.replace(tzinfo=MSK)
            except ValueError:
                continue

    m = re.search(r"(\d{1,2})\s+([а-яё]+)\s+(\d{4})", s, re.I)
    if m:
        months = ("январ,феврал,март,апрел,ма,июн,июл,август,сентябр,"
                  "октябр,ноябр,декабр").split(",")
        stem = m.group(2).lower()
        for i, mo in enumerate(months, 1):
            if stem.startswith(mo):
                try:
                    return datetime(int(m.group(3)), i, int(m.group(1)), tzinfo=MSK)
                except ValueError:
                    return None
    return None


_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def strip_html(s: str | None) -> str:
    if not s:
        return ""
    import html as _html
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", s)
    s = _TAG.sub(" ", s)
    s = _html.unescape(s)
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\n\s*\n\s*", "\n", s)
    return s.strip()


def squeeze(s: str | None, limit: int | None = None) -> str:
    s = _WS.sub(" ", (s or "")).strip()
    if limit and len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return s


def norm_text(s: str) -> str:
    """Нормализация для поиска: NFKC, нижний регистр, ё→е."""
    s = unicodedata.normalize("NFKC", s or "").lower().replace("ё", "е")
    return _WS.sub(" ", s)


def sha(*parts) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p or "").encode("utf-8", "ignore"))
        h.update(b"\x1f")
    return h.hexdigest()


def item_id(source: str, external_id: str) -> str:
    return f"{source}:{sha(source, external_id)[:20]}"
