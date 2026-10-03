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

# Измеренные пробелы словаря. Пять законопроектов ГД про «Об инвестиционных
# фондах» получали ровный ноль: в словаре была только «паев инвестиционн
# фонд». Проект положения ЦБ про требования к НФО — тоже ноль, слова в
# словаре не было вовсе. Обе дыры нашлись измерением полноты, и обе легко
# закрыть обратно, убрав шаблон «как лишний».
for _t2, _a2, _k2 in [
    ('О внесении изменений в Федеральный закон "Об инвестиционных фондах"',
     "ГД", "draft_law"),
    ("Проект положения Банка России «Об установлении обязательных для "
     "некредитных финансовых организаций требований к операционной надежности»",
     "ЦБ", "draft_act"),
]:
    _v2 = rel.score(dict(title=_t2, authority=_a2, kind=_k2, published_at="2026-09-20"))
    check(f"закрытый пробел: {_t2[:46]}", _v2.relevance >= 0.45, f"{_v2.relevance}")

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
buckets, rows, md, html, ev, label, dls, health_rows, act = a.build()
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

# Отчёт и приложение должны брать один и тот же акцент. Стили приложения
# лежат отдельным файлом, поэтому смотрим страницу вместе с ним: проверяем
# приложение, а не то, в каком файле оно хранит правило.
_WEBAPP = Path(__file__).resolve().parent / "webapp"


def _app_css(*names) -> str:
    """Страницы приложения и общий лист — одним куском."""
    return "\n".join((_WEBAPP / n).read_text(encoding="utf-8")
                      for n in (names or ("index.html", "app.css")))


_app = _app_css()
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

# «2 требует решения, всего 5» не отвечает на вопрос, ради которого
# уведомление и присылают: случилось ли что-то по моей теме. Открывать
# приложение, чтобы это узнать, — лишний шаг там, где его можно не делать.
def _entry16(title, authority="ЦБ", stage=None):
    return {"row": _FakeRow16(title=title, authority=authority, stage=stage, url=None),
            "events": ["new"]}


_p16f = _wp16.payload_for(
    {"high": [_entry16("Проект указания Банка России «О квалифицированных инвесторах»",
                       stage="публичное обсуждение")]},
    "Отчёт", False, "https://x/", [])
check("в уведомлении виден документ, а не счётчик",
      _p16f["title"].startswith("Проект указания"), _p16f["title"])
check("орган и стадия — в подписи",
      "ЦБ" in _p16f["body"] and "публичное обсуждение" in _p16f["body"], _p16f["body"])

_p16g = _wp16.payload_for(
    {"critical": [_entry16("Проект закона о ЦФА", "ГД", "первое чтение")],
     "normal": [_entry16("a"), _entry16("b")]},
    "Отчёт", False, "https://x/", [])
check("главным берётся самый важный", _p16g["title"] == "Проект закона о ЦФА",
      _p16g["title"])
check("остальные посчитаны", "ещё 2 документа" in _p16g["body"], _p16g["body"])
check("уведомление ведёт в приложение, а не на сайт ведомства",
      _p16g["url"] == "https://x/")
check("пустой выпуск остаётся прежним",
      _wp16.payload_for({}, "Отчёт", False, "https://x/", [])["title"]
      .startswith("Регмонитор"))

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
# «Только на сервере» — обычное дело: утренний прогон идёт в облаке,
# и на ноутбуке того отчёта нет. Он не должен исчезать с сайта.
_only18 = "2026-09-28_0949_daily.html"
for _n18 in ("README.md", "selftest.py", "index.html", _only18,
             "2026-09-26_0946_daily.html"):
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
check("отчёт, которого нет на ноутбуке, остаётся на сайте", _only18 in _names18,
      "публикация с ноутбука стирает то, что выложил облачный прогон")

# Окно всё-таки есть — иначе репозиторий будет расти без предела.
for _i18 in range(20):
    (_repo18 / f"2026-08-{_i18 + 1:02d}_0900_daily.html").write_text("х", encoding="utf-8")
_sync18(_web18, _repo18, keep_reports=12)
_rep18 = sorted(p.name for p in _repo18.iterdir()
                if p.name.endswith("_daily.html"))
check("окно отчётов соблюдается", len(_rep18) == 12, f"осталось {len(_rep18)}")
check("в окне остаются самые свежие", _only18 in _rep18 and
      "2026-08-01_0900_daily.html" not in _rep18)
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


# ------------------------------------------- оформление: шкала, полоса, значки
section("23. Оформление собрано в систему")
from regwatch import brand as _B23
from regwatch import report as _R23
import re as _re23

# Шестнадцать размеров шрифта вразнобой — это не система, а накопление.
_sizes23 = sorted(set(_re23.findall(r"font-size:([0-9.]+)px", _R23.STYLE)), key=float)
check("размеров шрифта не больше семи", len(_sizes23) <= 7, ", ".join(_sizes23))
check("все размеры из объявленной шкалы",
      set(_sizes23) <= {v.replace("px", "") for v in _B23.FONT_SCALE.values()},
      f"лишние: {set(_sizes23) - {v.replace('px','') for v in _B23.FONT_SCALE.values()}}")

