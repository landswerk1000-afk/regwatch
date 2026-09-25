#!/usr/bin/env python3
"""Самопроверка агента. Запуск: python3 selftest.py [--offline]

Проверяет разбор дат, тематический фильтр, дедупликацию, SOCKS5-туннель,
сборку отчёта и письма, доступность источников. Ненулевой код возврата —
что-то сломано.
"""
from __future__ import annotations

import os
import socket
import struct
import sys
import base64
import types
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from regwatch.config import load_env_file
load_env_file()   # секреты из ~/.regwatch.env

OFFLINE = "--offline" in sys.argv
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  — {detail}" if detail and not cond else ""))
    return cond


def section(t):
    print(f"\n{t}")
    print("-" * 66)


# ---------------------------------------------------------------- даты
section("1. Разбор дат")
from regwatch.util import parse_dt, norm_text, item_id

cases = {
    "2026-09-01": (2026, 9, 1), "2026-09-23T00:00:00": (2026, 9, 23),
    "2026-09-23T14:20:45+03:00": (2026, 9, 23),
    "Tue, 22 Sep 2026 13:10:17 +0300": (2026, 9, 22),
    "22.09.2026": (2026, 9, 22), "22.09.2026 15:52": (2026, 9, 22),
    "5 октября 2026": (2026, 10, 5), "2026-09-23T00:00:00.000": (2026, 9, 23),
}
for raw, want in cases.items():
    d = parse_dt(raw)
    check(f"дата {raw[:30]}", d and (d.year, d.month, d.day) == want, f"получено {d}")
check("мусор не парсится", parse_dt("мусор") is None)
check("пустое не парсится", parse_dt("") is None and parse_dt(None) is None)
check("ё приводится к е", norm_text("КвалифицирЁванный") == "квалифицирeванный".replace("e", "е"))

# ---------------------------------------------------------- релевантность
section("2. Тематический фильтр")
from regwatch.relevance import Relevance

rel = Relevance(Path(__file__).parent / "topics.json")
relevant = [
    ("Проект указания о тестировании неквалифицированных инвесторов", "ЦБ", "draft_act", "публичное обсуждение"),
    ("Основные направления развития финансового рынка на 2027 год", "ЦБ", "strategy", "доклад для общественных консультаций"),
    ("О программе долгосрочных сбережений: принят в третьем чтении", "ГД", "draft_law", "принят в третьем чтении"),
    ("Об индивидуальных инвестиционных счетах третьего типа", "Минфин", "draft_law", "первое чтение"),
    ("Новые правила инвестирования для ПИФ", "ЦБ", "event", "состав и структура активов паевых фондов"),
    ("Требования к деятельности инвестиционных советников", "ЦБ", "draft_act", ""),
]
noise = [
    ("Результаты мониторинга максимальных процентных ставок", "ЦБ", "press", ""),
    ("Золотая антилопа: шедевры мультипликации на монете", "ЦБ", "press", "памятная монета"),
    ("Ставка RUONIA", "ЦБ", "news", "курс валют"),
    ("График работы Банка России в праздничные дни", "ЦБ", "news", ""),
]
for t, a, k, s in relevant:
    v = rel.score(dict(title=t, authority=a, kind=k, summary=s, published_at="2026-09-20"))
    check(f"ловит: {t[:44]}", v.relevance >= 0.45, f"релевантность {v.relevance}")
for t, a, k, s in noise:
    v = rel.score(dict(title=t, authority=a, kind=k, summary=s, published_at="2026-09-20"))
    check(f"гасит: {t[:44]}", v.relevance < 0.45, f"релевантность {v.relevance}")

v_crit = rel.score(dict(title="Законопроект о квалифицированных инвесторах внесен в Государственную Думу",
                        authority="ГД", kind="draft_law", summary="внесен в Государственную Думу",
                        published_at="2026-09-22"))
check("срочность critical проставляется", v_crit.urgency == "critical", v_crit.urgency)

fresh = rel.score(dict(title="Основные направления развития финансового рынка", authority="ЦБ",
                       kind="strategy", published_at="2026-08-31"))
