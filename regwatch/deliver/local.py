"""Доставка без паролей: уведомление macOS плюс отчёт в папке.

Почта требует пароля приложения, Telegram — токена бота. Этот канал не требует
ничего: отчёт кладётся в reports/, а на экране всплывает уведомление со сводкой.
Клик по уведомлению открывает отчёт в браузере.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def available() -> bool:
    """Уведомления есть только на macOS."""
    return shutil.which("osascript") is not None


def _escape(s: str) -> str:
    """AppleScript-строка: экранируем кавычки и обратные слэши."""
    return (s or "").replace("\\", "\\\\").replace('"', '\\"')


def notify(title: str, message: str, subtitle: str | None = None,
           sound: bool = False) -> bool:
    if not available():
        return False
    # В AppleScript "with" ставится только перед первым модификатором,
    # дальше идут голые имена: with title "..." subtitle "..." sound name "...".
    parts = [f'display notification "{_escape(message)}"',
             f'with title "{_escape(title)}"']
    if subtitle:
        parts.append(f'subtitle "{_escape(subtitle)}"')
    if sound:
        parts.append('sound name "Glass"')
    try:
        r = subprocess.run(["osascript", "-e", " ".join(parts)],
                           capture_output=True, timeout=15, text=True)
        # osascript возвращает 0 даже при синтаксической ошибке — судим по stderr
        return not (r.stderr or "").strip()
    except Exception:
        return False


def deliver(report_path: Path, subject: str, critical: int, high: int,
            total: int, open_report: bool = False) -> str:
    """Кладёт отчёт и показывает уведомление. Возвращает строку для журнала."""
    html = Path(report_path).with_suffix(".html")

    if total == 0:
        body = "Значимых изменений нет"
    else:
        bits = []
        if critical:
            bits.append(f"{critical} срочных")
        if high:
            bits.append(f"{high} важных")
        bits.append(f"всего {total}")
        body = ", ".join(bits)

    shown = notify("Регмонитор", body, subtitle=subject, sound=bool(critical))

    # Отчёт с срочными пунктами открываем сразу — иначе уведомление легко пропустить
    if open_report and critical and html.exists():
        try:
            subprocess.run(["open", str(html)], check=False, timeout=15)
        except Exception:
            pass

    where = f"отчёт: {html if html.exists() else report_path}"
    return f"уведомление показано, {where}" if shown else f"уведомления недоступны, {where}"