# Полоса активности: отчёт отвечает «сколько сегодня», но не отвечает
# «много это или мало».
_act23 = [{"date": f"2026-09-{d:02d}", "label": f"{d:02d}.09", "n": n,
           "weekend": d % 7 in (0, 6)}
          for d, n in zip(range(15, 29), [0, 0, 3, 5, 0, 0, 2, 74, 8, 9, 0, 0, 7, 4])]
_h23 = _R23.render_html({}, [], [], "Проверка", [], _act23)
check("полоса активности нарисована", "<svg" in _h23 and "Динамика за две недели" in _h23)
check("полоса — разметка, а не скрипт",
      _h23.count("<rect") == 14, f"столбиков {_h23.count('<rect')}")
check("пик подписан числом", "пик 74" in _h23)

# Один всплеск не должен прижимать остальные дни к нулю.
_hts23 = [float(m) for m in _re23.findall(r'<rect[^>]*height="([0-9.]+)"', _h23)]
_pairs23 = [(h, d["n"]) for h, d in zip(_hts23, _act23) if d["n"] > 0]
_big23 = max(h for h, _ in _pairs23)
_small23 = min(h for h, _ in _pairs23)
_raw23 = max(n for _, n in _pairs23) / min(n for _, n in _pairs23)
_vis23 = _big23 / _small23
# Прямая шкала дала бы отношение 37:1 — мелкие дни выродились бы в нитки.
# Корневая сжимает до корня из этого, и будни остаются различимы.
check("корневая шкала сжимает разброс", _vis23 < _raw23 / 2,
      f"в данных {_raw23:.0f}:1, на полосе {_vis23:.1f}:1")

# Без данных полоса просто не рисуется — пустая рамка хуже её отсутствия.
check("без данных полосы нет", "Динамика за две недели" not in
      _R23.render_html({}, [], [], "Проверка", [], None))

# Значки — контурные, в цвет текста: документу нужна сдержанность.
# Значки живут у заголовков разделов и у сроков — на пустом отчёте
# их нет по построению, поэтому проверяем на наполненном.
class _Row23(dict):
    def __getitem__(self, k):
        return self.get(k)


_r23 = _Row23(id="x", authority="ЦБ", title="Проект указания", summary=None,
              stage=None, url=None, published_at="2026-09-28T00:00:00+03:00",
              meta="{}", relevance=0.7, urgency="high", topics="[]",
              event_type="new", event_id=1)
_dl23 = [(_now16(), _Row23(title="Проект", url=None), {})]
_full23 = _R23.render_html(_R23.group([_r23], {"max_items_per_report": 60}),
                           [_r23], [], "Проверка", _dl23, _act23)
check("значки нарисованы", _full23.count("<svg") >= 2, f"найдено {_full23.count('<svg')}")
check("значки контурные", 'fill="none"' in _full23 and "currentColor" in _full23)
check("значки скрыты от чтения вслух", 'aria-hidden="true"' in _full23)

# Приложение: скелет и уважение к системной настройке движения.
_app23 = _app_css("index.html", "documents.html", "app.css")
check("в приложении есть скелет загрузки", 'class="sk"' in _app23)
check("состояние загрузки объявлено для чтения вслух", "aria-busy" in _app23)
check("анимация отключаема системной настройкой",
      "prefers-reduced-motion" in _app23)


# --------------------------------- указатель документов: данные и страница
section("24. Указатель документов")
import json as _js24, re as _re24, tempfile as _tf24, shutil as _sh24
from datetime import timedelta as _td24
from regwatch.store import Store as _St24
from regwatch.deliver import webpush as _wp24
from regwatch.util import now_msk as _now24

# Указатель — единственное место, где документ можно найти по названию,
# а не наткнуться на него в отчёте за нужное число.
_root24 = Path(_tf24.mkdtemp())
(_root24 / "webapp").mkdir()
_st24 = _St24(_root24 / "t.db")


def _doc24(iid, title, rel, authority="ЦБ", meta="{}", days_ago=1, stage=None):
    _st24.db.execute(
        "INSERT INTO items (id,source,authority,kind,external_id,url,title,stage,"
        "published_at,first_seen,last_seen,content_hash,meta) VALUES (?,?,?,?,?,?,?,?,"
        "datetime('now', ?),datetime('now'),datetime('now'),?,?)",
        (iid, "s", authority, "doc", iid, "https://www.cbr.ru/x", title, stage,
         f"-{days_ago} days", "h" + iid, meta))
    _st24.db.execute(
        "INSERT INTO scores (item_id,relevance,urgency,topics,matched,rationale,scored_at)"
        " VALUES (?,?,?,?,?,?,datetime('now'))",
        (iid, rel, "normal", '["ЦФА, цифровые активы, криптовалюта"]', "[]", ""))
    _st24.db.commit()


_soon24 = _now24() + _td24(days=9)
_gone24 = _now24() - _td24(days=120)
_MON24 = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]


def _ru24(d):
    return f"{d.day} {_MON24[d.month - 1]} {d.year}"


_doc24("a", "Проект указания о цифровых правах", 0.8, days_ago=1)
_doc24("b", "Пресс-релиз ни о чём", 0.0, authority="СМИ", days_ago=2)
# Ноль по теме, но открыто обсуждение: ровно те документы, по которым
# ещё можно что-то сказать, — их нельзя терять из-за словаря тем.
_doc24("c", "Проект положения об обязательных нормативах", 0.0, days_ago=3,
       meta=_js24.dumps({"comments_until": _ru24(_soon24)}))
