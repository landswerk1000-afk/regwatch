"""Пул бесплатных российских прокси — запасной путь к СОЗД, когда нет туннеля.

Бесплатные публичные прокси живут часами и в массе мертвы, поэтому список
бесполезен без проверки. Модуль сам добывает кандидатов, проверяет каждого
именно на СОЗД (а не на «хоть что-нибудь») и запоминает рабочих.

Через пул идут ТОЛЬКО домены ГД. Почта, пароли и всё остальное уходят
напрямую — чужой прокси их не видит. Это принципиально: публичный прокси
это посторонний посредник, доверять ему нечего.
"""
from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path

from .http import Http
from .util import iso, now_utc, parse_dt

# Открытые списки с фильтром по стране. Ключ API не нужен, регистрация тоже.
SOURCES = [
    ("proxyscrape-socks5",
     "https://api.proxyscrape.com/v4/free-proxy-list/get"
     "?request=display_proxies&country=ru&protocol=socks5"
     "&proxy_format=protocolipport&format=text"),
    ("proxyscrape-http",
     "https://api.proxyscrape.com/v4/free-proxy-list/get"
     "?request=display_proxies&country=ru&protocol=http"
     "&proxy_format=protocolipport&format=text"),
]
MONOSANS = "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies.json"

PROBE_URL = "https://sozd.duma.gov.ru/oz"
CACHE_NAME = "proxy_cache.json"


def _normalize(url: str) -> str:
    """socks5 → socks5h: имя хоста должен резолвить прокси, а не мы.

    Иначе DNS-запрос к sozd уйдёт отсюда и упрётся в ту же фильтрацию,
    от которой мы и уходим.
    """
    url = url.strip()
    if url.startswith("socks5://"):
        url = "socks5h://" + url[len("socks5://"):]
    return url


def fetch_candidates(http: Http, limit: int = 200) -> list[str]:
    """Собирает кандидатов из открытых списков, отбирая российские."""
    out: list[str] = []
    for _, url in SOURCES:
        r = http.get_or_none(url)
        if r:
            out += [l.strip() for l in r.text.splitlines() if l.strip()]

    r = http.get_or_none(MONOSANS)
    if r:
        try:
            for p in r.json():
                geo = (p.get("geolocation") or {}).get("country") or {}
                if geo.get("iso_code") == "RU":
                    out.append(f"{p['protocol']}://{p['host']}:{p['port']}")
        except Exception:
            pass

    seen, uniq = set(), []
    for u in map(_normalize, out):
        if u and u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq[:limit]


def probe(proxy_url: str, timeout: int = 14) -> tuple:
    """Проверяет один прокси именно на СОЗД. → (годен, секунды, пояснение)"""
    t0 = time.time()
    try:
        h = Http(proxy_url=proxy_url, proxy_hosts=[], timeout=timeout,
                 retries=1, min_interval=0)
        r = h.get(PROBE_URL, timeout=timeout, retries=1)
        dt = time.time() - t0
        body = r.text
        ok = r.status == 200 and (len(body) > 20000 or "законопроект" in body.lower())
        return ok, dt, "ok" if ok else f"HTTP {r.status}, {len(body)} байт"
    except Exception as e:
        return False, time.time() - t0, f"{type(e).__name__}: {str(e)[:60]}"


def find_working(candidates: list, workers: int = 12, want: int = 5,
                 on_result=None) -> list:
    """Параллельно проверяет кандидатов. Возвращает рабочих, быстрые первыми."""
    q: queue.Queue = queue.Queue()
    for c in candidates:
        q.put(c)
    found: list = []
    lock = threading.Lock()
    stop = threading.Event()

    def work():
        while not stop.is_set():
            try:
                pu = q.get_nowait()
            except queue.Empty:
                return
            ok, dt, detail = probe(pu)
            with lock:
                if on_result:
                    on_result(pu, ok, dt, detail)
                if ok:
                    found.append({"url": pu, "seconds": round(dt, 2)})
                    if len(found) >= want:
                        stop.set()
            q.task_done()

    threads = [threading.Thread(target=work, daemon=True) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=240)
    found.sort(key=lambda x: x["seconds"])
    return found


def cache_path(root: Path) -> Path:
    return root / "data" / CACHE_NAME


def save(root: Path, working: list) -> Path:
    p = cache_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"checked_at": iso(now_utc()), "proxies": working},
                            ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def load(root: Path, max_age_hours: int = 6) -> str | None:
    """Лучший прокси из кэша, если он не протух."""
    p = cache_path(root)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        checked = parse_dt(data.get("checked_at"))
        if checked and (now_utc() - checked).total_seconds() > max_age_hours * 3600:
            return None
        items = data.get("proxies") or []
        return items[0]["url"] if items else None
    except Exception:
        return None
