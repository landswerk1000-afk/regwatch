"""Web push: подписки, отправка, выгрузка отчётов в веб-приложение.

Канал устроен так, чтобы молчания при сбое не было никогда:
  * push уходит на все зарегистрированные устройства;
  * подписка, на которую сервис ответил 404 или 410, удаляется — устройство
    отписалось или исчезло, держать её бессмысленно;
  * если активных подписок нет или ни одна доставка не удалась, вызывающий код
    узнаёт об этом из результата и поднимает резервный канал.

Шифрование выполняется отдельным процессом в .venv-push (см. _push_sender.py),
поэтому ядро агента остаётся без внешних зависимостей.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

SENDER = Path(__file__).with_name("_push_sender.py")
VENV = ".venv-push"
# Имя, которое даёт отчётам agent.run(): 2026-09-24_1017_daily.html
REPORT_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{4}_(daily|alert)\.html$")
MAX_TITLE = 120
MAX_BODY = 300


class PushNotConfigured(Exception):
    pass


def _interpreter(root: Path, module: str) -> str | None:
    """Чем запускать вспомогательный процесс.

    На компьютере зависимости изолированы в отдельном окружении, чтобы ядро
    агента оставалось без них. На сервере сборки окружения нет — библиотеки
    ставятся прямо в систему, и тогда годится текущий интерпретатор.
    """
    import subprocess as _sp
    import sys as _sys
    p = root / VENV / "bin" / "python"
    if p.exists():
        return str(p)
    try:
        _sp.run([_sys.executable, "-c", f"import {module}"], check=True,
                capture_output=True, timeout=60)
        return _sys.executable
    except Exception:
        return None


def venv_python(root: Path):
    return _interpreter(root, "pywebpush")


def vapid() -> dict:
    return {
        "private": os.environ.get("VAPID_PRIVATE_KEY", "").strip(),
        "public": os.environ.get("VAPID_PUBLIC_KEY", "").strip(),
        "subject": os.environ.get("VAPID_SUBJECT", "").strip() or "mailto:admin@example.com",
    }


def configured(root: Path) -> bool:
    return bool(vapid()["private"]) and venv_python(root) is not None


def check(root: Path) -> str | None:
    """Возвращает описание проблемы или None, если всё на месте."""
    if not vapid()["private"]:
        return "нет VAPID_PRIVATE_KEY в ~/.regwatch.env"
    if venv_python(root) is None:
        return ("нет pywebpush — создайте окружение: "
                "python3 -m venv .venv-push && ./.venv-push/bin/pip install pywebpush")
    if not SENDER.exists():
        return f"нет файла отправщика {SENDER.name}"
    return None


def parse_subscription(raw: str) -> dict:
    """Разбирает код устройства, скопированный со страницы приложения."""
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("пустой код")
    try:
        d = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"это не JSON: {e}") from e
    endpoint = d.get("endpoint")
    keys = d.get("keys") or {}
    if not endpoint or not keys.get("p256dh") or not keys.get("auth"):
        raise ValueError("в коде нет endpoint или ключей p256dh/auth")
    return {"endpoint": endpoint, "p256dh": keys["p256dh"],
            "auth": keys["auth"], "device": d.get("device")}


def _rows_to_subs(rows) -> list:
    return [{"endpoint": r["endpoint"],
             "keys": {"p256dh": r["p256dh"], "auth": r["auth"]}} for r in rows]


def _subscriptions_from_env():
    """Подписки из REGWATCH_PUSH_SUBSCRIPTIONS, если переменная задана.

    Формат — то же, что отдаёт `push-list --json`: список объектов
    {"endpoint": ..., "keys": {"p256dh": ..., "auth": ...}}.
    Пустая или испорченная переменная означает «брать из базы», а не
    «отправлять некуда»: молчание из-за опечатки в секрете недопустимо.
    """
    raw = os.environ.get("REGWATCH_PUSH_SUBSCRIPTIONS", "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict):
        data = [data]
    out = []
    for d in data or []:
        keys = d.get("keys") or {}
        if d.get("endpoint") and keys.get("p256dh") and keys.get("auth"):
            out.append({"endpoint": d["endpoint"], "keys": dict(keys)})
    return out or None


def send(root: Path, store, payload: dict) -> dict:
    """Рассылает payload на все подписки. Возвращает сводку доставки."""
    problem = check(root)
    if problem:
        raise PushNotConfigured(problem)

    # Подписки могут приходить из окружения — это нужно, когда агент работает
    # на сервере сборки: база состояния там лежит в публичном репозитории,
    # а адрес устройства в нём публиковать нельзя. В таком режиме подписки
    # только читаются: регистрация остаётся ручной операцией на компьютере.
    external = _subscriptions_from_env()
    if external is not None:
        rows = external
        readonly = True
    else:
        rows = store.subscriptions()
        readonly = False

    if not rows:
        return {"sent": 0, "failed": 0, "dropped": 0, "total": 0,
                "detail": "нет зарегистрированных устройств"}

    subs = rows if readonly else _rows_to_subs(rows)
    task = {"vapid": vapid(), "payload": payload, "subscriptions": subs}
    proc = subprocess.run(
        [str(venv_python(root)), str(SENDER)],
        input=json.dumps(task, ensure_ascii=False), capture_output=True,
        text=True, timeout=120)

    try:
        out = json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception:
        raise RuntimeError(
            f"отправщик не ответил: {(proc.stderr or proc.stdout or '')[:200]}")
    if out.get("fatal"):
        raise PushNotConfigured(out["fatal"])

    sent = failed = dropped = 0
    for r in out.get("results", []):
        if r.get("ok"):
            sent += 1
            if not readonly:
                store.subscription_result(r["endpoint"], True)
        elif r.get("status") in (404, 410):
            # Устройство отписалось — подписка больше не действительна.
            dropped += 1
            if not readonly:
                store.drop_subscription(r["endpoint"])
        else:
            failed += 1
            if not readonly:
                store.subscription_result(r["endpoint"], False, r.get("error"))

    detail = f"доставлено {sent} из {len(rows)}"
    if dropped:
        detail += f", удалено устаревших {dropped}"
    if failed:
        detail += f", ошибок {failed}"
    return {"sent": sent, "failed": failed, "dropped": dropped,
            "total": len(rows), "detail": detail}


# ---------- формирование уведомления ----------

def _closest_deadline(deadlines) -> str | None:
    """Короткая фраза о ближайшем сроке замечаний — для тела уведомления."""
    if not deadlines:
        return None
    from ..util import now_msk
    today = now_msk().date()
    dt = deadlines[0][0]
    days = (dt.date() - today).days
    if days < 0:
        return None
    n = len(deadlines)
    when = ("Срок замечаний сегодня" if days == 0
            else "Срок замечаний завтра" if days == 1
            else f"Ближайший срок замечаний — через {days} дн.")
    return when if n == 1 else f"{when} (всего сроков: {n})"

def payload_for(buckets: dict, period_label: str, alert_mode: bool,
                base_url: str = "", deadlines=None) -> dict:
    """Собирает короткое уведомление из тех же данных, что и остальные каналы.

    Сроки подачи замечаний упоминаются даже в спокойный день: это
    единственное в отчёте с жёстким дедлайном. 27 сентября уведомление
    сказало «значимых изменений нет», хотя через два дня истекал срок
    по проекту указания ЦБ, — а открывать отчёт после такой фразы
    у человека нет причины.
    """
    from ..report import URGENCY_META, ORDER
    from ..util import now_msk, squeeze

    counts = {u: len(buckets.get(u) or []) for u in ORDER}
    total = sum(counts.values())
    critical = counts["critical"]

    if alert_mode and critical:
        top = buckets["critical"][0]["row"]
        title = "Срочно: " + squeeze(top["title"], MAX_TITLE - 9)
        bits = [top["authority"]]
        if top["stage"]:
            bits.append(squeeze(top["stage"], 90))
        body = " · ".join(bits)
        url = top["url"] or (base_url or "./")
        level = "critical"
    else:
        title = f"Регмонитор · {period_label}"
        soon = _closest_deadline(deadlines)
        if not total:
            body = "Значимых изменений нет"
            if soon:
                body += f". {soon}"
        else:
            parts = []
            if critical:
                parts.append(f"{critical} требует решения")
            if counts["high"]:
                parts.append(f"{counts['high']} важных")
            parts.append(f"всего {total}")
            body = ", ".join(parts)
            if soon:
                body += f". {soon}"
        url = base_url or "./"
        # Срок, истекающий сегодня или завтра, поднимает важность: такое
        # уведомление не должно теряться среди обычных.
        urgent_deadline = bool(soon and ("сегодня" in soon or "завтра" in soon))
        level = "critical" if (critical or urgent_deadline) else "normal"

    return {"title": squeeze(title, MAX_TITLE), "body": squeeze(body, MAX_BODY),
            "url": url, "level": level, "tag": "alert" if alert_mode else "daily"}


# ---------- выгрузка отчётов в веб-приложение ----------

def _human_stamp(stem: str) -> str:
    """«2026-09-27_0946» → «27 сентября, 09:46».

    Машинная дата в списке отчётов читается хуже обычной, а два отчёта
    за один день без времени вообще не различить.
    """
    from ..report import MONTHS
    try:
        date_part, time_part = stem.split("_")[0], stem.split("_")[1]
        y, m, d = (int(x) for x in date_part.split("-"))
        return f"{d} {MONTHS[m - 1]}, {time_part[:2]}:{time_part[2:4]}"
    except Exception:
        return stem

def export_reports(root: Path, reports_dir: Path, limit: int = 12) -> int:
    """Копирует свежие HTML-отчёты в webapp/ и пишет latest.json для страницы.

    Всё кладётся ПЛОСКО, в корень webapp/, без вложенной папки. Причина
    практическая: приложение публикуется перетаскиванием файлов в веб-форму
    GitHub, а она разворачивает папки в корень — вложенность там не переживает
    загрузку, и ссылки из latest.json обрываются. Плоская раскладка переносится
    как есть.
    """
    webapp = root / "webapp"
    if not webapp.exists():
        return 0

    # Складываем свежие отчёты с теми, что уже лежат в приложении. Это важно
    # там, где агент работает на сервере сборки: папка reports/ там пустая
    # при каждом запуске, и без объединения выгрузка снесла бы всю историю,
    # оставив на сайте единственный сегодняшний отчёт.
    fresh = {f.name: f for f in reports_dir.glob("*.html") if REPORT_NAME.match(f.name)}
    already = {f.name: f for f in webapp.glob("*.html") if REPORT_NAME.match(f.name)}
    merged = {**already, **fresh}          # свежая копия важнее старой

    # Имя начинается с даты и времени, поэтому сортировка по имени —
    # это сортировка по времени, и она не зависит от отметок файловой системы.
    names = sorted(merged, reverse=True)[:limit]
    files = [merged[n] for n in names]

    keep = set(names)
    for old in webapp.glob("*.html"):
        if REPORT_NAME.match(old.name) and old.name not in keep:
            old.unlink()

    # Прежняя раскладка с подпапкой — убираем, чтобы не осталось двух копий.
    legacy = webapp / "reports"
    if legacy.is_dir():
        shutil.rmtree(legacy, ignore_errors=True)

    # Подписи прошлых отчётов берём из прежнего указателя: рядом с ними
    # на сервере нет markdown-файла — он создаётся заново каждый прогон
    # и в репозиторий не уезжает. Без этого вчерашние отчёты теряли подпись.
    known = {}
    old_index = webapp / "latest.json"
    if old_index.exists():
        try:
            for r in json.loads(old_index.read_text(encoding="utf-8")).get("reports", []):
                if r.get("file"):
                    known[r["file"]] = r
        except Exception:
            pass

    index = []
    for f in files:
        if f.resolve() != (webapp / f.name).resolve():
            shutil.copy2(f, webapp / f.name)

        md = f.with_suffix(".md")
        summary = None
        if md.exists():
            text = md.read_text(encoding="utf-8")
            for line in text.splitlines():
                if line.startswith("По органам:"):
                    summary = line.replace("По органам:", "").strip()
                    break
            if summary is None:
                # Пустой отчёт — это тоже сведение, и «подписи нет» читается
                # как поломка. Говорим прямо.
                summary = "значимых изменений нет"
        if summary is None:
            summary = (known.get(f.name) or {}).get("summary", "")

        kind = "Срочное уведомление" if "_alert" in f.name else "Ежедневный отчёт"
        index.append({"file": f.name,
                      "title": f"{kind} — {_human_stamp(f.stem)}",
                      "summary": summary})

    # Ссылка на несуществующий файл хуже отсутствия ссылки: человек жмёт
    # и получает «страница не найдена», решая, что сломан весь агент.
    index = [r for r in index if (webapp / r["file"]).exists()]

    (webapp / "latest.json").write_text(
        json.dumps({"reports": index}, ensure_ascii=False, indent=1), encoding="utf-8")
    return len(index)

def export_documents(root: Path, store, floor: float = 0.45, limit: int = 1500) -> int:
    """Выкладывает указатель документов рядом с отчётами.

    Приложение до сих пор было устроено вокруг отчётов: «вот отчёт за
    28 сентября». Но юрист думает документами — «что у нас по ЦФА». Найти
    конкретный проект указания можно было только открывая отчёты по одному,
    а их к декабрю будет под сотню.

    Данные для этого давно копятся, не хватало только вида. Кладём их
    отдельным файлом: страница указателя работает без сервера, как и отчёт.
    """
    webapp = root / "webapp"
    if not webapp.exists():
        return 0

    COLS = """SELECT i.id, i.title, i.authority, i.url, i.stage, i.published_at,
                  i.first_seen, i.meta, s.topics,
                  (SELECT COUNT(*) FROM events e
                   WHERE e.item_id = i.id AND e.event_type != 'new') AS changes
           FROM items i LEFT JOIN scores s ON s.item_id = i.id"""

    rows = store.db.execute(
        COLS + """ WHERE s.relevance >= ?
           ORDER BY COALESCE(i.published_at, i.first_seen) DESC
           LIMIT ?""", (floor, limit)).fetchall()

    # Отдельно — то, у чего открыт срок замечаний. Оценка значимости тут
    # не судья: пять проектов указаний ЦБ с открытым обсуждением набрали
    # ноль (словарь тем до них не дотянулся), а это ровно те документы,
    # по которым ещё можно что-то сказать. В отчёте блок сроков давно
    # живёт по тому же правилу — указатель не должен быть строже.
    known = {r["id"] for r in rows}
    extra = [r for r in store.db.execute(
        COLS + " WHERE i.meta LIKE '%comments_until%'").fetchall()
        if r["id"] not in known]

    from ..report import _stage_short, MONTHS
    from ..util import parse_dt, squeeze, now_msk
    from datetime import timedelta

    stale = now_msk() - timedelta(days=60)
    picked = list(rows)
    for r in extra:
        try:
            raw = (json.loads(r["meta"] or "{}") or {}).get("comments_until")
        except Exception:
            raw = None
        dt = parse_dt(raw) if raw else None
        # Совсем старые обсуждения не тянем: они уже история, а не работа.
        if dt and dt >= stale:
            picked.append(r)

    docs = []
    for r in picked:
        try:
            meta = json.loads(r["meta"] or "{}")
        except Exception:
            meta = {}
        try:
            topics = json.loads(r["topics"] or "[]")
        except Exception:
            topics = []
        code, stage = _stage_short(r["stage"])
        dt = parse_dt(r["published_at"]) or parse_dt(r["first_seen"])
        docs.append({
            "id": r["id"],
            "title": squeeze(r["title"], 300),
            "org": r["authority"],
            "url": r["url"] or "",
            "stage": squeeze(stage, 140) if stage else "",
            "code": code or "",
            "date": f"{dt.day} {MONTHS[dt.month - 1]} {dt.year}" if dt else "",
            "sort": (r["published_at"] or r["first_seen"] or "")[:10],
            "topics": topics[:4],
            "deadline": meta.get("comments_until") or "",
            # Машинная дата рядом с человеческой: по ней страница отличает
            # открытый срок от прошедшего, не разбирая «25 сентября 2026».
            "deadline_iso": (lambda d: d.strftime("%Y-%m-%d") if d else "")(
                parse_dt(meta.get("comments_until")) if meta.get("comments_until") else None),
            # Документ, сменивший стадию, — новость крупнее нового: он живёт
            # и движется. Отмечаем, чтобы это было видно в списке.
            "changed": int(r["changes"] or 0),
        })

    # Сортируем уже в Python: во втором запросе свой порядок, и без этого
    # документы со сроком замечаний оказались бы хвостом списка.
    docs.sort(key=lambda d: d["sort"], reverse=True)
    del docs[limit:]

    (webapp / "documents.json").write_text(
        json.dumps({"updated": iso_now(), "total": len(docs), "documents": docs},
                   ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8")
    return len(docs)


def iso_now() -> str:
    from ..util import now_utc, iso
    return iso(now_utc())
