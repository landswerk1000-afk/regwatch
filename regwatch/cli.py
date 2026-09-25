"""Командный интерфейс агента."""
from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from pathlib import Path

from .agent import Agent
from .config import Config, load_env_file
from .sources import ALL_SOURCES, enabled


def _setup_logging(verbose: bool, root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    handlers = [logging.FileHandler(root / "regwatch.log", encoding="utf-8")]
    if verbose:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(
        level=logging.INFO, handlers=handlers,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")


def _check_proxy(cfg: Config, http) -> int:
    """Путь к госсайтам проверяется делом: доходит ли он до СОЗД."""
    probe = "https://sozd.duma.gov.ru/oz"

    if cfg.relay_url and cfg.relay_token:
        print(f"Ретранслятор: {cfg.relay_url}")
        print(f"  через него идут: {', '.join(cfg.proxy_hosts) or 'весь трафик'}")
        print(f"  проверяю доступ к {probe} …", end=" ", flush=True)
        r = http.get_or_none(probe, timeout=60, retries=1)
        if r:
            print(f"ok ({r.status}, {len(r.body)} байт)")
            return 0
        print("НЕ ДОХОДИТ")
        print("  Проверьте: функция опубликована, TOKEN на ней совпадает")
        print("  с REGWATCH_RELAY_TOKEN, домен есть в списке ALLOWED.")
        print("  Подробности:  python3 -m regwatch relay-test")
        return 1

    if cfg.relay_url and not cfg.relay_token:
        print("Ретранслятор: адрес задан, а токена нет")
        print("  Впишите REGWATCH_RELAY_TOKEN в ~/.regwatch.env")
        return 1

    if not cfg.proxy_url:
        print("Прокси: — не настроен —")
        print("  Без него недоступны СОЗД и regulation.gov.ru (портал ГД режет")
        print("  TLS с не-российских адресов). См. раздел README про туннель.")
        return 0

    masked = re.sub(r"://[^@/]*@", "://***@", cfg.proxy_url)
    print(f"Прокси: {masked}  [{http.proxy_kind or '?'}]")
    print(f"  через него идут: {', '.join(cfg.proxy_hosts) or 'весь трафик'}")

    print(f"  проверяю доступ к {probe} …", end=" ", flush=True)
    r = http.get_or_none(probe, timeout=40, retries=1)
    if r:
        print(f"ok ({r.status}, {len(r.body)} байт)")
        return 0
    print("НЕ ДОХОДИТ")
    print("  Туннель поднят? Проверьте:  nc -z 127.0.0.1 1080  и  ./tunnel.sh status")
    return 1


def cmd_doctor(cfg: Config, args) -> int:
    """Проверка каждого источника, прокси и почты — до запуска по расписанию."""
    http = cfg.make_http()
    failures = _check_proxy(cfg, http)
    print(f"База:   {cfg.db_path}")
    print(f"Отчёты: {cfg.reports_dir}\n")

    active = {s.id for s in enabled(cfg)}
    print(f"{'источник':<20}{'орган':<14}{'статус':<10}{'док.':>6}  примечание")
    print("-" * 92)
    for s in ALL_SOURCES:
        if s.id not in active:
            print(f"{s.id:<20}{s.authority:<14}{'выкл':<10}{'—':>6}  {s.note}")
            continue
        if s.requires_proxy and not cfg.proxy_url:
            print(f"{s.id:<20}{s.authority:<14}{'нет прокси':<10}{'—':>6}  {s.note}")
            continue
        if s.id == "duma_api":
            from .sources import duma_api as _da
            if not _da.configured():
                print(f"{s.id:<20}{s.authority:<14}{'нет ключей':<10}{'—':>6}  "
                      f"получите на api.duma.gov.ru/key-request")
                continue
        t = time.time()
        res = s.run(http)
        dt = time.time() - t
        if res.ok:
            print(f"{s.id:<20}{s.authority:<14}{'ok':<10}{len(res.items):>6}  {dt:.1f}s")
        else:
            failures += 1
            print(f"{s.id:<20}{s.authority:<14}{'СБОЙ':<10}{'—':>6}  {res.error[:52]}")

    print()
    em = cfg.email
    if not em.get("enabled"):
        print("Почта: выключена (email.enabled = false в config.json)")
    elif not cfg.smtp_password:
        print(f"Почта: ВКЛЮЧЕНА, но пароль пуст — задайте ${em.get('password_env')}")
        failures += 1
    else:
        print(f"Почта: {em['from_addr']} → {', '.join(em['to'])} через {em['smtp_host']}:{em['smtp_port']}")
    return 1 if failures else 0


def cmd_collect(cfg: Config, args) -> int:
    a = Agent(cfg)
    try:
        st = a.collect()
        print(f"Источников ок: {st['sources_ok']}, сбоев: {st['sources_failed']}")
        print(f"Обработано документов: {st['items']}  ·  новых: {st['new']}  ·  изменений: {st['changed']}")
        if st["skipped_proxy"]:
            print(f"Пропущены без прокси: {', '.join(st['skipped_proxy'])}")
    finally:
        a.close()
    return 0


def cmd_run(cfg: Config, args) -> int:
    a = Agent(cfg)
    try:
        out = a.run(alert_mode=args.alert, dry_run=args.dry_run,
                    skip_collect=args.no_collect, mark=not args.keep)
        st = out["stats"] or {}
        if st:
            print(f"Сбор: {st['sources_ok']} ок / {st['sources_failed']} сбоев · "
                  f"новых {st['new']}, изменений {st['changed']}")
        print(f"{out['label']}: {out['items']} документов "
              f"(срочных {out['critical']}, важных {out['high']})")
        if out.get("filtered_out"):
            print(f"Отсеяно фильтром: {out['filtered_out']}")
        if out["path"]:
            print(f"Файл:     {out['path']}")
        print(f"Доставка: {out['delivery']}")
    finally:
        a.close()
    return 0


def cmd_baseline(cfg: Config, args) -> int:
    """Первичный прогон: наполнить базу, ничего не отправляя."""
    a = Agent(cfg)
    try:
        st = a.collect()
        a.store.db.execute("UPDATE events SET reported=1, alerted=1")
        a.store.db.commit()
        print(f"База заполнена: {st['items']} документов, {st['new']} записей помечены как известные.")
        print("Следующий прогон покажет только новое.")
    finally:
        a.close()
    return 0


def cmd_rescore(cfg: Config, args) -> int:
    a = Agent(cfg)
    try:
        n = a.rescore_all()
        print(f"Пересчитано документов: {n}")
    finally:
        a.close()
    return 0


def cmd_stats(cfg: Config, args) -> int:
    a = Agent(cfg)
    try:
        s = a.store.stats()
        print(f"Документов: {s['items']}  ·  событий: {s['events']}  ·  "
              f"неотправленных: {s['unreported']}  ·  прогонов: {s['runs']}")
        rows = a.store.db.execute(
            """SELECT i.authority, COUNT(*) n, ROUND(AVG(sc.relevance),2) avg_rel
               FROM items i LEFT JOIN scores sc ON sc.item_id=i.id
               GROUP BY i.authority ORDER BY n DESC""").fetchall()
        print(f"\n{'орган':<16}{'док.':>7}{'ср. релевантность':>20}")
        for r in rows:
            print(f"{r['authority'] or '—':<16}{r['n']:>7}{str(r['avg_rel'] or 0):>20}")
        top = a.store.db.execute(
            """SELECT i.title, sc.relevance, sc.urgency FROM items i
               JOIN scores sc ON sc.item_id=i.id
               ORDER BY sc.relevance DESC LIMIT 8""").fetchall()
        if top:
            print("\nСамое релевантное в базе:")
            for r in top:
                print(f"  {r['relevance']:.2f} {r['urgency']:<9} {r['title'][:70]}")
    finally:
        a.close()
    return 0


def cmd_proxies(cfg: Config, args) -> int:
    """Найти рабочий бесплатный российский прокси — запасной путь к СОЗД."""
    from . import proxypool

    print("Запасной путь к СОЗД через публичные российские прокси.")
    print("Через него пойдут ТОЛЬКО домены ГД — почта и всё прочее идут напрямую.\n")

    http = cfg.make_http()
    print("Собираю кандидатов из открытых списков…")
    cands = proxypool.fetch_candidates(http, limit=args.limit)
    if not cands:
        print("Списки недоступны. Попробуйте позже или поднимите туннель (README).")
        return 1
    print(f"Кандидатов: {len(cands)}. Проверяю каждого на {proxypool.PROBE_URL}")
    print("Бесплатные прокси в массе мертвы — это ожидаемо, ждём несколько минут.\n")

    seen = {"n": 0}

    def report(url, ok, dt, detail):
        seen["n"] += 1
        if ok:
            print(f"  РАБОТАЕТ  {url:<36} {dt:.1f}s")
        elif args.verbose:
            print(f"  нет       {url:<36} {detail[:44]}")
        elif seen["n"] % 25 == 0:
            print(f"  …проверено {seen['n']} из {len(cands)}")

    working = proxypool.find_working(cands, want=args.want, on_result=report)
    print(f"\nПроверено: {seen['n']}   Рабочих: {len(working)}")
    if not working:
        print("\nНи один не дошёл до СОЗД. Для бесплатных списков это обычный исход:")
        print("они почти все в дата-центрах, а СОЗД режет именно такие адреса.")
        print("Надёжный путь — туннель через свою машину в России (README).")
        return 1

    path = proxypool.save(cfg.root, working)
    print(f"\nСохранено: {path}")
    print(f"Лучший: {working[0]['url']}  ({working[0]['seconds']}s)\n")
    print("Чтобы агент им пользовался, впишите в ~/.regwatch.env:")
    print(f"  REGWATCH_PROXY={working[0]['url']}")
    print("и включите источники в config.json:")
    print('  "sources": {"sozd": {"enabled": true}, "regulation": {"enabled": true}}')
    return 0


def cmd_telegram_setup(cfg: Config, args) -> int:
    """Находит канал, куда добавлен бот, и записывает его id в ~/.regwatch.env."""
    from .deliver import telegram as tg
    from pathlib import Path
    import re as _re

    if not tg.token():
        print("Нет TELEGRAM_BOT_TOKEN в ~/.regwatch.env")
        return 1
    me = tg.get_me()
    if not me.get("ok"):
        print(f"Бот не отвечает: {me.get('description')}")
        return 1
    print(f"Бот: @{me['result']['username']}\n")

    chats = tg.discover_chats()
    if chats and "error" in chats[0]:
        print(f"Ошибка: {chats[0]['error']}")
        return 1
    if not chats:
        print("Бот пока нигде не состоит. Что нужно сделать:")
        print("  1. В Telegram создайте канал (Новый канал).")
        print("  2. Настройки канала -> Администраторы -> Добавить -> "
              f"найдите @{me['result']['username']}")
        print("  3. Дайте ему право «Публикация сообщений» и сохраните.")
        print("  4. Напишите в канал любое слово — чтобы бот его увидел.")
        print("  5. Запустите эту команду снова.")
        return 1

    print("Найдено:")
    for c in chats:
        print(f"   {c['id']:<16} {c['type']:<10} {c['title']}")

    preferred = [c for c in chats if c["type"] == "channel"] or chats
    target = preferred[0]
    env = Path.home() / ".regwatch.env"
    text = env.read_text(encoding="utf-8")
    line = f"TELEGRAM_CHAT_ID={target['id']}"
    if _re.search(r"^TELEGRAM_CHAT_ID=.*$", text, _re.M):
        text = _re.sub(r"^TELEGRAM_CHAT_ID=.*$", line, text, flags=_re.M)
    else:
        text = text.rstrip() + "\n" + line + "\n"
    env.write_text(text, encoding="utf-8")
    env.chmod(0o600)
    print(f"\nЗаписано: {line}  ({target['title']})")
    print("Проверить доставку: python3 -m regwatch telegram-test")
    return 0


def cmd_publish(cfg: Config, args) -> int:
    """Выкладывает webapp/ на GitHub Pages вручную."""
    from . import publish as pub
    problem = pub.check()
    if problem:
        print(f"Не настроено: {problem}")
        print()
        print("Нужны две строки в ~/.regwatch.env:")
        print("  GITHUB_REPO=логин/regwatch")
        print("  GITHUB_TOKEN=токен с правом записи в этот репозиторий")
        return 1
    try:
        res = pub.publish(cfg.root)
    except Exception as e:
        print(f"Ошибка: {type(e).__name__}: {e}")
        return 1
    print(res["detail"])
    if res.get("url"):
        print(f"Сайт: {res['url']}")
    return 0


def cmd_relay_test(cfg: Config, args) -> int:
    """Проверяет ретранслятор по шагам, чтобы было видно, что именно не так."""
    if not cfg.relay_url:
        print("Адрес функции не задан.")
        print("Впишите REGWATCH_RELAY_URL в ~/.regwatch.env — он показан")
        print("в консоли Яндекса на вкладке «Обзор» функции.")
        return 1
    if not cfg.relay_token:
        print("Токен не задан. Впишите REGWATCH_RELAY_TOKEN в ~/.regwatch.env —")
        print("тот же, что задан переменной TOKEN на самой функции.")
        return 1

    print(f"Функция: {cfg.relay_url}")
    print(f"Токен:   {cfg.relay_token[:4]}…{cfg.relay_token[-2:]} ({len(cfg.relay_token)} симв.)")
    print()

    http = cfg.make_http()
    checks = [
        ("Госдума (СОЗД)", "https://sozd.duma.gov.ru/oz"),
        ("Совет Федерации", "http://council.gov.ru/activity/documents/"),
        ("regulation.gov.ru", "https://regulation.gov.ru/projects"),
    ]
    bad = 0
    for name, url in checks:
        print(f"  {name:<20} …", end=" ", flush=True)
        try:
            r = http.get(url, timeout=60, retries=1)
            print(f"ok ({r.status}, {len(r.body)} байт, {r.elapsed:.1f}с)")
        except Exception as e:
            bad += 1
            print(f"СБОЙ — {e}")

    print()
    if bad:
        print("Что проверить:")
        print("  403 — токен на функции и в ~/.regwatch.env не совпадают")
        print("  400 — домена нет в списке ALLOWED внутри функции")
        print("  404 — адрес функции неверен или она не опубликована")
        print("  502 — функция жива, но сам госсайт не ответил")
        return 1
    print("Все госсайты доступны через ретранслятор.")
    print("Прокси больше не нужен: очистите REGWATCH_PROXY в ~/.regwatch.env.")
    return 0


def cmd_telegram_test(cfg: Config, args) -> int:
    from .deliver import telegram as tg, TelegramNotConfigured
    try:
        tg.send_message(
            "<b>Регмонитор на связи</b>\n"
            "Если вы читаете это сообщение, отчёты будут приходить сюда.")
        print("Отправлено. Проверьте канал.")
        return 0
    except TelegramNotConfigured as e:
        print(f"Не настроено: {e}")
        print("Запустите: python3 -m regwatch telegram-setup")
        return 1
    except Exception as e:
        print(f"Ошибка: {type(e).__name__}: {e}")
        return 1


def cmd_push_add(cfg: Config, args) -> int:
    """Регистрирует устройство по коду, скопированному со страницы приложения."""
    from .deliver import webpush
    from .agent import Agent

    raw = args.code
    if not raw:
        print("Вставьте код устройства со страницы приложения и нажмите Ctrl-D:\n")
        raw = sys.stdin.read()
    try:
        sub = webpush.parse_subscription(raw)
    except ValueError as e:
        print(f"Код не подошёл: {e}")
        return 1

    a = Agent(cfg)
    try:
        what = a.store.add_subscription(**sub)
        print(f"Подписка {what}. Устройство: {sub.get('device') or 'без названия'}")
        print(f"Всего устройств: {len(a.store.subscriptions())}")
        print("Проверить: python3 -m regwatch push-test")
    finally:
        a.close()
    return 0


def cmd_push_list(cfg: Config, args) -> int:
    from .agent import Agent
    from .deliver import webpush
    a = Agent(cfg)
    try:
        rows = a.store.subscriptions()
        # Выгрузка для секрета GitHub: когда агент работает на сервере сборки,
        # база лежит в публичном репозитории, и адресам устройств там не место.
        if getattr(args, "json", False):
            import json as _j
            print(_j.dumps(
                [{"endpoint": r["endpoint"],
                  "keys": {"p256dh": r["p256dh"], "auth": r["auth"]}} for r in rows],
                ensure_ascii=False))
            return 0
        problem = webpush.check(cfg.root)
        print(f"Готовность: {problem or 'всё настроено'}\n")
        if not rows:
            print("Устройств нет. Откройте приложение, включите уведомления")
            print("и передайте код: python3 -m regwatch push-add")
            return 0
        print(f"{'устройство':<18}{'добавлено':<12}{'последняя доставка':<20}сбоев")
        for r in rows:
            print(f"{(r['device'] or '—')[:17]:<18}{(r['created_at'] or '')[:10]:<12}"
                  f"{(r['last_ok'] or 'ещё не было')[:19]:<20}{r['fail_streak']}")
            if r["last_error"]:
                print(f"    последняя ошибка: {r['last_error'][:70]}")
    finally:
        a.close()
    return 0


def cmd_push_test(cfg: Config, args) -> int:
    from .agent import Agent
    from .deliver import webpush, PushNotConfigured
    a = Agent(cfg)
    try:
        res = webpush.send(cfg.root, a.store, {
            "title": "Регмонитор на связи",
            "body": "Если вы видите это уведомление, доставка настроена.",
            "url": "./", "level": "normal", "tag": "test"})
        print(res["detail"])
        return 0 if res["sent"] else 1
    except PushNotConfigured as e:
        print(f"Не настроено: {e}")
        return 1
    except Exception as e:
        print(f"Ошибка: {type(e).__name__}: {e}")
        return 1
    finally:
        a.close()


def cmd_push_export(cfg: Config, args) -> int:
    from .deliver import webpush
    n = webpush.export_reports(cfg.root, cfg.reports_dir)
    print(f"Выгружено отчётов в webapp/: {n}")
    if n:
        print("Опубликуйте папку webapp/ — см. раздел про web push в README.")
    return 0


def cmd_test_email(cfg: Config, args) -> int:
    from .deliver import send_email, EmailNotConfigured
    html = ("<div style=\"font-family:-apple-system,Segoe UI,Roboto,sans-serif;padding:20px\">"
            "<h2>Регмонитор на связи</h2><p>Если вы читаете это письмо, доставка настроена.</p></div>")
    try:
        print(send_email(cfg.email, cfg.smtp_password, "проверка доставки",
                         html, "Регмонитор: проверка доставки.", dry_run=args.dry_run))
        return 0
    except EmailNotConfigured as e:
        print(f"Не настроено: {e}")
        return 1
    except Exception as e:
        print(f"Ошибка отправки: {type(e).__name__}: {e}")
        return 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="regwatch",
        description="Мониторинг регулирования частных инвестиций: ЦБ, Минфин, ГД, СФ")
    p.add_argument("-c", "--config", default=None, help="путь к config.json")
    p.add_argument("-v", "--verbose", action="store_true", help="лог в stderr")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="проверить источники, прокси и почту")
    sub.add_parser("collect", help="только собрать, без отчёта")
    sub.add_parser("baseline", help="первичное наполнение базы без рассылки")
    sub.add_parser("rescore", help="пересчитать релевантность после правки topics.json")
    sub.add_parser("stats", help="что накоплено в базе")

    r = sub.add_parser("run", help="полный цикл: сбор, отчёт, отправка")
    r.add_argument("--alert", action="store_true", help="режим срочного уведомления")
    r.add_argument("--dry-run", action="store_true", help="не отправлять письмо")
    r.add_argument("--no-collect", action="store_true", help="не собирать, отчёт по накопленному")
    r.add_argument("--keep", action="store_true", help="не помечать события отправленными")

    pr = sub.add_parser("proxies", help="найти рабочий бесплатный российский прокси")
    pr.add_argument("--limit", type=int, default=200, help="сколько кандидатов проверить")
    pr.add_argument("--want", type=int, default=5, help="остановиться, найдя N рабочих")
    pr.add_argument("--verbose", action="store_true", help="показывать и неудачные")

    sub.add_parser("publish", help="выложить приложение и отчёты на GitHub Pages")
    sub.add_parser("relay-test", help="проверить ретранслятор в Yandex Cloud")
    sub.add_parser("telegram-setup", help="привязать канал Telegram")
    sub.add_parser("telegram-test", help="отправить пробное сообщение в Telegram")

    pa = sub.add_parser("push-add", help="зарегистрировать устройство для уведомлений")
    pa.add_argument("code", nargs="?", help="код устройства; без него читается из ввода")
    pl = sub.add_parser("push-list", help="устройства и состояние доставки")
    pl.add_argument("--json", action="store_true",
                    help="выгрузить подписки для секрета GitHub")
    sub.add_parser("push-test", help="пробное уведомление на все устройства")
    sub.add_parser("push-export", help="выгрузить отчёты в webapp/")

    te = sub.add_parser("test-email", help="отправить пробное письмо")
    te.add_argument("--dry-run", action="store_true")

    args = p.parse_args(argv)
    load_env_file()   # секреты из ~/.regwatch.env
    here = Path(__file__).resolve().parent.parent
    cfg = Config.load(args.config or here / "config.json")
    _setup_logging(args.verbose, cfg.root / "logs")

    return {
        "doctor": cmd_doctor, "collect": cmd_collect, "run": cmd_run,
        "baseline": cmd_baseline, "rescore": cmd_rescore, "stats": cmd_stats,
        "proxies": cmd_proxies, "test-email": cmd_test_email,
        "publish": cmd_publish,
        "relay-test": cmd_relay_test,
        "telegram-setup": cmd_telegram_setup, "telegram-test": cmd_telegram_test,
        "push-add": cmd_push_add, "push-list": cmd_push_list,
        "push-test": cmd_push_test, "push-export": cmd_push_export,
    }[args.cmd](cfg, args)