old = rel.score(dict(title="Основные направления развития финансового рынка", authority="ЦБ",
                     kind="strategy", published_at="2021-08-31"))
check("архив весит меньше свежего", old.relevance < fresh.relevance,
      f"{old.relevance} vs {fresh.relevance}")
check("морфология: падежи ловятся",
      rel.score(dict(title="о программе долгосрочных сбережений", authority="ГД",
                     kind="draft_law", published_at="2026-09-01")).relevance >= 0.45)

# ------------------------------------------------------------- хранилище
section("3. Хранилище и дедупликация")
from regwatch.store import Store

with tempfile.TemporaryDirectory() as td:
    st = Store(Path(td) / "t.db")
    base = dict(id="x:1", source="s", authority="ЦБ", kind="draft_act", external_id="1",
                url="https://e", title="Проект", summary="о ПИФ", stage="обсуждение",
                published_at="2026-09-01T00:00:00+03:00", meta={})
    check("новый документ даёт событие new",
          [e["event_type"] for e in st.upsert_item(dict(base))] == ["new"])
    check("повтор не даёт события", st.upsert_item(dict(base)) == [])
    ch = dict(base, stage="принят")
    check("смена стадии ловится",
          [e["event_type"] for e in st.upsert_item(ch)] == ["stage_change"])
    ch2 = dict(ch, title="Проект изменён")
    check("правка текста ловится",
          [e["event_type"] for e in st.upsert_item(ch2)] == ["changed"])
    nodate = dict(base, id="x:2", external_id="2", published_at=None, title="Без даты")
    st.upsert_item(nodate)
    st.upsert_item(dict(nodate, published_at="2026-09-05T00:00:00+03:00"))
    got = st.db.execute("SELECT published_at FROM items WHERE id='x:2'").fetchone()[0]
    check("дата дозаполняется задним числом", got is not None, str(got))
    st.set_score("x:1", 0.8, "high", ["ПИФ"], ["пиф"], "тест")
    check("порог релевантности отсекает", len(st.pending(0.9)) == 0)
    check("события выше порога видны", len(st.pending(0.5)) > 0)
    ids = [r["event_id"] for r in st.pending(0.5)]
    st.mark(ids)
    check("отправленное не повторяется", len(st.pending(0.5)) == 0)
    check("id детерминирован", item_id("s", "1") == item_id("s", "1"))
    check("id различает источники", item_id("a", "1") != item_id("b", "1"))
    st.close()

# ------------------------------------------------------------------ SOCKS
section("4. SOCKS5 (то, что даёт ssh -D)")
from regwatch import socks as socks_mod
from regwatch.http import Http

check("socks5h распознаётся", socks_mod.is_socks("socks5h://127.0.0.1:1080"))
check("http не считается socks", not socks_mod.is_socks("http://1.2.3.4:3128"))
p = socks_mod.parse("socks5h://u:p%40ss@10.0.0.1:1081")
check("разбор логина и пароля", p["user"] == "u" and p["password"] == "p@ss" and p["port"] == 1081)
check("socks5h резолвит удалённо", p["remote_dns"])

_seen = []


def _mock_socks(sock):
    """Мок принимает уже привязанный сокет: порт выбирает ОС.

    Раньше порт был зашит числом, и если его занимал чужой процесс (скажем,
    не добитый прошлый прогон), привязка падала внутри потока, список _seen
    оставался пустым, а тест сообщал «туннель не использован» — хотя
    с туннелем всё было в порядке. Ложная тревога хуже отсутствия теста.
    """
    while True:
        c, _ = sock.accept()
        threading.Thread(target=_serve, args=(c,), daemon=True).start()


def _serve(c):
    import select
    try:
        c.recv(2 + 8)
        c.sendall(b"\x05\x00")
        c.recv(4)
        host = c.recv(c.recv(1)[0]).decode()
        port = struct.unpack(">H", c.recv(2))[0]
        _seen.append(f"{host}:{port}")
        up = socket.create_connection((host, port), timeout=20)
        c.sendall(b"\x05\x00\x00\x01" + b"\x00" * 6)
        while True:
            r, _, _ = select.select([c, up], [], [], 25)
            if not r:
                break
            for s in r:
                d = s.recv(65536)
                if not d:
                    return
                (up if s is c else c).sendall(d)
    except Exception:
        pass
    finally:
        try: c.close()
        except Exception: pass


