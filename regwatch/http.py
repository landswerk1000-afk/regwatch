"""HTTP-слой: только стандартная библиотека, ретраи, gzip, пер-хостовый прокси.

Никаких внешних зависимостей — агент должен переживать обновления системы
и работать из cron без venv.
"""
from __future__ import annotations

import gzip
import io
import json
import random
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field

from . import socks

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

DEFAULT_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "close",
}


class FetchError(Exception):
    def __init__(self, url: str, reason: str, status: int | None = None):
        super().__init__(f"{reason} :: {url}")
        self.url, self.reason, self.status = url, reason, status


class _Redirects(urllib.request.HTTPRedirectHandler):
    """Штатный обработчик не знает 308: его добавили только в Python 3.11.

    Мы живём на 3.9, а regulation.gov.ru отвечает именно 308 — без этого
    источник молча отдаёт ноль документов.

    Одного http_error_308 мало: внутри redirect_request зашит белый список
    кодов (301, 302, 303, 307), и всё остальное там превращается в ошибку.
    Поэтому 308 подменяем на 307 — смысл у них общий, метод запроса
    сохраняется, а мы и без того ходим только GET.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(
            req, fp, 307 if code == 308 else code, msg, headers, newurl)

    http_error_308 = urllib.request.HTTPRedirectHandler.http_error_301


@dataclass
class Response:
    url: str
    status: int
    body: bytes
    headers: dict = field(default_factory=dict)
    elapsed: float = 0.0

    @property
    def text(self) -> str:
        enc = None
        ct = (self.headers.get("Content-Type") or "").lower()
        if "charset=" in ct:
            enc = ct.split("charset=", 1)[1].split(";")[0].strip().strip('"')
        head = self.body[:2000].decode("ascii", "ignore").lower()
        if not enc:
            if "windows-1251" in head or "cp1251" in head:
                enc = "cp1251"
            elif "utf-8" in head:
                enc = "utf-8"
        for cand in ([enc] if enc else []) + ["utf-8", "cp1251", "latin-1"]:
            if not cand:
                continue
            try:
                return self.body.decode(cand)
            except (UnicodeDecodeError, LookupError):
                continue
        return self.body.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.text)


def _decompress(raw: bytes, encoding: str) -> bytes:
    encoding = (encoding or "").lower()
    try:
        if "gzip" in encoding:
            return gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
        if "deflate" in encoding:
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
    except Exception:
        return raw
    return raw


class Http:
    """Клиент с ретраями и маршрутизацией части хостов через прокси.

    proxy_hosts — список доменных суффиксов, которые ходят через proxy_url.
    Пустой список при заданном proxy_url означает «весь трафик через прокси».
    """

    def __init__(self, proxy_url: str | None = None, proxy_hosts=None,
                 timeout: int = 30, retries: int = 3, verify_tls: bool = True,
                 min_interval: float = 0.7, logger=None,
                 relay_url: str | None = None, relay_token: str | None = None):
        self.proxy_url = (proxy_url or "").strip() or None
        # Ретранслятор — функция в Yandex Cloud, скачивающая страницу
        # с российского IP. Предпочтительнее публичного прокси: живёт
        # месяцами, а не днями, и трафик не идёт через чужую машину.
        self.relay_url = (relay_url or "").strip() or None
        self.relay_token = (relay_token or "").strip() or None
        if self.relay_token:
            # Заголовки HTTP передаются в latin-1. Токен с кириллицей роняет
            # запрос сообщением про кодек, из которого причина не видна.
            try:
                self.relay_token.encode("latin-1")
            except UnicodeEncodeError:
                raise ValueError(
                    "REGWATCH_RELAY_TOKEN содержит нелатинские символы. "
                    "Токен передаётся в заголовке HTTP — используйте латиницу, "
                    "цифры и дефис.") from None
        self.proxy_hosts = [h.lower().lstrip(".") for h in (proxy_hosts or [])]
        self.timeout = timeout
        self.retries = retries
        self.min_interval = min_interval
        self.log = logger
        self._last_hit: dict[str, float] = {}
        # Счётчики нужны, чтобы отличить «источник пуст» от «источник
        # недоступен»: без них упавшие запросы дают ok и ноль документов.
        self._ok_count = 0
        self._fail_count = 0

        self._ctx = ssl.create_default_context()
        if not verify_tls:
            self._ctx.check_hostname = False
            self._ctx.verify_mode = ssl.CERT_NONE

        self._plain = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=self._ctx),
            _Redirects(),
        )
        self._via_proxy = None
        self.proxy_kind = None
        if self.proxy_url:
            if socks.is_socks(self.proxy_url):
                # SOCKS5 — то, что поднимает `ssh -D`. urllib его не умеет,
                # поэтому идём через собственные обработчики.
                self.proxy_kind = "socks"
                self._via_proxy = socks.build_opener(self.proxy_url, ssl_context=self._ctx)
            else:
                self.proxy_kind = "http"
                self._via_proxy = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": self.proxy_url,
                                                 "https": self.proxy_url}),
                    urllib.request.HTTPSHandler(context=self._ctx),
                    _Redirects(),
                )

    def _host_listed(self, url: str) -> bool:
        if not self.proxy_hosts:
            return True
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        return any(host == h or host.endswith("." + h) for h in self.proxy_hosts)

    def uses_relay(self, url: str) -> bool:
        return bool(self.relay_url and self.relay_token) and self._host_listed(url)

    def uses_proxy(self, url: str) -> bool:
        if not self._via_proxy:
            return False
        # Ретранслятор старше по приоритету: если он берёт этот адрес,
        # прокси уже не при делах.
        if self.uses_relay(url):
            return False
        return self._host_listed(url)

    def route(self, url: str) -> str:
        """Каким путём пойдёт запрос — для диагностики."""
        if self.uses_relay(url):
            return "ретранслятор"
        if self.uses_proxy(url):
            return f"прокси ({self.proxy_kind})"
        return "напрямую"

    def _throttle(self, url: str) -> None:
        host = urllib.parse.urlsplit(url).hostname or ""
        last = self._last_hit.get(host)
        if last is not None:
            wait = self.min_interval - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._last_hit[host] = time.monotonic()

    def get(self, url: str, headers=None, timeout=None, retries=None,
            allow_status=(200,)) -> Response:
        timeout = timeout or self.timeout
        retries = self.retries if retries is None else retries
        opener = self._via_proxy if self.uses_proxy(url) else self._plain
        hdrs = dict(DEFAULT_HEADERS)
        if headers:
            hdrs.update(headers)

        # Через ретранслятор идёт не исходный адрес, а функция, которой он
        # передан параметром. Токен кладём в заголовок: в строке запроса он
        # осел бы в журналах Яндекса и в истории обращений.
        request_url = url
        via_relay = self.uses_relay(url)
        if via_relay:
            request_url = (self.relay_url.rstrip("/") + "?url="
                           + urllib.parse.quote(url, safe=""))
            hdrs["X-Relay-Token"] = self.relay_token
            opener = self._plain

        last_err = None
        for attempt in range(1, retries + 1):
            self._throttle(url)
            started = time.monotonic()
            try:
                req = urllib.request.Request(request_url, headers=hdrs, method="GET")
                with opener.open(req, timeout=timeout) as resp:
                    raw = resp.read()
                    body = _decompress(raw, resp.headers.get("Content-Encoding", ""))
                    # Через ретранслятор resp.geturl() — это адрес функции.
                    # Источникам нужен настоящий адрес страницы: по нему они
                    # достраивают относительные ссылки.
                    if via_relay:
                        final = resp.headers.get("X-Relay-Final-Url") or url
                    else:
                        final = resp.geturl()
                    r = Response(final, resp.status, body,
                                 dict(resp.headers), time.monotonic() - started)
                    if allow_status and r.status not in allow_status:
                        raise FetchError(url, f"HTTP {r.status}", r.status)
                    self._ok_count += 1
                    return r
            except urllib.error.HTTPError as e:
                last_err = FetchError(url, f"HTTP {e.code}", e.code)
                if e.code in (400, 401, 403, 404, 410):
                    break
            except (urllib.error.URLError, socket.timeout, ssl.SSLError,
                    ConnectionError, TimeoutError) as e:
                reason = getattr(e, "reason", e)
                last_err = FetchError(url, f"{type(e).__name__}: {reason}")
            except Exception as e:
                last_err = FetchError(url, f"{type(e).__name__}: {e}")

            if attempt < retries:
                time.sleep(min(2 ** attempt + random.random(), 12))
        self._fail_count += 1
        raise last_err or FetchError(url, "unknown failure")

    def counters(self) -> tuple[int, int]:
        """Сколько запросов удалось и сколько провалилось за всё время."""
        return self._ok_count, self._fail_count

    def get_or_none(self, url: str, **kw) -> Response | None:
        try:
            return self.get(url, **kw)
        except FetchError as e:
            if self.log:
                self.log.warning("fetch failed: %s", e)
            return None
