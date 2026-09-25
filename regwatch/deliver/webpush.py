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

def payload_for(buckets: dict, period_label: str, alert_mode: bool,
                base_url: str = "") -> dict:
    """Собирает короткое уведомление из тех же данных, что и остальные каналы."""
    from ..report import URGENCY_META, ORDER
    from ..util import squeeze

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
        if not total:
            body = "Значимых изменений нет"
        else:
            parts = []
            if critical:
                parts.append(f"{critical} требует решения")
            if counts["high"]:
                parts.append(f"{counts['high']} важных")
            parts.append(f"всего {total}")
            body = ", ".join(parts)
        url = base_url or "./"
        level = "critical" if critical else "normal"

    return {"title": squeeze(title, MAX_TITLE), "body": squeeze(body, MAX_BODY),
            "url": url, "level": level, "tag": "alert" if alert_mode else "daily"}


# ---------- выгрузка отчётов в веб-приложение ----------

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

    files = sorted(reports_dir.glob("*.html"), key=lambda p: p.stat().st_mtime, reverse=True)
    files = [f for f in files if REPORT_NAME.match(f.name)][:limit]

    # Убираем отчёты, выпавшие из окна, чтобы папка не росла бесконечно.
    # Сверяемся с шаблоном имени: рядом лежат index.html и прочие файлы
    # приложения, и снести их было бы катастрофой.
    keep = {f.name for f in files}
    for old in webapp.glob("*.html"):
        if REPORT_NAME.match(old.name) and old.name not in keep:
            old.unlink()

    # Прежняя раскладка с подпапкой — убираем, чтобы не осталось двух копий.
    legacy = webapp / "reports"
    if legacy.is_dir():
        shutil.rmtree(legacy, ignore_errors=True)

    index = []
    for f in files:
        shutil.copy2(f, webapp / f.name)
        md = f.with_suffix(".md")
        summary = ""
        if md.exists():
            for line in md.read_text(encoding="utf-8").splitlines():
                if line.startswith("| ") and "---" not in line:
                    continue
                if line.startswith("По органам:"):
                    summary = line.replace("По органам:", "").strip()
                    break
        kind = "Срочное уведомление" if "_alert" in f.name else "Ежедневный отчёт"
        stamp = f.stem.split("_")[0]
        index.append({"file": f.name,
                      "title": f"{kind} — {stamp}",
                      "summary": summary})

    (webapp / "latest.json").write_text(
        json.dumps({"reports": index}, ensure_ascii=False, indent=1), encoding="utf-8")
    return len(index)