if not OFFLINE:
    _srv = socket.socket()
    _srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    _srv.bind(("127.0.0.1", 0))          # 0 — «дай любой свободный»
    _srv.listen(8)
    _port = _srv.getsockname()[1]
    threading.Thread(target=_mock_socks, args=(_srv,), daemon=True).start()
    time.sleep(0.3)
    h = Http(proxy_url=f"socks5h://127.0.0.1:{_port}", proxy_hosts=[], timeout=30, retries=1)
    check("тип прокси определён как socks", h.proxy_kind == "socks")
    try:
        r = h.get("https://www.cbr.ru/rss/project")
        check("HTTPS через туннель", r.status == 200 and "<rss" in r.text, f"status {r.status}")
    except Exception as e:
        check("HTTPS через туннель", False, str(e)[:60])
    check("туннель реально использован", any("cbr.ru:443" in s for s in _seen), str(_seen))
    dead = Http(proxy_url="socks5h://127.0.0.1:19998", proxy_hosts=[], timeout=6, retries=1)
    try:
        dead.get("https://www.cbr.ru/rss/project")
        check("мёртвый прокси даёт ошибку", False, "запрос неожиданно прошёл")
    except Exception:
        check("мёртвый прокси даёт ошибку", True)

hs = Http(proxy_url="socks5h://127.0.0.1:1080", proxy_hosts=["duma.gov.ru", "regulation.gov.ru"])
check("СОЗД маршрутизируется в туннель", hs.uses_proxy("https://sozd.duma.gov.ru/oz"))
check("ЦБ идёт напрямую", not hs.uses_proxy("https://www.cbr.ru/rss/project"))
check("поддомен тоже ловится", hs.uses_proxy("https://api.duma.gov.ru/x"))
check("похожий чужой домен не ловится", not hs.uses_proxy("https://notduma.gov.ru.evil.com/x"))

# ------------------------------------------------------------ отчёт, почта
section("5. Отчёт и письмо")
from regwatch.config import Config
from regwatch.agent import Agent
from regwatch.deliver import send_email, EmailNotConfigured

cfg = Config.load(Path(__file__).parent / "config.json")
check("пути конфига абсолютны", cfg.db_path.is_absolute() and cfg.reports_dir.is_absolute())
a = Agent(cfg)
buckets, rows, md, html, ev, label, dls, health_rows = a.build()
check("markdown собирается", md.startswith("# Мониторинг") and len(md) > 80)
check("html валиден", html.startswith("<!doctype html") and html.rstrip().endswith("</html>"))
check("html без незакрытых тегов body", html.count("<body") == 1 and html.count("</body>") == 1)
subj = a.subject(buckets, False)
check("тема письма непустая", bool(subj.strip()), subj)
a.close()

# Адреса берём свои, а не из config.json: файл публичный, личной почты
# там нет и быть не должно, а проверка обязана работать в любом случае.
_mail = dict(cfg.email, enabled=True, from_addr="agent@example.com",
             to=["boss@example.com"])
try:
    send_email(dict(_mail), "", "тест", "<b>x</b>", "x", dry_run=True)
    check("без пароля письмо не уходит", False, "отправилось без пароля")
except EmailNotConfigured:
    check("без пароля письмо не уходит", True)
msg = send_email(dict(_mail), "pw", "тест", "<b>x</b>", "x", dry_run=True)
check("с паролем письмо собирается", "dry-run" in msg)

# Личная почта в публичных настройках — отдельная проверка: однажды
# она там уже лежала, и файл уехал бы в репозиторий вместе с ней.
import re as _re
_cfg_text = (Path(__file__).resolve().parent / "config.json").read_text(encoding="utf-8")
check("в config.json нет личных адресов",
      not _re.search(r"[\w.+-]+@[\w-]+\.[a-z]{2,}", _cfg_text),
      "найден адрес электронной почты")