_doc24("d", "Проект указания позапрошлогодний", 0.0, days_ago=200,
       meta=_js24.dumps({"comments_until": _ru24(_gone24)}))
# Указатель отвечает на вопрос «что регулируется», поэтому в нём только те,
# кто регулирует. Пересказ в деловой ленте туда не попадает ни при какой
# оценке — и даже если у него откуда-то взялся срок замечаний.
_doc24("e", "Газета пишет про ЦФА", 0.9, authority="СМИ", days_ago=1)
_doc24("f", "Постановление опубликовано", 0.9, authority="Правительство", days_ago=1,
       meta=_js24.dumps({"comments_until": _ru24(_soon24)}))

_n24 = _wp24.export_documents(_root24, _st24, floor=0.45,
                              skip_authorities=["СМИ", "Правительство"])
_out24 = _js24.loads((_root24 / "webapp" / "documents.json").read_text(encoding="utf-8"))
_ids24 = [d["id"] for d in _out24["documents"]]

check("файл указателя собран", (_root24 / "webapp" / "documents.json").exists())
check("значимое попало", "a" in _ids24)
check("шум отсеян", "b" not in _ids24, f"в списке {_ids24}")
check("открытый срок замечаний важнее оценки темы", "c" in _ids24,
      "документ с открытым обсуждением выпал — ради него указатель и нужен")
check("давно закрытое обсуждение не тянем", "d" not in _ids24)
check("отсечённый орган не проходит по оценке", "e" not in _ids24,
      f"в списке {_ids24}")
check("отсечённый орган не проходит и по сроку замечаний", "f" not in _ids24,
      "исключение обошли через блок сроков")
check("отбор берётся из настроек, а не вшит",
      "skip_authorities" in (Path(__file__).resolve().parent / "config.json")
      .read_text(encoding="utf-8"))
check("число в заголовке совпадает со списком",
      _out24["total"] == len(_out24["documents"]) == _n24)

_c24 = next(d for d in _out24["documents"] if d["id"] == "c")
check("срок отдан и человеку, и машине",
      _c24["deadline"] == _ru24(_soon24)
      and _c24["deadline_iso"] == _soon24.strftime("%Y-%m-%d"),
      f'{_c24["deadline"]!r} / {_c24["deadline_iso"]!r}')
check("свежее сверху",
      all(_out24["documents"][i]["sort"] >= _out24["documents"][i + 1]["sort"]
          for i in range(len(_out24["documents"]) - 1)))
check("поля на месте",
      set(_c24) >= {"id", "title", "org", "url", "stage", "code", "date",
                    "sort", "topics", "deadline", "deadline_iso", "changed"},
      f"не хватает {{'id','title','org','url','stage','code','date','sort','topics','deadline','deadline_iso','changed'}} - {set(_c24)}")
# Файл уезжает в репозиторий, а прогонов восемь в сутки. Если переписывать
# его всегда, в историю каждые три часа ложится 44 КБ ради одной отметки.
_was24 = (_root24 / "webapp" / "documents.json").stat().st_mtime_ns
_wp24.export_documents(_root24, _st24, floor=0.45,
                       skip_authorities=["СМИ", "Правительство"])
check("без изменений файл не переписывается",
      (_root24 / "webapp" / "documents.json").stat().st_mtime_ns == _was24)
_doc24("g", "Новый проект указания о ЦФА", 0.8, days_ago=0)
_wp24.export_documents(_root24, _st24, floor=0.45,
                       skip_authorities=["СМИ", "Правительство"])
check("с изменением — переписывается",
      (_root24 / "webapp" / "documents.json").stat().st_mtime_ns != _was24)

# Сломавшийся агент выглядит как спокойный день: приложение показывает
# вчерашний отчёт, и отличить одно от другого нечем.
_wp24.export_status(_root24, documents=7, reports=3)
_st_json24 = _js24.loads((_root24 / "webapp" / "status.json").read_text(encoding="utf-8"))
check("отметка о сборе записана", bool(_st_json24.get("checked")))
check("отметка разбирается как дата",
      _st_json24["checked"][:4].isdigit() and "T" in _st_json24["checked"],
      _st_json24.get("checked", ""))
check("отметка маленькая",
      (_root24 / "webapp" / "status.json").stat().st_size < 200,
      f'{(_root24 / "webapp" / "status.json").stat().st_size} байт')
_sh24.rmtree(_root24, ignore_errors=True)

# --- страница ---
_page24 = (_WEBAPP / "documents.html").read_text(encoding="utf-8")
_css24 = (_WEBAPP / "app.css").read_text(encoding="utf-8")
_idx24 = (_WEBAPP / "index.html").read_text(encoding="utf-8")
_sw24 = (_WEBAPP / "sw.js").read_text(encoding="utf-8")

check("страница берёт общие стили", './app.css' in _page24)
check("страница читает свой файл данных", "documents.json" in _page24)
check("с главной есть вход в указатель", "documents.html" in _idx24,
      "страница есть, но открыть её неоткуда")

