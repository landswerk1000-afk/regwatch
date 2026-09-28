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

# Флаг можно задать и переменной окружения: в конвейере сборки удобнее
# так, чем протаскивать аргументы через шаг запуска.
OFFLINE = ("--offline" in sys.argv
           or os.environ.get("REGWATCH_SELFTEST_OFFLINE") == "1")
PASS, FAIL, SKIPPED = [], [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  — {detail}" if detail and not cond else ""))
    return cond


def skip(name, why):
    """Проверка неприменима в этом окружении. Не провал, но и не успех —
    молча пропускать нельзя, иначе дыра в покрытии останется незамеченной."""
    SKIPPED.append(name)
    print(f"  ∼ {name} — пропущено: {why}")


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
# Агент открывает базу по пути из настроек. В проверках это недопустимо:
# 25 сентября самопроверка создала пустую базу в рабочем месте, шаг
# сохранения счёл её состоянием и затёр настоящую историю в репозитории.
_tmpdb = tempfile.mkdtemp()
from regwatch.config import Config
from regwatch.agent import Agent
from regwatch.deliver import send_email, EmailNotConfigured

cfg = Config.load(Path(__file__).parent / "config.json")
check("пути конфига абсолютны", cfg.db_path.is_absolute() and cfg.reports_dir.is_absolute())
cfg.data["db_path"] = str(Path(_tmpdb) / "t.db")
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

# Наличие ключей и окружений — свойство МАШИНЫ, а не кода. Если проверять
# их здесь, тесты нельзя прогнать на чистой копии репозитория, а заслон
# перед развёртыванием начинает падать по причинам, не связанным с правками.
# Настроенность окружения проверяет `regwatch doctor` и отдельный шаг
# в конвейере сборки; здесь — только то, что можно проверить где угодно.
if _wp.vapid()["private"]:
    check("ключ VAPID пригоден для подписи", len(_wp.vapid()["private"]) >= 40,
          f'подозрительно короткий: {len(_wp.vapid()["private"])} символов')
else:
    skip("ключ VAPID задан", "ключа нет в окружении — это проверяет doctor")
if _wp.venv_python(root) is not None:
    check("отправщик push запускается", True)
else:
    skip("окружение для push", "ни .venv-push, ни pywebpush — это проверяет doctor")
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


# ------------------------------- предупреждение о молчащих источниках
section("14. Молчащий источник не должен остаться незамеченным")
import tempfile as _tf3, shutil as _sh3
from regwatch.agent import Agent as _Agent
from regwatch.deliver import telegram as _tg

_box3 = Path(_tf3.mkdtemp())
_cfg3 = Config.load(Path(__file__).resolve().parent / "config.json")
_cfg3.data["db_path"] = str(_box3 / "t.db")
_sent3 = []
_real_send, _real_conf = _tg.send_message, _tg.configured
_tg.send_message = lambda text, **kw: _sent3.append(text)
_tg.configured = lambda: True
_a3 = _Agent(_cfg3)


def _fail3(src, times, err="нет ответа"):
    for _ in range(times):
        _a3.store.health(src, False, err)
    _a3.store.db.commit()


# Порог в два прогона выбран по случаю 26 сентября: пять источников упали
# в одном прогоне и сами поднялись к следующему. Сообщать о таком — приучать
# не читать предупреждения.
_fail3("sozd", 1)
_sent3.clear(); _a3.notify_health()
check("один сбой не тревожит", len(_sent3) == 0)

_fail3("sozd", 1)
_sent3.clear(); _a3.notify_health()
check("два сбоя подряд — сообщение ушло", len(_sent3) == 1)
check("в сообщении назван источник", _sent3 and "sozd" in _sent3[0])

_fail3("sozd", 1)
_sent3.clear(); _a3.notify_health()
check("пока лежит — не повторяемся", len(_sent3) == 0)

_a3.store.health("sozd", True); _a3.store.db.commit()
_sent3.clear(); _a3.notify_health()
check("о восстановлении сообщаем", len(_sent3) == 1 and "снова отвечает" in _sent3[0])
_sent3.clear(); _a3.notify_health()
check("о восстановлении — один раз", len(_sent3) == 0)

# Сбой отправки не должен ронять прогон: отчёт важнее служебного сообщения,
# но и замолчать навсегда нельзя — в следующий прогон пробуем снова.
_fail3("minfin", 2)


def _boom3(text, **kw):
    raise RuntimeError("телеграм недоступен")


_tg.send_message = _boom3
_note3 = _a3.notify_health()
check("сбой отправки не роняет прогон", _note3 and "не отправлено" in _note3)
_tg.send_message = lambda text, **kw: _sent3.append(text)
_sent3.clear(); _a3.notify_health()
check("после сбоя отправки пробует снова", len(_sent3) == 1)

# Текст ошибки приходит из исключения и может содержать разметку.
_a3.store.health("minfin", True); _a3.store.db.commit(); _a3.notify_health()
_fail3("cbr_news", 2, 'HTTPError: <b>500</b> & "сломалось"')
_sent3.clear(); _a3.notify_health()
_t3 = _sent3[0] if _sent3 else ""
check("опасный текст ошибки экранирован",
      "<b>500</b>" not in _t3 and "&lt;b&gt;500" in _t3)

_tg.send_message, _tg.configured = _real_send, _real_conf
_a3.close(); _sh3.rmtree(_box3)


# ------------------------- запасной путь при падении ретранслятора
section("15. Смерть ретранслятора не оставляет без источников")
import http.server as _hs2, socketserver as _ss2
from regwatch.http import Http as _H2, FetchError as _FE2

_MODE = {"relay": "ok"}
_HITS = {"relay": 0, "spare": 0}


class _RelayStub2(_hs2.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        _HITS["relay"] += 1
        m = _MODE["relay"]
        if m == "gone":            # функцию удалили: своя ошибка, без заголовка
            self.send_response(404); self.end_headers(); self.wfile.write(b"no"); return
        if m == "relayed404":      # госсайт ответил 404, ретранслятор жив
            self.send_response(404)
            self.send_header("X-Relay-Final-Url", "https://sozd.duma.gov.ru/x")
            self.end_headers(); self.wfile.write(b"gone"); return
        b = "<h1>через ретранслятор</h1>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("X-Relay-Final-Url", "https://sozd.duma.gov.ru/oz")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)


class _SpareStub2(_hs2.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        _HITS["spare"] += 1
        b = "<h1>через запасной путь</h1>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)


_rs2 = _ss2.TCPServer(("127.0.0.1", 0), _RelayStub2)
_sp2 = _ss2.TCPServer(("127.0.0.1", 0), _SpareStub2)
threading.Thread(target=_rs2.serve_forever, daemon=True).start()
threading.Thread(target=_sp2.serve_forever, daemon=True).start()
time.sleep(0.3)
_RELAY2 = f"http://127.0.0.1:{_rs2.server_address[1]}/"
_SPARE2 = f"http://127.0.0.1:{_sp2.server_address[1]}/"


class _SpareOpener2:
    def open(self, req, timeout=None):
        import urllib.request as _u
        return _u.urlopen(_SPARE2, timeout=timeout)


def _mk2(with_spare=True):
    h = _H2(relay_url=_RELAY2, relay_token="tok",
            proxy_hosts=["sozd.duma.gov.ru"], retries=1, timeout=5)
    h.fallback_proxies = ["socks5h://127.0.0.1:1"] if with_spare else []
    h._make_fallback = lambda u: _SpareOpener2()
    return h


_U2 = "https://sozd.duma.gov.ru/oz"

_MODE["relay"] = "ok"; _HITS.update(relay=0, spare=0)
_h2 = _mk2()
check("исправный ретранслятор используется",
      "через ретранслятор" in _h2.get(_U2).text and _HITS["spare"] == 0)

# Через ретранслятор приходит ЧУЖОЙ код ответа: 404 от СОЗД и 404 «функции
# нет» выглядят одинаково. Путать их нельзя — иначе агент уйдёт на резерв
# из-за одной несуществующей страницы.
_MODE["relay"] = "relayed404"; _HITS.update(relay=0, spare=0)
_h2 = _mk2()
try:
    _h2.get(_U2)
except _FE2:
    pass
check("чужой 404 не считается поломкой", _h2.relay_down_reason is None)
check("на запасной путь при этом не уходим", _HITS["spare"] == 0)

_MODE["relay"] = "gone"; _HITS.update(relay=0, spare=0)
_h2 = _mk2()
_r2 = _h2.get(_U2)
check("своя ошибка функции — это поломка", _h2.relay_down_reason is not None)
check("ответ получен запасным путём", "запасной путь" in _r2.text)

# После падения идти напрямую нельзя: госсайт оборвёт TLS и запрос повиснет
# до таймаута. Проверяем, что следующий запрос тоже идёт в обход.
_HITS.update(relay=0, spare=0)
_h2.get(_U2)
check("мёртвый путь больше не пробуется", _HITS["relay"] == 0)
check("следующий запрос тоже идёт запасным", _HITS["spare"] == 1)

_MODE["relay"] = "gone"
_h2 = _mk2(with_spare=False)
try:
    _h2.get(_U2)
    check("без запаса — внятная ошибка", False, "ошибки не было")
except _FE2 as _e2:
    check("без запаса — внятная ошибка", "запасной путь не найден" in str(_e2))

_h2 = _mk2()
check("ЦБ не затронут", _h2.route("https://cbr.ru/x") == "напрямую")
check("почта не затронута", _h2.route("https://smtp.gmail.com/") == "напрямую")
_rs2.shutdown(); _sp2.shutdown()


# --------------------- срок замечаний не должен молчать в спокойный день
section("16. Срок замечаний не теряется при пустом отчёте")
from datetime import timedelta as _td16
from regwatch.deliver import webpush as _wp16, telegram as _tg16
from regwatch.util import now_msk as _now16


class _FakeRow16(dict):
    def __getitem__(self, k):
        return self.get(k)


def _dl16(days):
    row = _FakeRow16(title="Проект указания Банка России", url="https://cbr.ru/x")
    return [(_now16() + _td16(days=days), row, {})]


# 27 сентября уведомление сказало «значимых изменений нет», хотя через два
# дня истекал срок подачи замечаний по проекту указания ЦБ. Срок был в отчёте
# на сайте — но открывать отчёт после такой фразы у человека нет причины.
# Для юридического инструмента это худший класс пропуска: молчание ровно
# о том единственном, у чего есть жёсткий дедлайн.
_p16 = _wp16.payload_for({}, "Отчёт", False, "https://x/", _dl16(2))
check("push упоминает срок при пустом отчёте", "срок" in _p16["body"].lower(), _p16["body"])
check("срок через 2 дня — важность обычная", _p16["level"] == "normal")

_p16b = _wp16.payload_for({}, "Отчёт", False, "https://x/", _dl16(1))
check("срок завтра поднимает важность", _p16b["level"] == "critical", _p16b["level"])
_p16c = _wp16.payload_for({}, "Отчёт", False, "https://x/", _dl16(0))
check("срок сегодня поднимает важность", _p16c["level"] == "critical")

_p16d = _wp16.payload_for({}, "Отчёт", False, "https://x/", [])
check("без сроков текст прежний", _p16d["body"] == "Значимых изменений нет")
_p16e = _wp16.payload_for({}, "Отчёт", False, "https://x/", _dl16(-3))
check("истёкший срок не упоминается", "срок" not in _p16e["body"].lower())

_t16 = _tg16.render({}, "Отчёт", _dl16(2))
check("telegram показывает сроки при нуле документов", "Ближайшие сроки" in _t16)
check("telegram говорит, сколько осталось", "через 2 дн." in _t16)
_t16b = _tg16.render({}, "Отчёт", [])
check("без сроков telegram прежний", "Ближайшие сроки" not in _t16b)


# ------------------------------- вёрстка отчёта не должна разъезжаться
section("17. Отчёт не выпадает из колонок")
import re as _re17
from regwatch import report as _R17


class _Row17(dict):
    def __getitem__(self, k):
        return self.get(k)


# Заголовки в базе доходят до 400 символов, адреса до 204, стадии до 200.
# Именно на таких вёрстка и ломалась: плашки тем склеивались без пробелов,
# и ряд в 493 пикселя раздувал страницу до 541 при экране 375.
_long17 = "Проект указания Банка России «" + "О внесении изменений " * 18 + "»"
_row17 = _Row17(id="x", authority="ЦБ", title=_long17,
                summary="Очень длинное описание. " * 20,
                stage="3.2 " + "Рассмотрение законопроекта " * 6,
                url="https://cbr.ru/" + "a" * 190,
                published_at="2026-09-27T00:00:00+03:00", meta="{}",
                relevance=0.8, urgency="high", topics='["ЦФА", "ПИФ", "ИИС", "Брокеры"]',
                event_type="new", event_id=1)
_b17 = _R17.group([_row17], {"max_items_per_report": 60})
_h17 = _R17.render_html(_b17, [_row17], [], "Проверка", [])

check("плашки тем переносятся, а не тянут строку",
      "flex-wrap:wrap" in _h17 and "gap:4px 14px" in _h17)
check("длинные слова и адреса переносятся", "overflow-wrap:anywhere" in _h17)
check("есть правила для узкого экрана", "@media (max-width: 560px)" in _h17)
check("цифры сводки перестраиваются", "flex:0 0 50%" in _h17)

# Фильтр по органу: отчёт — обычный файл без сервера, отбор идёт на странице.
check("карточка помечена органом", 'data-org="ЦБ"' in _h17)
check("раздел помечен для скрытия", 'data-sec="high"' in _h17)
_b17b = _R17.group([_row17], {"max_items_per_report": 60})
_h17b = _R17.render_html(_b17b, [_row17], [], "Проверка", [])
check("кнопки органов есть", 'class="org-btn"' in _h17b and 'data-filter="ЦБ"' in _h17b)
check("есть кнопка «Все»", 'data-filter=""' in _h17b)
check("отбор не уезжает в печать", 'display:block!important' in _h17b)

# Подписи отчётов в приложении
from regwatch.deliver.webpush import _human_stamp as _hs17
check("дата в списке по-человечески",
      _hs17("2026-09-27_0946") == "27 сентября, 09:46", _hs17("2026-09-27_0946"))
check("битое имя не роняет список", _hs17("мусор") == "мусор")


# --------------------- публикация не должна сносить код из репозитория
section("18. Публикация трогает только сайт")
import tempfile as _tf18, shutil as _sh18
from regwatch.publish import _sync as _sync18

# Публикация писалась, когда в репозитории лежал один сайт: она чистила
# рабочую копию подчистую и клала туда содержимое webapp/. После переезда
# агента в тот же репозиторий такой прогон снёс бы и код, и файл расписания —
# агент перестал бы запускаться вовсе. Не выстрелило лишь потому, что
# на сервере публикация отключена, а локально шли сухие прогоны.
_b18 = Path(_tf18.mkdtemp())
_repo18, _web18 = _b18 / "repo", _b18 / "webapp"
_repo18.mkdir(parents=True); _web18.mkdir(parents=True)
(_repo18 / ".git").mkdir()
(_repo18 / "regwatch").mkdir()
(_repo18 / "regwatch" / "agent.py").write_text("код", encoding="utf-8")
(_repo18 / ".github").mkdir()
(_repo18 / ".github" / "wf.yml").write_text("расписание", encoding="utf-8")
for _n18 in ("README.md", "selftest.py", "index.html",
             "2026-09-01_0900_daily.html", "2026-09-26_0946_daily.html"):
    (_repo18 / _n18).write_text("старое", encoding="utf-8")
for _n18 in ("index.html", "config.js",
             "2026-09-26_0946_daily.html", "2026-09-27_1800_daily.html"):
    (_web18 / _n18).write_text("новое", encoding="utf-8")

_sync18(_web18, _repo18)
_names18 = {p.name for p in _repo18.iterdir()}
check("код агента переживает публикацию", (_repo18 / "regwatch" / "agent.py").exists())
check("файл расписания переживает публикацию", (_repo18 / ".github" / "wf.yml").exists())
check("README и тесты целы",
      (_repo18 / "README.md").exists() and (_repo18 / "selftest.py").exists())
check("сайт обновляется", (_repo18 / "index.html").read_text(encoding="utf-8") == "новое")
check("новый отчёт добавляется", "2026-09-27_1800_daily.html" in _names18)
check("выпавший из окна отчёт удаляется", "2026-09-01_0900_daily.html" not in _names18)
_sh18.rmtree(_b18)


# ----------------------------- чистка базы, чтобы она не росла без предела
section("19. Чистка базы не трогает нужное")
import tempfile as _tf19, shutil as _sh19
from regwatch.store import Store as _St19

# База лежит в репозитории и уходит туда каждый прогон. На 4.5 МБ отправка
# начала обрываться с HTTP 400, а растёт она примерно на 380 документов
# в сутки. 83%% объёма — ленты СМИ с нулевой оценкой: они нужны только для
# дедупликации, а ленты обновляются за дни.
_b19 = Path(_tf19.mkdtemp())
_st19 = _St19(_b19 / "t.db")


def _add19(n, rel, days_old, reported=True, tag=""):
    for i in range(n):
        iid = f"s:{rel}:{days_old}:{tag}{i}"
        _st19.db.execute(
            "INSERT INTO items (id,source,authority,kind,external_id,url,title,"
            "first_seen,last_seen,content_hash) VALUES (?,?,?,?,?,?,?,"
            "datetime('now', ?),datetime('now'),?)",
            (iid, "s", "СМИ", "news", str(i), "u", "заголовок",
             f"-{days_old} days", f"h{iid}"))
        _st19.db.execute("INSERT INTO scores (item_id,relevance,urgency,topics,"
                         "matched,rationale,scored_at) VALUES (?,?,?,?,?,?,datetime('now'))",
                         (iid, rel, "low", "[]", "[]", ""))
        _st19.db.execute("INSERT INTO events (item_id,event_type,detected_at,reported,alerted)"
                         " VALUES (?,?,datetime('now'),?,?)",
                         (iid, "new", 1 if reported else 0, 1 if reported else 0))
    _st19.db.commit()


_add19(30, 0.0, 40)      # старый шум — убрать
_add19(10, 0.0, 3)       # свежий шум — оставить
_add19(12, 0.7, 40)      # старое, но значимое — оставить
_add19(5, 0.0, 40, reported=False, tag="p")  # старый шум, но человеку не показан

_res19 = _st19.prune(noise_days=14, max_days=180)
_left19 = {r[0] for r in _st19.db.execute("SELECT id FROM items")}
check("старый шум убран", _res19["noise"] == 30, f"убрано {_res19['noise']}")
check("свежий шум сохранён",
      sum(1 for i in _left19 if i.startswith("s:0.0:3:")) == 10)
check("значимое сохранено, даже старое",
      sum(1 for i in _left19 if i.startswith("s:0.7:")) == 12)
# Неразосланное трогать нельзя ни при каких условиях: иначе документ
# исчезнет, так и не попав к человеку.
check("неразосланное не удалено",
      _st19.db.execute("SELECT COUNT(*) FROM events WHERE reported=0").fetchone()[0] == 5)
check("осиротевших оценок нет",
      _st19.db.execute("SELECT COUNT(*) FROM scores s LEFT JOIN items i "
                       "ON i.id=s.item_id WHERE i.id IS NULL").fetchone()[0] == 0)
check("осиротевших событий нет",
      _st19.db.execute("SELECT COUNT(*) FROM events e LEFT JOIN items i "
                       "ON i.id=e.item_id WHERE i.id IS NULL").fetchone()[0] == 0)
_st19.close(); _sh19.rmtree(_b19)


# --------------------- указатель отчётов не должен вести в никуда
section("20. Указатель отчётов соответствует действительности")
import tempfile as _tf20, shutil as _sh20, json as _j20
from regwatch.deliver import webpush as _wp20

# 27 сентября на сайте висел список из пяти файлов, один из которых
# отдавал «страница не найдена», а двух свежих отчётов в нём не было.
# Локальный прогон записал свой указатель, а срочные прогоны без событий
# выходят досрочно и указатель не пересобирают — испорченный список
# переносился в корень сайта прогон за прогоном.
_b20 = Path(_tf20.mkdtemp())
(_b20 / "webapp").mkdir(parents=True)
(_b20 / "reports").mkdir(parents=True)
for _n20 in ("2026-09-26_0946_daily.html", "2026-09-27_0946_daily.html"):
    (_b20 / "webapp" / _n20).write_text("<html>отчёт</html>", encoding="utf-8")
# Указатель, доставшийся от чужого прогона: ссылается на то, чего нет
(_b20 / "webapp" / "latest.json").write_text(_j20.dumps(
    {"reports": [{"file": "2026-09-27_0355_daily.html", "title": "Призрак", "summary": ""}]},
    ensure_ascii=False), encoding="utf-8")

_wp20.export_reports(_b20, _b20 / "reports")
_idx20 = _j20.loads((_b20 / "webapp" / "latest.json").read_text(encoding="utf-8"))["reports"]
_files20 = {r["file"] for r in _idx20}

check("несуществующий файл из указателя убран",
      "2026-09-27_0355_daily.html" not in _files20, str(_files20))
check("настоящие отчёты в указателе есть", len(_files20) == 2, str(_files20))
check("каждая ссылка указывает на существующий файл",
      all((_b20 / "webapp" / r["file"]).exists() for r in _idx20))
check("самый свежий первым",
      _idx20[0]["file"] == "2026-09-27_0946_daily.html", _idx20[0]["file"])
_sh20.rmtree(_b20)


# ------------------- шлюз Yandex отбивает часть запросов, это не смерть
section("21. Придушенный шлюзом ретранслятор не хоронится")
import http.server as _hs21, socketserver as _ss21
from regwatch.http import Http as _H21

# 28 сентября шлюз Yandex Cloud ответил nginx-403 на часть частых запросов:
# заголовок Server: Yandex-Cloud-Functions/1.0, HTML-страница вместо нашего
# текста. Функция при этом жива. Уйти на резерв по такому отказу — променять
# рабочий путь на бесплатный прокси, который, скорее всего, мёртв.
_st21 = {"deny": 0, "always": False}
_hits21 = {"n": 0, "t": []}


class _Gate21(_hs21.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        _hits21["n"] += 1
        _hits21["t"].append(time.monotonic())
        if _st21["always"] or _st21["deny"] > 0:
            if not _st21["always"]:
                _st21["deny"] -= 1
            self.send_response(403)
            self.send_header("Server", "Yandex-Cloud-Functions/1.0")
            self.end_headers(); self.wfile.write(b"<html>403</html>"); return
        _b = b"<h1>ok</h1>"
        self.send_response(200)
        self.send_header("X-Relay-Final-Url", "https://sozd.duma.gov.ru/oz")
        self.send_header("Content-Length", str(len(_b)))
        self.end_headers(); self.wfile.write(_b)


class _Spare21(_hs21.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        _b = b"<h1>zapas</h1>"
        self.send_response(200)
        self.send_header("Content-Length", str(len(_b)))
        self.end_headers(); self.wfile.write(_b)


_g21 = _ss21.TCPServer(("127.0.0.1", 0), _Gate21)
_s21 = _ss21.TCPServer(("127.0.0.1", 0), _Spare21)
threading.Thread(target=_g21.serve_forever, daemon=True).start()
threading.Thread(target=_s21.serve_forever, daemon=True).start()
time.sleep(0.3)
_GATE21 = f"http://127.0.0.1:{_g21.server_address[1]}/"
_SPARE21 = f"http://127.0.0.1:{_s21.server_address[1]}/"


class _SpOpen21:
    def open(self, req, timeout=None):
        import urllib.request as _u
        return _u.urlopen(_SPARE21, timeout=timeout)


def _mk21(retries=6):
    h = _H21(relay_url=_GATE21, relay_token="tok",
             proxy_hosts=["sozd.duma.gov.ru"], retries=retries, timeout=10)
    h.fallback_proxies = ["socks5h://127.0.0.1:1"]
    h._make_fallback = lambda u: _SpOpen21()
    h.RELAY_MIN_INTERVAL = 0.3
    return h


_U21 = "https://sozd.duma.gov.ru/oz"

_st21.update(deny=1, always=False)
_h21 = _mk21()
check("разовый 403 пережит",
      "ok" in _h21.get(_U21).text and _h21.relay_down_reason is None)

_st21.update(deny=2)
_h21 = _mk21()
check("два отказа подряд пережиты",
      "ok" in _h21.get(_U21).text and _h21.relay_down_reason is None)

_st21.update(deny=1)
_h21 = _mk21(); _h21.get(_U21)
check("удачный ответ обнуляет счётчик", _h21._relay_fails == 0)

# Но если функция действительно легла, резерв нужен.
_st21.update(deny=0, always=True)
_h21 = _mk21(retries=8)
check("три отказа подряд — уходим на резерв", "zapas" in _h21.get(_U21).text)
check("причина падения записана", _h21.relay_down_reason is not None)

_st21.update(always=False, deny=0)
_hits21.update(n=0, t=[])
_h21 = _mk21()
for _ in range(3):
    _h21.get(_U21)
_gaps21 = [_hits21["t"][i + 1] - _hits21["t"][i] for i in range(len(_hits21["t"]) - 1)]
# Обычная выдержка считается по адресу назначения, но через ретранслятор
# все запросы идут в один адрес функции — именно это шлюз считает наплывом.
check("между обращениями к функции выдержана пауза",
      all(x >= 0.25 for x in _gaps21), str([round(x, 2) for x in _gaps21]))
check("через функцию попыток больше обычного", _H21.RELAY_RETRIES >= 5)
_g21.shutdown(); _s21.shutdown()


# ------------------------------- региональные акты в отчёт не попадают
section("22. Региональные инициативы отсеиваются")
from regwatch.relevance import Relevance as _R22

_rel22 = _R22(Path(__file__).resolve().parent / "topics.json")


def _score22(title, authority="Правительство", kind="published_act"):
    return _rel22.score(dict(title=title, authority=authority, kind=kind,
                             published_at="2026-09-28T00:00:00+03:00", meta={})).relevance


# 28 сентября отчёт руководителю состоял из семи приказов о тарифах
# Тамбовской области: сработало «акционерное общество» — организационная
# форма регулируемой компании, а не тема ценных бумаг.
for _t22 in (
    'Приказ Департамента цен и тарифов Тамбовской области от 24.09.2026 № 45-э '
    '"О внесении изменений в тарифы акционерного общества"',
    'Постановление Правительства Республики Хакасия от 23.09.2026 № 476 '
    'об акционерных обществах',
    'Решение Управления Алтайского края по государственному регулированию цен '
    'и тарифов в отношении акционерного общества',
    'Распоряжение правительства Еврейской автономной области от 25.09.2026 '
    'об акционерном обществе',
):
    check(f"отсеяно: {_t22[:42]}…", _score22(_t22) == 0, f"оценка {_score22(_t22):.2f}")

# Шаблон «акционерное общество» сам по себе нужен: поправки в закон
# «Об акционерных обществах» — прямо тема мониторинга.
check("федеральный закон об АО проходит",
      _score22('О внесении изменений в статью 84.2 Федерального закона '
               '"Об акционерных обществах"', authority="ГД", kind="draft_law") >= 0.45)
check("проект указания ЦБ проходит",
      _score22('Проект указания Банка России о квалифицированных инвесторах '
               'и индивидуальных инвестиционных счетах', authority="ЦБ",
               kind="draft_act") >= 0.45)
# «Губернатор» и «глава республики» — люди, а не издающие органы: Минфин
# публикует встречи с ними, и такие новости отсеивать нельзя по этому признаку.
check("встреча министра с губернатором не отсеяна по региональности",
      _score22("Министр финансов провёл рабочую встречу с губернатором "
               "по программе долгосрочных сбережений",
               authority="Минфин", kind="press") > 0)


# ------------------------------------------------------------------ итог
print("\n" + "=" * 66)
tail = f"   Пропущено: {len(SKIPPED)}" if SKIPPED else ""
print(f"Пройдено: {len(PASS)}   Провалено: {len(FAIL)}{tail}")
if FAIL:
    print("\nНе прошли:")
    for f in FAIL:
        print(f"   ✗ {f}")
sys.exit(1 if FAIL else 0)