# -------------------------------------------------------------- источники
if not OFFLINE:
    section("6. Живые источники")
    from regwatch.sources import ALL_SOURCES, enabled
    http = cfg.make_http()
    for s in enabled(cfg):
        if s.requires_proxy and not cfg.proxy_url:
            continue
        res = s.run(http)
        check(f"{s.id}", res.ok and len(res.items) > 0,
              res.error or "вернул 0 документов")


# -------------------------------------------------------------- web push
section("6. Web push")
from regwatch.deliver import webpush as _wp
import json as _json

root = Path(__file__).resolve().parent
check("ключ VAPID задан", bool(_wp.vapid()["private"]), "нет VAPID_PRIVATE_KEY")
check("окружение .venv-push на месте", _wp.venv_python(root) is not None)
check("отправщик на месте", _wp.SENDER.exists())

good = _json.dumps({"endpoint": "https://fcm.googleapis.com/fcm/send/X",
                    "keys": {"p256dh": "BN", "auth": "aa"}, "device": "iPhone"})
check("код устройства разбирается", _wp.parse_subscription(good)["device"] == "iPhone")
for bad, why in [("мусор", "не JSON"), ('{"endpoint":"x"}', "без ключей"), ("", "пустой")]:
    try:
        _wp.parse_subscription(bad)
        check(f"отклоняет {why} код", False, "принят")
    except ValueError:
        check(f"отклоняет {why} код", True)

for f in ("index.html", "sw.js", "manifest.json", "config.js", "icon-192.png", "icon-512.png"):
    check(f"webapp/{f}", (root / "webapp" / f).exists())
mf = _json.loads((root / "webapp" / "manifest.json").read_text(encoding="utf-8"))
check("манифест: относительные пути", mf["start_url"].startswith("./") and mf["scope"].startswith("./"),
      "абсолютный путь сломает GitHub Pages в подпапке")
check("манифест: display standalone", mf.get("display") == "standalone", "иначе iOS не даст push")
sw = (root / "webapp" / "sw.js").read_text(encoding="utf-8")
check("sw.js: обработчик push", "addEventListener('push'" in sw)
check("sw.js: обработчик клика", "notificationclick" in sw)
check("публичный ключ в config.js", "VAPID_PUBLIC_KEY" in
      (root / "webapp" / "config.js").read_text(encoding="utf-8"))

fake_crit = {"row": {"id": "x", "title": "Тестовый законопроект", "authority": "ГД",
                     "stage": "3.3 первое чтение", "url": "https://e/1",
                     "published_at": "2026-09-24"}, "events": ["new"]}
pl = _wp.payload_for({"critical": [fake_crit]}, "Срочное", True)
check("срочный push помечен critical", pl["level"] == "critical", pl["level"])
check("срочный push ведёт на документ", pl["url"] == "https://e/1")
pl2 = _wp.payload_for({}, "Отчёт за 24.09", False)
check("пустой отчёт даёт внятный текст", "нет" in pl2["body"].lower(), pl2["body"])

check(".gitignore прячет секреты и базу",
      all(x in (root / ".gitignore").read_text(encoding="utf-8") for x in ("data/", ".venv-push/")))

# ------------------------------------------- молчаливый провал источника
section("8. Источник не должен молчать о недоступности")
from regwatch.sources.base import Source as _Src


class _FakeHttp:
    """Считает запросы так же, как настоящий клиент, но в сеть не ходит."""

    def __init__(self):
        self._ok = self._fail = 0

    def counters(self):
        return self._ok, self._fail

    def spend(self, ok=0, fail=0):
        self._ok += ok
        self._fail += fail


def _src_result(ok_n, fail_n, items):
    def collect(http, src):
        http.spend(ok=ok_n, fail=fail_n)
        return list(items)
    return _Src("t", "тест", "X", "kind", collect).run(_FakeHttp())