# Стили вынесли из страницы — оболочка для работы без сети должна была
# поехать следом. Один раз это уже забылось.
check("офлайн-оболочка знает про общий лист", "'./app.css'" in _sw24)
check("офлайн-оболочка знает про указатель", "'./documents.html'" in _sw24)
check("офлайн-оболочка знает про отметку о сборе", "'./fresh.js'" in _sw24)
for _n24 in ("index.html", "documents.html"):
    _t24 = (_WEBAPP / _n24).read_text(encoding="utf-8")
    check(f"{_n24} показывает отметку о сборе",
          'id="fresh"' in _t24 and "fresh.js" in _t24)

# Цвет объявляется в одном месте. Переменная, которой нет в общем листе, —
# это не «чуть другой оттенок», это отсутствие цвета.
_declared24 = set(_re24.findall(r"(--[a-z0-9-]+)\s*:", _css24))
_used24 = set()
for _t24 in (_page24, _idx24, _css24):
    _used24 |= set(_re24.findall(r"var\((--[a-z0-9-]+)", _t24))
check("все цвета объявлены в общем листе", _used24 <= _declared24,
      f"нет объявления: {sorted(_used24 - _declared24)}")

# Скрипт зовёт элементы по идентификатору. Опечатка тут — пустая страница
# без единой ошибки в консоли.
_wanted24 = set(_re24.findall(r"\$\('([A-Za-z0-9_]+)'\)", _page24))
_have24 = set(_re24.findall(r'id="([A-Za-z0-9_]+)"', _page24))
check("скрипт зовёт только существующие элементы", _wanted24 <= _have24,
      f"нет в разметке: {sorted(_wanted24 - _have24)}")

# Названия и ссылки приходят с чужих сайтов и попадают в разметку.
check("текст экранируется перед вставкой",
      "&amp;" in _page24 and "&quot;" in _page24)
check("в ссылку пускаем только http(s)", "^https?:" in _page24,
      "нет проверки схемы — в href попадёт что угодно")

# Поиск должен переживать склонение: «указание» не входит в «указания».
check("поиск укорачивает слово с конца", "function stems" in _page24)
_floor24 = _re24.search(r"Math\.max\((\d+), Math\.ceil\(w\.length \* ([0-9.]+)\)\)",
                        _page24)
check("укорачивание ограничено", bool(_floor24) and int(_floor24.group(1)) >= 4,
      "без нижней границы «торги» превратятся в «тор» и найдут «директор»")


# ------------------- вызовы отчёта должны сходиться с его сигнатурами
section("25. Агент зовёт отчёт по-настоящему")
import ast as _ast25, inspect as _in25
from regwatch import report as _R25

# Полоса активности добавилась в render_html — и заодно попала в to_model,
# который её не принимает. Агент ловил TypeError, писал строчку в журнал
# и шёл дальше: письмо уходило без PDF, а PDF — это то, что читает
# руководитель. Три дня никто не знал. Такое должна ловить проверка,
# а не человек, который однажды заглянет в журнал.
_src25 = (Path(__file__).resolve().parent / "regwatch" / "agent.py").read_text(encoding="utf-8")
_tree25 = _ast25.parse(_src25)
_watch25 = ("to_model", "render_html", "render_markdown", "group", "deadlines")
_calls25 = [n for n in _ast25.walk(_tree25)
            if isinstance(n, _ast25.Call)
            and isinstance(n.func, _ast25.Attribute)
            and n.func.attr in _watch25]

check("вызовы отчёта найдены в агенте", len(_calls25) >= 4, f"найдено {len(_calls25)}")
for _c25 in _calls25:
    _fn25 = getattr(_R25, _c25.func.attr, None)
    if _fn25 is None:
        check(f"{_c25.func.attr} существует", False, "функции нет в report.py")
        continue
    _star25 = any(isinstance(a, _ast25.Starred) for a in _c25.args)
    if _star25:
        skip(f"{_c25.func.attr}(...) — аргументы", "распаковка, арность не видна")
        continue
    try:
        _in25.signature(_fn25).bind(*[None] * len(_c25.args),
                                    **{k.arg: None for k in _c25.keywords if k.arg})
        _ok25, _why25 = True, ""
    except TypeError as _e25:
        _ok25, _why25 = False, str(_e25)
    check(f"{_c25.func.attr}(...) в строке {_c25.lineno} сходится с сигнатурой",
          _ok25, _why25)


# ------------------------- клавиатура должна видеть, где находится
section("26. Видимый фокус")
import re as _re26
from regwatch import brand as _B26
from regwatch import report as _R26

# Кольцо фокуса — такой же указатель, как курсор: по WCAG 1.4.11 ему нужно
# не меньше 3:1 к тому, на чём оно нарисовано. Бирюза на светлом даёт 2,3:1,
# то есть его просто не видно. Проверяем числом, а не на глаз.
_css26 = (_WEBAPP / "app.css").read_text(encoding="utf-8")


def _hex26(v):
    """#fff и #ffffff — одно и то же; в листе встречаются обе записи."""
    v = v.lstrip("#")
    return "#" + ("".join(c * 2 for c in v) if len(v) == 3 else v)


def _vals26(name):
    return [_hex26(v) for v in
            _re26.findall(name + r":\s*(#[0-9a-fA-F]{3,6})\b", _css26)]


