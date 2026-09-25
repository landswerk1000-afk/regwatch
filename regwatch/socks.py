"""Поддержка SOCKS5 для urllib — стандартная библиотека этого не умеет.

`ssh -D 1080` поднимает именно SOCKS5, поэтому без этого модуля SSH-туннель
до российской машины бесполезен. Реализован RFC 1928 (+ RFC 1929 для логина).
Схема socks5h означает, что имя хоста резолвит удалённая сторона, — это важно:
DNS-запрос к sozd.duma.gov.ru тоже должен уходить из России.
"""
from __future__ import annotations

import http.client
import socket
import ssl
import struct
import urllib.parse
import urllib.request

SOCKS_SCHEMES = ("socks5", "socks5h", "socks4", "socks4a")

_AUTH_NONE, _AUTH_USERPASS = 0x00, 0x02
_CMD_CONNECT = 0x01
_ATYP_IPV4, _ATYP_DOMAIN, _ATYP_IPV6 = 0x01, 0x03, 0x04

_ERRORS = {
    1: "общий сбой SOCKS-сервера", 2: "соединение запрещено правилами",
    3: "сеть недоступна", 4: "хост недоступен", 5: "соединение отклонено",
    6: "истёк TTL", 7: "команда не поддерживается", 8: "тип адреса не поддерживается",
}


class SocksError(OSError):
    pass


def is_socks(url: str) -> bool:
    return urllib.parse.urlsplit(url).scheme.lower() in SOCKS_SCHEMES


def parse(url: str) -> dict:
    p = urllib.parse.urlsplit(url)
    return {
        "scheme": p.scheme.lower(),
        "host": p.hostname or "127.0.0.1",
        "port": p.port or 1080,
        "user": urllib.parse.unquote(p.username) if p.username else None,
        "password": urllib.parse.unquote(p.password) if p.password else None,
        "remote_dns": p.scheme.lower() in ("socks5h", "socks4a"),
    }


def _recv_exactly(sock, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise SocksError("SOCKS-прокси закрыл соединение раньше времени")
        buf += chunk
    return buf


def socks_connect(proxy: dict, dest_host: str, dest_port: int, timeout=None):
    """Открывает TCP-соединение до dest через SOCKS5-прокси."""
    sock = socket.create_connection((proxy["host"], proxy["port"]), timeout=timeout)
    try:
        sock.settimeout(timeout)
        want_auth = bool(proxy.get("user"))
        methods = [_AUTH_NONE, _AUTH_USERPASS] if want_auth else [_AUTH_NONE]
        sock.sendall(bytes([0x05, len(methods)]) + bytes(methods))

        ver, method = _recv_exactly(sock, 2)
        if ver != 0x05:
            raise SocksError(f"не SOCKS5-прокси (версия {ver})")
        if method == 0xFF:
            raise SocksError("прокси отверг предложенные способы аутентификации")
        if method == _AUTH_USERPASS:
            if not proxy.get("user"):
                raise SocksError("прокси требует логин и пароль, а они не заданы")
            u = proxy["user"].encode(); p = (proxy.get("password") or "").encode()
            sock.sendall(bytes([0x01, len(u)]) + u + bytes([len(p)]) + p)
            _, status = _recv_exactly(sock, 2)
            if status != 0x00:
                raise SocksError("прокси отклонил логин или пароль")
        elif method != _AUTH_NONE:
            raise SocksError(f"неподдерживаемый способ аутентификации {method}")

        # CONNECT. При socks5h имя хоста резолвит удалённая сторона.
        if proxy.get("remote_dns", True):
            host_bytes = dest_host.encode("idna")
            req = bytes([0x05, _CMD_CONNECT, 0x00, _ATYP_DOMAIN,
                         len(host_bytes)]) + host_bytes
        else:
            req = bytes([0x05, _CMD_CONNECT, 0x00, _ATYP_IPV4]) + \
                  socket.inet_aton(socket.gethostbyname(dest_host))
        sock.sendall(req + struct.pack(">H", dest_port))

        ver, rep, _, atyp = _recv_exactly(sock, 4)
        if rep != 0x00:
            raise SocksError(f"прокси не смог подключиться к {dest_host}:{dest_port} "
                             f"— {_ERRORS.get(rep, f'код {rep}')}")
        # Адрес привязки и — обязательно — 2 байта порта. Недочитанный хвост
        # уедет в TLS как мусор и даст WRONG_VERSION_NUMBER.
        if atyp == _ATYP_IPV4:
            _recv_exactly(sock, 4)
        elif atyp == _ATYP_DOMAIN:
            _recv_exactly(sock, _recv_exactly(sock, 1)[0])
        elif atyp == _ATYP_IPV6:
            _recv_exactly(sock, 16)
        else:
            raise SocksError(f"неизвестный тип адреса в ответе прокси: {atyp}")
        _recv_exactly(sock, 2)
        return sock
    except Exception:
        sock.close()
        raise


class _SocksHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, socks_proxy=None, **kwargs):
        self._socks_proxy = socks_proxy
        super().__init__(*args, **kwargs)

    def connect(self):
        self.sock = socks_connect(self._socks_proxy, self.host, self.port,
                                  self.timeout)


class _SocksHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, socks_proxy=None, **kwargs):
        self._socks_proxy = socks_proxy
        super().__init__(*args, **kwargs)

    def connect(self):
        raw = socks_connect(self._socks_proxy, self.host, self.port, self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


class SocksHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, proxy: dict, debuglevel=0):
        super().__init__(debuglevel)
        self._proxy = proxy

    def http_open(self, req):
        def build(host, **kw):
            kw.pop("context", None)
            return _SocksHTTPConnection(host, socks_proxy=self._proxy, **kw)
        return self.do_open(build, req)


class SocksHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, proxy: dict, context=None, debuglevel=0):
        super().__init__(debuglevel, context)
        self._proxy = proxy
        self._ctx = context or ssl.create_default_context()

    def https_open(self, req):
        def build(host, **kw):
            kw.pop("context", None)
            return _SocksHTTPSConnection(host, socks_proxy=self._proxy,
                                         context=self._ctx, **kw)
        return self.do_open(build, req)


def build_opener(proxy_url: str, ssl_context=None) -> urllib.request.OpenerDirector:
    from . import http as _http   # внутри функции: модули ссылаются друг на друга
    proxy = parse(proxy_url)
    return urllib.request.build_opener(
        SocksHTTPHandler(proxy),
        SocksHTTPSHandler(proxy, context=ssl_context),
        _http._Redirects(),
    )