# Совет Федерации месяцами отдавал ноль документов со статусом «ok»: все
# запросы падали, а get_or_none глотал ошибки. Ноль сам по себе законен —
# незаконно рапортовать здоровье, когда НИ ОДИН запрос не прошёл.
check("пусто, но источник отвечал — это норма", _src_result(3, 0, []).ok)
check("все запросы провалились — это сбой", not _src_result(0, 2, []).ok)
check("часть упала, но документы есть — норма", _src_result(1, 1, [{"x": 1}]).ok)
check("часть упала, документов нет — норма", _src_result(2, 1, []).ok)
check("запросов не было вовсе — норма", _src_result(0, 0, []).ok)


def _boom(http, src):
    raise ValueError("вёрстка изменилась")


_r = _Src("t", "тест", "X", "kind", _boom).run(_FakeHttp())
check("исключение в разборе — это сбой", not _r.ok and "ValueError" in (_r.error or ""))

# ------------------------------------------------- редирект 308 (Python 3.9)
section("9. Редирект 308")
from regwatch.http import _Redirects
import urllib.request as _ur

check("обработчик 308 объявлен", hasattr(_Redirects, "http_error_308"))
check("redirect_request переопределён",
      _Redirects.redirect_request is not _ur.HTTPRedirectHandler.redirect_request,
      "без этого белый список кодов внутри stdlib отбросит 308")


_codes = []
_orig = _ur.HTTPRedirectHandler.redirect_request


def _spy(self, req, fp, code, msg, headers, newurl):
    _codes.append(code)
    return None


_ur.HTTPRedirectHandler.redirect_request = _spy
try:
    _Redirects().redirect_request(None, None, 308, "", {}, "/x")
    _Redirects().redirect_request(None, None, 302, "", {}, "/x")
finally:
    _ur.HTTPRedirectHandler.redirect_request = _orig

check("308 подменяется на 307 для stdlib", _codes[:1] == [307], f"получено {_codes[:1]}")
check("остальные коды не трогаются", _codes[1:] == [302], f"получено {_codes[1:]}")


# ------------------------------------------------ срочность стареет
section("10. Срочность стареет")
from regwatch.relevance import Relevance as _Rel
from regwatch.util import now_msk as _now
from datetime import timedelta as _td

_rel = _Rel(Path(__file__).resolve().parent / "topics.json")


def _sf(days_ago):
    d = _now() - _td(days=days_ago)
    return _rel.score({
        "title": f"{d:%d.%m.%Y} № 362-СФ О Федеральном законе «О внесении изменений "
                 f"в Федеральный закон „О рынке ценных бумаг“»",
        "authority": "СФ", "kind": "law", "stage": "одобрен Советом Федерации",
        "published_at": d.isoformat(), "meta": {},
    })


# Постановление СФ от 24 июля приходило «срочным» в сентябре: правила по
# словам не знали затухания, оно было только у кодов стадий СОЗД.
check("свежее одобрение — срочное", _sf(2).urgency == "critical", _sf(2).urgency)
check("одобрение двухмесячной давности — уже не срочное",
      _sf(62).urgency == "normal", _sf(62).urgency)
check("порог события короче порога стадии",
      _Rel.EVENT_FRESH_DAYS < _Rel.STAGE_FRESH_DAYS)

# Стадия описывает, где документ сейчас, и живёт дольше события.
_stage = _rel.score({
    "title": "О внесении изменений в Федеральный закон «О рынке ценных бумаг»",
    "authority": "ГД", "kind": "draft_law", "stage": "3.1 Рассмотрение в первом чтении",
    "published_at": (_now() - _td(days=60)).isoformat(), "meta": {},
})
check("чтение двухмесячной давности остаётся срочным",
      _stage.urgency == "critical", _stage.urgency)


# ------------------------------------------ ретранслятор в Yandex Cloud
section("11. Ретранслятор (функция в Yandex Cloud)")
import http.server as _hs, socketserver as _ss, urllib.parse as _up
from regwatch.http import Http as _Http

_RTOKEN = "k7Qm2xR9vLpA4hTzN6wYbJ3sF8dGcE5u"
_relay_seen = []