_focus26, _bg26, _card26 = _vals26("--focus"), _vals26("--bg"), _vals26("--card")
check("цвет фокуса объявлен в обеих темах", len(_focus26) == 2, str(_focus26))
if len(_focus26) == 2 and len(_bg26) == 2 and len(_card26) == 2:
    for _i26, _theme26 in enumerate(("светлая", "тёмная")):
        for _on26, _name26 in ((_bg26[_i26], "фон"), (_card26[_i26], "карточка")):
            _r26 = _ratio(_focus26[_i26], _on26)
            check(f"фокус различим: {_theme26} тема, {_name26}", _r26 >= 3.0,
                  f"{_r26:.2f}:1 при нужных 3:1")

check("кольцо фокуса есть в приложении", ":focus-visible{outline:" in _css26)
# Правило, гасящее контур, легко поставить и забыть — тогда мышью всё
# красиво, а клавиатурой не пройти.
_docs26 = (_WEBAPP / "documents.html").read_text(encoding="utf-8")
check("поиск не гасит кольцо для клавиатуры",
      "input:focus-visible{outline:2px" in _docs26,
      "правило :focus у поля поиска выключает контур и для клавиатуры тоже")

# Отчёт — отдельный файл со своими стилями: в нём и ссылки на
# первоисточники, и кнопки отбора по органу.
check("кольцо фокуса есть в отчёте", ":focus-visible{outline:" in _R26.STYLE)
_rf26 = _re26.search(r":focus-visible\{outline:2px solid (#[0-9a-fA-F]{6})", _R26.STYLE)
check("фокус в отчёте различим на белом",
      bool(_rf26) and _ratio(_rf26.group(1), "#ffffff") >= 3.0,
      f"{_ratio(_rf26.group(1), '#ffffff'):.2f}:1" if _rf26 else "цвет не найден")


# ------------------------------- колонтитул: что за документ в руках
section("27. Колонтитул на каждой странице")
_h27 = _R26.render_html({}, [], [], "Отчёт за 29.09.2026", [], None)
check("колонтитул есть в разметке отчёта", 'class="runhead"' in _h27)
check("колонтитул называет выпуск", "Отчёт за 29.09.2026" in _h27.split("</div>")[0]
      or "Отчёт за 29.09.2026" in _h27[:_h27.index('class="head"')],
      "в колонтитуле нет даты выпуска")
check("на экране колонтитула не видно", ".runhead{display:none;}" in _R26.STYLE)
# Браузеры повторяют на каждой странице только группу заголовка таблицы.
_print27 = _R26.STYLE[_R26.STYLE.index("@media print"):]
check("в печати колонтитул повторяется",
      "table-header-group" in _print27 and ".sheet{display:table" in _print27)
check("экранной вёрстки правило не касается",
      ".sheet{display:table" not in _R26.STYLE[:_R26.STYLE.index("@media print")])

# PDF уходит письмом и живёт дальше сам: его пересылают и печатают.
# На четвёртой странице должно быть видно, что это и всё ли дошло.
from regwatch.deliver import pdf as _pdf27
if not _pdf27.configured(Path(__file__).resolve().parent):
    skip("колонтитул в PDF", "нет окружения .venv-pdf")
else:
    try:
        from pypdf import PdfReader as _Reader27
    except ImportError:
        _Reader27 = None
    if _Reader27 is None:
        skip("колонтитул в PDF", "нет pypdf для чтения готового файла")
    else:
        import tempfile as _tf27
        # Идентификаторы обязаны различаться: отчёт схлопывает одинаковые
        # в одну карточку, и документ остался бы одностраничным.
        _rows27 = [_Row23(id=f"x{_i}", authority="ЦБ", summary="С" * 900, stage=None,
                          title=f"Проект указания Банка России № {_i} " * 3, url=None,
                          published_at="2026-09-29T00:00:00+03:00", meta="{}",
                          relevance=0.7, urgency="high", topics="[]",
                          event_type="new", event_id=_i)
                   for _i in range(22)]
        _m27 = _R26.to_model(_R26.group(_rows27, {"max_items_per_report": 60}),
                             _rows27, [], "Отчёт за 29.09.2026", [])
        _out27 = Path(_tf27.mkdtemp()) / "p.pdf"
        _pdf27.build(Path(__file__).resolve().parent, _m27, _out27)
        _pages27 = _Reader27(str(_out27)).pages
        check("PDF получился многостраничным", len(_pages27) > 1, f"{len(_pages27)} стр.")
        _bad27 = []
        for _i27, _pg27 in enumerate(_pages27, 1):
            _txt27 = _pg27.extract_text() or ""
            if "Отчёт за 29.09.2026" not in _txt27:
                _bad27.append(f"{_i27}: нет выпуска")
            elif f"{_i27} из {len(_pages27)}" not in _txt27:
                _bad27.append(f"{_i27}: нет номера")
        check("на каждой странице PDF виден выпуск и номер", not _bad27, "; ".join(_bad27))
        _sh24.rmtree(_out27.parent, ignore_errors=True)


# --------------------------- чем мерить, что агент не пропускает
section("28. Измерение охвата")
import json as _js28, tempfile as _tf28, shutil as _sh28
from regwatch import coverage as _cov28
from regwatch.store import Store as _St28

# Отчёт показывает то, что агент счёл значимым. Чего не счёл — не показывает
# нигде, и до эталонной выборки узнать об этом было неоткуда. Инструмент,
# про который нельзя сказать, что он пропускает, юристу верить не на что.
_root28 = Path(_tf28.mkdtemp())
(_root28 / "webapp").mkdir()
_st28 = _St28(_root28 / "t.db")


def _item28(iid, title, rel, kind="draft_act", authority="ЦБ", days=1):
    _st28.db.execute(
        "INSERT INTO items (id,source,authority,kind,external_id,url,title,"
        "published_at,first_seen,last_seen,content_hash) VALUES (?,?,?,?,?,?,?,"
        "datetime('now', ?),datetime('now'),datetime('now'),?)",
        (iid, "s", authority, kind, iid, "u", title, f"-{days} days", "h" + iid))
    _st28.db.execute(
        "INSERT INTO scores (item_id,relevance,urgency,topics,matched,rationale,scored_at)"
        " VALUES (?,?,'normal','[]','[]','',datetime('now'))", (iid, rel))
    _st28.db.commit()


_item28("a", "Узнан и значим", 0.80)
_item28("b", "Пропущен, но значим", 0.00)
_item28("c", "Узнан, хотя не нужен", 0.70)
_item28("d", "Не узнан и не нужен", 0.00)
_item28("e", "По названию не определить", 0.00)
# Значим, узнаётся по теме, но стар: скидка за возраст увела ниже порога.
# Это намеренно, и считать это пропуском нельзя.
_item28("f", "Старая редакция ОНРФР", 0.28, kind="strategy", days=1200)

_cov28.save_reference(_root28, {"labels": {
    "a": {"значим": "да"}, "b": {"значим": "да"}, "c": {"значим": "нет"},
    "d": {"значим": "нет"}, "e": {"значим": "?"}, "f": {"значим": "да"},
}})
_m28 = _cov28.measure(_st28, _root28, floor=0.45)

check("выборка собрана целиком", _m28["total"] == 6, str(_m28["total"]))
# Полнота: значимых трое (a, b, f), узнаны двое — a и f (у f скидка
# за возраст, но по теме он узнан).
check("полнота считается без возрастной скидки",
      abs(_m28["recall"] - 2 / 3) < 1e-9, str(_m28["recall"]))
check("точность считается", abs(_m28["precision"] - 2 / 3) < 1e-9,
      str(_m28["precision"]))
check("пропуск назван поимённо",
      [r["id"] for r in _m28["misses"]] == ["b"],
      str([r["id"] for r in _m28["misses"]]))
check("лишнее названо поимённо",
      [r["id"] for r in _m28["false_hits"]] == ["c"])
check("возрастная скидка показана отдельно, а не как пропуск",
      [r["id"] for r in _m28["aged"]] == ["f"],
      "иначе архивные редакции выглядят дырой в словаре")
check("«по названию не определить» не влияет на полноту", _m28["unknown"] == 1)
check("вывод читается человеком",
      "Полнота" in _cov28.render(_m28) and "Точность" in _cov28.render(_m28))

# Скидка за возраст и словарь отвечают за разное, и разделять их надо честно.
check("оценка без скидки восстанавливается",
      abs(_cov28.thematic(0.35, None) - 0.35 / 0.92) < 1e-6,
      str(_cov28.thematic(0.35, None)))
check("свежему документу скидка не меняет ничего",
      _cov28.thematic(0.8, "2026-09-29T00:00:00+03:00") == 0.8)

# Разметка должна пережить выгрузку и возврат, иначе мерить нечем.
_rev28 = _root28 / "новое.json"
_item28("g", "Ещё не размечен", 0.5)
check("неразмеченное выписывается", _cov28.review_file(_st28, _root28, _rev28) == 1)
_data28 = _js28.loads(_rev28.read_text(encoding="utf-8"))
_data28["labels"]["g"]["значим"] = "да"
# Человек правит файл на диске — так же поступает и проверка.
_rev28.write_text(_js28.dumps(_data28, ensure_ascii=False), encoding="utf-8")
_taken28, _ = _cov28.apply_file(_root28, _rev28)
check("разметка возвращается в эталон", _taken28 == 1)
check("эталон переживает перезапись",
      _cov28.load_reference(_root28)["labels"]["g"]["значим"] == "да")
_sh28.rmtree(_root28, ignore_errors=True)

# Половина разметки — документы с нулевой оценкой: ровно те, которые чистка
# забирает первыми. Стереть их значит стереть доказательство, что фильтр
# работает, и возможность сравнить «до» и «после».
_b28 = Path(_tf28.mkdtemp())
_st28b = _St28(_b28 / "t.db")
for _i28 in range(6):
    _id28 = f"x{_i28}"
    _st28b.db.execute(
        "INSERT INTO items (id,source,authority,kind,external_id,url,title,"
        "first_seen,last_seen,content_hash) VALUES (?,?,?,?,?,?,?,"
        "datetime('now','-90 days'),datetime('now'),?)",
        (_id28, "s", "СМИ", "news", str(_i28), "u", "шум", "h" + _id28))
    _st28b.db.execute("INSERT INTO scores (item_id,relevance,urgency,topics,matched,"
                      "rationale,scored_at) VALUES (?,0,'low','[]','[]','',datetime('now'))",
                      (_id28,))
    _st28b.db.execute("INSERT INTO events (item_id,event_type,detected_at,reported,alerted)"
                      " VALUES (?,'new',datetime('now'),1,1)", (_id28,))