class _RelayStub(_hs.BaseHTTPRequestHandler):
    """Двойник функции: тот же договор, без облака."""

    def log_message(self, *a):
        pass

    def do_GET(self):
        q = _up.parse_qs(_up.urlsplit(self.path).query)
        target = (q.get("url") or [""])[0]
        tok = self.headers.get("X-Relay-Token", "")
        _relay_seen.append({"target": target, "in_header": bool(tok),
                            "in_query": "token" in q})
        if tok != _RTOKEN:
            self.send_response(403); self.end_headers(); self.wfile.write(b"no"); return
        # Госсайты часто отдают windows-1251 — проверяем, что не побьётся
        body = "<h1>Законопроект о рынке ценных бумаг</h1>".encode("cp1251")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=windows-1251")
        self.send_header("X-Relay-Final-Url", target)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)


_rsrv = _ss.TCPServer(("127.0.0.1", 0), _RelayStub)
threading.Thread(target=_rsrv.serve_forever, daemon=True).start()
time.sleep(0.3)
_rurl = f"http://127.0.0.1:{_rsrv.server_address[1]}/"
_rh = _Http(relay_url=_rurl, relay_token=_RTOKEN, retries=1, timeout=10,
            proxy_hosts=["sozd.duma.gov.ru", "council.gov.ru"])

check("госсайт идёт через ретранслятор", _rh.uses_relay("https://sozd.duma.gov.ru/b"))
check("ЦБ не идёт через ретранслятор", not _rh.uses_relay("https://cbr.ru/x"))
check("почта не трогается", _rh.route("https://smtp.gmail.com/") == "напрямую")
_rr = _rh.get("https://sozd.duma.gov.ru/bill/777")
check("страница получена", _rr.status == 200)
check("windows-1251 не побилась", "ценных бумаг" in _rr.text, repr(_rr.text[:60]))
check("итоговый адрес — госсайта, не функции",
      _rr.url.startswith("https://sozd.duma.gov.ru/"), _rr.url)
# Токен в строке запроса осел бы в журналах Яндекса и в истории обращений.
check("токен ушёл заголовком", _relay_seen and _relay_seen[0]["in_header"])
check("токена нет в строке запроса", _relay_seen and not _relay_seen[0]["in_query"])

_bad = _Http(relay_url=_rurl, relay_token="wrong-token-value", retries=1, timeout=10,
             proxy_hosts=["sozd.duma.gov.ru"])
try:
    _bad.get("https://sozd.duma.gov.ru/b")
    check("неверный токен отвергается", False, "запрос прошёл")
except Exception as _e:
    check("неверный токен отвергается", "403" in str(_e), str(_e)[:40])

try:
    _Http(relay_url=_rurl, relay_token="кириллица")
    check("нелатинский токен объяснён", False, "принят молча")
except ValueError as _e:
    check("нелатинский токен объяснён", "латиниц" in str(_e))

# Ретранслятор старше прокси: одновременно они не применяются.
_both = _Http(relay_url=_rurl, relay_token=_RTOKEN,
              proxy_url="socks5h://127.0.0.1:1080", proxy_hosts=["sozd.duma.gov.ru"])
check("при живом ретрансляторе прокси не задействуется",
      _both.uses_relay("https://sozd.duma.gov.ru/b")
      and not _both.uses_proxy("https://sozd.duma.gov.ru/b"))
_rsrv.shutdown()

# Сборщик пропускает источник, если пути к российскому IP нет. Путей два,
# и когда ретранслятор сменил прокси, проверка «настроен ли прокси» стала
# отвечать «нет» при живом канале — СОЗД молча выпал из сбора.
for _name, _kw, _expect in [
    ("только ретранслятор", dict(relay_url="https://f/", relay_token="tok"), True),
    ("только прокси", dict(proxy_url="socks5h://127.0.0.1:1080"), True),
    ("оба сразу", dict(relay_url="https://f/", relay_token="tok",
                       proxy_url="socks5h://127.0.0.1:1080"), True),
    ("ни одного", dict(), False),
]:
    _hh = _Http(proxy_hosts=["sozd.duma.gov.ru"], **_kw)
    _reachable = _hh.route("https://sozd.duma.gov.ru/oz") != "напрямую"
    check(f"СОЗД собирается — {_name}", _reachable is _expect)