_st28b.db.commit()
_st28b.prune(noise_days=14, max_days=180, keep_ids=("x1", "x3"))
_left28 = sorted(r[0] for r in _st28b.db.execute("SELECT id FROM items"))
check("чистка не трогает эталонную выборку", _left28 == ["x1", "x3"], str(_left28))
_st28b.close()
_sh28.rmtree(_b28, ignore_errors=True)

# Правка словаря должна доезжать до накопленного. Иначе дыру «закрыли»,
# а в указателе и в измерении она на месте: новые документы считаются
# по новому словарю, старые остаются с прежними оценками.
_c28 = Path(_tf28.mkdtemp())
(_c28 / "webapp").mkdir()
(_c28 / "reports").mkdir()
_sh28.copy2(Path(__file__).resolve().parent / "topics.json", _c28 / "topics.json")
(_c28 / "config.json").write_text(_js28.dumps({
    "db_path": "t.db", "reports_dir": "reports", "topics_path": "topics.json",
    "sources": {}, "email": {"enabled": False},
}, ensure_ascii=False), encoding="utf-8")
from regwatch.config import Config as _Cfg28
from regwatch.agent import Agent as _Ag28

_ag28 = _Ag28(_Cfg28.load(_c28 / "config.json"))
try:
    _ag28.store.db.execute(
        "INSERT INTO items (id,source,authority,kind,external_id,url,title,"
        "published_at,first_seen,last_seen,content_hash) VALUES "
        "('z','s','ЦБ','draft_act','z','u','Проект указания о тестировании "
        "неквалифицированных инвесторов',datetime('now'),datetime('now'),"
        "datetime('now'),'hz')")
    _ag28.store.set_score("z", 0.0, "low", [], [], "оценено старым словарём")
    _ag28.store.db.commit()

    check("первый прогон переоценивает базу", _ag28.apply_topics_changes() == 1)
    _z28 = _ag28.store.db.execute("SELECT relevance FROM scores WHERE item_id='z'").fetchone()[0]
    check("старая оценка исправлена новым словарём", _z28 >= 0.45, str(_z28))
    check("без правки словаря переоценки нет", _ag28.apply_topics_changes() == 0,
          "переоценка на каждом прогоне — лишняя работа и лишний шум в журнале")

    _t28 = (_c28 / "topics.json")
    _cfg28 = _js28.loads(_t28.read_text(encoding="utf-8"))
    _cfg28["topics"][0]["patterns"].append("совершенно новый шаблон")
    _t28.write_text(_js28.dumps(_cfg28, ensure_ascii=False), encoding="utf-8")
    check("правка словаря вызывает переоценку", _ag28.apply_topics_changes() == 1)
finally:
    _ag28.close()
_sh28.rmtree(_c28, ignore_errors=True)

# Сам эталон в репозитории: без него измерение — пустая команда.
_ref28 = _cov28.reference_path(Path(__file__).resolve().parent)
if not _ref28.exists():
    skip("эталонная разметка на месте", "эталон.json ещё не собран")
else:
    _labels28 = _cov28.load_reference(Path(__file__).resolve().parent)["labels"]
    check("эталонная разметка на месте", len(_labels28) >= 50,
          f"размечено {len(_labels28)}")
    check("все метки допустимы",
          all(v.get("значим") in _cov28.VALID for v in _labels28.values()))


# ------------------- региональное и местное не должно попадать в базу
section("29. Только федеральный уровень")
from regwatch.sources.pravo import _federal as _fed29

# Портал опубликования выдаёт акты всех уровней сразу, и регионы дают
# подавляющий объём: из 869 собранных записей 714 оказались краевыми,
# областными и республиканскими приказами. Раньше их гасила оценка в ноль —
# то есть они всё равно ложились в базу, которая после каждого прогона
# уезжает в репозиторий. Теперь отсев на сборе.
_федеральные29 = [
    "Федеральный закон от 24.09.2026 № 300-ФЗ \"О внесении изменений\"",
    "Указ Президента Российской Федерации от 22.09.2026 № 700",
    "Постановление Правительства Российской Федерации от 24.09.2026 № 1232",
    "Приказ Министерства финансов Российской Федерации от 02.06.2026 № 425",
    "Приказ Минфина России от 02.06.2026 № 425 \"Об утверждении порядка\"",
    "Указание Банка России от 15.09.2026 № 6700-У",
    "Приказ Федеральной службы по финансовому мониторингу от 19.06.2026 № 145",
    "Постановление Конституционного Суда Российской Федерации от 22.09.2026 № 54-П",
]
_региональные29 = [
    "Закон Камчатского края от 24.09.2026 № 621 \"О внесении изменений\"",
    "Постановление Правительства Красноярского края от 22.09.2026 № 700-п",
    "Приказ Министерства финансов Республики Алтай от 18.09.2026 № П-08-01/278",
    "Постановление Кабинета Министров Чувашской Республики от 20.09.2026 № 500",
    "Указ Главы Республики Башкортостан от 22.09.2026 № УГ-951",
    "Постановление Губернатора Краснодарского края от 17.09.2026 № 611",
    "Решение Управления Алтайского края по государственному регулированию "
    "цен и тарифов от 11.09.2026 № 120",
    "Постановление Региональной службы по тарифам и ценам Камчатского края "
    "от 23.09.2026 № 108-Н",
    "Сообщение о принятии решения Томского областного суда от 15.09.2026",
    "Закон Кемеровской области - Кузбасса от 24.09.2026 № 103-ОЗ",
    "Постановление Совета министров Республики Крым от 21.09.2026 № 609",
]
for _t29 in _федеральные29:
    check(f"остаётся: {_t29[:52]}", _fed29(_t29), "федеральный акт потерян")