# --- сама функция: белый список и токен ---
_fake = types.ModuleType("requests")


class _RqExc(Exception):
    pass


class _FakeRaw:
    def __init__(self, d): self.d = d
    def read(self, n, decode_content=True): return self.d[:n]


class _FakeResp:
    def __init__(self, d):
        self.raw, self.status_code = _FakeRaw(d), 200
        self.url = "https://sozd.duma.gov.ru/final"
        self.headers = {"Content-Type": "text/html; charset=windows-1251"}


_fake.get = lambda url, **kw: _FakeResp("<h1>Законопроект</h1>".encode("cp1251"))
_fake.RequestException = _RqExc
sys.modules["requests"] = _fake
sys.path.insert(0, str(Path(__file__).resolve().parent / "yandex_function"))
os.environ["TOKEN"] = _RTOKEN
import index as _fn


def _call(url, token=_RTOKEN):
    return _fn.handler({"queryStringParameters": {"url": url},
                        "headers": {"X-Relay-Token": token}}, None)


check("функция: неверный токен — 403", _call("https://sozd.duma.gov.ru/x", "нет")["statusCode"] == 403)
check("функция: чужой домен — 400", _call("https://evil.com/x")["statusCode"] == 400)
# "duma.gov.ru" in url пропустил бы duma.gov.ru.злодей.рф — сверяем имя узла.
check("функция: подделка поддомена отвергается",
      _call("https://duma.gov.ru.evil.com/x")["statusCode"] == 400)
check("функция: свой поддомен принимается", _call("https://sozd.duma.gov.ru/x")["statusCode"] == 200)
check("функция: относительный адрес — 400", _call("/projects")["statusCode"] == 400)
_fr = _call("https://sozd.duma.gov.ru/x")
check("функция: ответ в base64", _fr.get("isBase64Encoded") is True)
check("функция: байты не испорчены",
      base64.b64decode(_fr["body"]).decode("cp1251") == "<h1>Законопроект</h1>")
os.environ.pop("TOKEN", None)


# ------------------------------------------------ фирменный стиль
section("12. Фирменные цвета и доступность")
from regwatch import brand as _B
from regwatch import report as _R