for _t29 in _региональные29:
    check(f"отсеивается: {_t29[:50]}", not _fed29(_t29))

# Отбор смотрит только на издателя. Федеральный акт про любой регион —
# всё равно федеральный, и терять его нельзя.
check("регион в названии не делает акт региональным",
      _fed29("Постановление Правительства Российской Федерации от 24.09.2026 "
             "№ 1232 \"О развитии Камчатского края\""),
      "отбор залез в название вместо издателя")
check("акт без даты в названии не роняет отбор",
      _fed29("Федеральный закон о рынке ценных бумаг") is True)
check("пустое название не роняет отбор", _fed29("") is False and _fed29(None) is False)


# --------------------------- отчёт должен приходить в обещанное время
section("30. Отчёт приходит ровно в 9:30")
import re as _re30
from datetime import datetime as _dt30, timezone as _tz30, timedelta as _td30
from regwatch.agent import Agent as _Ag30

# Планировщик GitHub не пунктуален: задачи в этом репозитории стартовали
# с опозданием от 15 до 27 минут, и отчёт приходил в 9:51–10:00 вместо
# обещанных 9:30. Лечится не сдвигом расписания — так отчёт просто начнёт
# приходить «когда получится» в другую сторону, — а разделением: сбор
# заранее, рассылка по часам.
_MSK30 = _tz30(_td30(hours=3))


def _at30(h, m):
    return _dt30(2026, 10, 3, h, m, tzinfo=_MSK30)


check("сбор раньше срока — ждём до минуты",
      _Ag30.seconds_until("09:30", _at30(8, 43))[0] == 47 * 60)
check("за минуту до срока — ждём минуту",
      _Ag30.seconds_until("09:30", _at30(9, 29))[0] == 60)
# Опоздание планировщика больше запаса — отчёт нужен сразу, а не завтра.
check("срок прошёл — рассылаем немедленно",
      _Ag30.seconds_until("09:30", _at30(9, 35))[0] == 0)
check("ровно в срок — не ждём", _Ag30.seconds_until("09:30", _at30(9, 30))[0] == 0)
check("дольше полутора часов не ждём никогда",
      _Ag30.seconds_until("09:30", _at30(7, 0))[0] == 0,
      "иначе сбой расписания подвесит прогон на полдня")
for _bad30 in ("абвгд", "25:99", "", "9", None):
    check(f"битое время не подвешивает прогон: {_bad30!r}",
          _Ag30.seconds_until(_bad30, _at30(8, 40))[0] == 0)

# Режим прогона выбирается сравнением строки расписания. Если поправить
# cron и забыть сравнение,каждый прогон станет срочным, а ежедневный отчёт
# тихо перестанет выходить — заметить это можно только по его отсутствию.
_wf30 = (Path(__file__).resolve().parent / ".github" / "workflows" / "regwatch.yml") \
    .read_text(encoding="utf-8")
_crons30 = _re30.findall(r'- cron: "([^"]+)"', _wf30)
_daily30 = _re30.search(r'github\.event\.schedule \}\}" = "([^"]+)"[^\n]*MODE=daily', _wf30)
check("расписание прочитано", len(_crons30) >= 2, str(_crons30))
check("ежедневный отчёт опознаётся по своему расписанию",
      bool(_daily30) and _daily30.group(1) in _crons30,
      f"в шаге сравнивается {_daily30.group(1) if _daily30 else '—'}, "
      f"а в расписании {_crons30}")
check("рассылка ежедневного отчёта привязана ко времени",
      "--not-before 09:30" in _wf30,
      "без этого отчёт снова будет приходить «когда получится»")
# Одновременные прогоны запрещены, поэтому срочная проверка не должна
# попадать в окно, пока ежедневный отчёт ждёт своего часа.
if _daily30:
    _h30 = int(_daily30.group(1).split()[1])
    _alert30 = [c for c in _crons30 if c != _daily30.group(1)]
    _hours30 = set()
    for c in _alert30:
        f = c.split()[1]
        _hours30 |= ({int(x) for x in f.split(",")} if "," in f
                     else set(range(0, 24, int(f.split("/")[1]))) if "/" in f
                     else {int(f)})
    check("срочная проверка не встаёт в очередь за ежедневным отчётом",
          (_h30 + 1) % 24 not in _hours30 and _h30 not in _hours30,
          f"ежедневный в {_h30}:xx UTC, срочные в {sorted(_hours30)}")


# ------------------------------------------------------------------ итог
print("\n" + "=" * 66)
tail = f"   Пропущено: {len(SKIPPED)}" if SKIPPED else ""
print(f"Пройдено: {len(PASS)}   Провалено: {len(FAIL)}{tail}")
if FAIL:
    print("\nНе прошли:")
    for f in FAIL:
        print(f"   ✗ {f}")
sys.exit(1 if FAIL else 0)