def _hx(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _lum(rgb):
    def ch(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = map(ch, rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _ratio(a, b):
    la, lb = _lum(_hx(a)), _lum(_hx(b))
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


_W, _P = "#ffffff", _B.PAPER
_col = _B.COLORS
# Значения сняты с fgiis.ru. Если сайт перекрасят, отчёт должен поехать
# следом осознанно, а не потому что кто-то поправил цвет наугад.
check("цвета сняты с fgiis.ru",
      (_B.INK, _B.ACCENT, _B.CORAL, _B.PAPER)
      == ("#313132", "#06b6cb", "#FF8562", "#F6F7F8"),
      f"{_B.INK} / {_B.ACCENT} / {_B.CORAL} / {_B.PAPER}")

# Текст обязан читаться и на белом, и на сером фоне страницы: серый темнее,
# поэтому меряем по худшему случаю, а не по белому.
for _name, _c in (("основной текст", _col["ink_65"]), ("заголовки", _col["ink"]),
                  ("ссылки", _col["accent_ink"])):
    _r = min(_ratio(_c, _W), _ratio(_c, _P))
    check(f"{_name} читается (AA 4.5)", _r >= 4.5, f"{_r:.2f}:1")

for _lvl, _m in _B.URGENCY.items():
    _r = min(_ratio(_m["color"], _W), _ratio(_m["color"], _m["soft"]))
    check(f"срочность «{_m['label']}» читается", _r >= 4.5, f"{_r:.2f}:1")

# Бирюза слишком светлая для белого текста — это ограничение самого цвета,
# и вёрстка обязана его соблюдать: тёмный текст на акценте, не белый.
check("графит на бирюзе читается", _ratio(_B.INK, _B.ACCENT) >= 4.5,
      f"{_ratio(_B.INK, _B.ACCENT):.2f}:1")
check("графит на коралле читается", _ratio(_B.INK, _B.CORAL) >= 4.5,
      f"{_ratio(_B.INK, _B.CORAL):.2f}:1")
check("коралл для текста читается",
      min(_ratio(_col["coral_ink"], _W), _ratio(_col["coral_ink"], _P)) >= 4.5,
      f"{min(_ratio(_col['coral_ink'], _W), _ratio(_col['coral_ink'], _P)):.2f}:1")
check("белый текст на акценте НЕ используется (он нечитаем)",
      _ratio(_W, _B.ACCENT) < 3.0, "цвет внезапно потемнел — проверьте вёрстку")

_css = _B.css_variables()
check("палитра отдаётся как CSS-переменные",
      "--accent: #06b6cb" in _css and "--u-critical" in _css)

# Отчёт и приложение должны брать один и тот же акцент.
_app = (Path(__file__).resolve().parent / "webapp" / "index.html").read_text(encoding="utf-8")
check("приложение использует тот же акцент", _B.ACCENT in _app)
check("приложение подключает Inter", "family=Inter" in _app)

# Отчёт читает руководитель: состояние источников для него — шум,
# который подрывает доверие к документу. Диагностика живёт в `doctor`.
_probe_health = [{"source": "sozd", "fail_streak": 3, "error_text": "нет ответа"}]
_hm = _R.render_markdown({}, [], _probe_health, "Проверка", [])
_hh = _R.render_html({}, [], _probe_health, "Проверка", [])
check("состояние источников не попадает в Markdown",
      "Состояние источников" not in _hm and "sozd" not in _hm)
check("состояние источников не попадает в HTML",
      "Состояние источников" not in _hh and "sozd" not in _hh)
check("состояние источников не попадает в модель для PDF",
      "health" not in _R.to_model({}, [], _probe_health, "Проверка", []))


# --------------------------------- выгрузка отчётов на сайт
section("13. Выгрузка отчётов не теряет историю")
import tempfile as _tf, shutil as _sh
from regwatch.deliver import webpush as _wp2


def _mk(d, names):
    d.mkdir(parents=True, exist_ok=True)
    for n in names:
        (d / n).write_text("<html>x</html>", encoding="utf-8")


# На сервере сборки папка reports/ пуста при каждом запуске. Пока выгрузка
# просто зеркалила её, на сайте оставался единственный свежий отчёт,
# а накопленная история пропадала.
_box = Path(_tf.mkdtemp())
_root = _box / "проект"
(_root / "webapp").mkdir(parents=True)
_mk(_root / "webapp", ["2026-09-24_1818_daily.html", "2026-09-25_0931_daily.html"])
(_root / "webapp" / "index.html").write_text("сайт", encoding="utf-8")
_rep = _box / "reports"
_mk(_rep, ["2026-09-25_1200_daily.html"])
_wp2.export_reports(_root, _rep)
_left = sorted(f.name for f in (_root / "webapp").glob("*.html")
               if _wp2.REPORT_NAME.match(f.name))
check("прежние отчёты остаются", len(_left) == 3, str(_left))
check("свежий добавляется", "2026-09-25_1200_daily.html" in _left)
check("index.html не трогается", (_root / "webapp" / "index.html").exists())
_idx = _json.loads((_root / "webapp" / "latest.json").read_text(encoding="utf-8"))
check("самый свежий первым в указателе",
      _idx["reports"][0]["file"] == "2026-09-25_1200_daily.html")
_sh.rmtree(_box)


# ------------------------------------------------------------------ итог
print("\n" + "=" * 66)
print(f"Пройдено: {len(PASS)}   Провалено: {len(FAIL)}")
if FAIL:
    print("\nНе прошли:")
    for f in FAIL:
        print(f"   ✗ {f}")
sys.exit(1 if FAIL else 0)
