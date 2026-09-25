"""Доставка отчётов в Telegram — канал или личный чат.

Telegram понимает лишь узкое подмножество HTML (<b>, <i>, <a>, <code>...),
поэтому письмо сюда не годится: нужен отдельный компактный рендер.
Ограничение на длину сообщения — 4096 символов, длинное режем по абзацам.

Настройки (в ~/.regwatch.env):
  TELEGRAM_BOT_TOKEN  — токен от @BotFather
  TELEGRAM_CHAT_ID    — id канала (вида -100...) или личного чата

Бот не может написать первым: в канал его добавляют администратором,
в личный чат — пользователь сам нажимает «Старт».
"""
from __future__ import annotations

import html as _html
import json
import mimetypes
import os
import urllib.request
import uuid
from pathlib import Path

API = "https://api.telegram.org/bot{token}/{method}"
LIMIT = 4096
URGENCY_MARK = {"critical": "🔴", "high": "🟠", "normal": "⚪️", "low": "·"}


class TelegramNotConfigured(Exception):
    pass


def token() -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()


def chat_id() -> str:
    return os.environ.get("TELEGRAM_CHAT_ID", "").strip()


def configured() -> bool:
    return bool(token() and chat_id())


def _call(method: str, payload: dict, timeout: int = 30) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        API.format(token=token(), method=method), data=data,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def get_me() -> dict:
    return _call_get("getMe")


def _call_get(method: str, params: str = "", timeout: int = 30) -> dict:
    url = API.format(token=token(), method=method) + (f"?{params}" if params else "")
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def discover_chats() -> list:
    """Находит чаты и каналы, где бот уже побывал. Для настройки TELEGRAM_CHAT_ID."""
    try:
        d = _call_get("getUpdates", "limit=100")
    except Exception as e:
        return [{"error": str(e)}]
    if not d.get("ok"):
        return [{"error": d.get("description", "неизвестная ошибка")}]
    seen, out = set(), []
    for upd in d.get("result", []):
        for key in ("message", "channel_post", "my_chat_member",
                    "edited_channel_post", "chat_member"):
            obj = upd.get(key)
            if not isinstance(obj, dict):
                continue
            chat = obj.get("chat")
            if isinstance(chat, dict) and chat.get("id") not in seen:
                seen.add(chat["id"])
                out.append({"id": chat["id"],
                            "type": chat.get("type"),
                            "title": chat.get("title") or chat.get("username")
                            or chat.get("first_name") or "—"})
    return out


def _esc(s) -> str:
    return _html.escape(str(s or ""), quote=False)


def _split(text: str, limit: int = LIMIT) -> list:
    """Режет по абзацам, не разрывая разметку внутри строки."""
    if len(text) <= limit:
        return [text]
    parts, buf = [], ""
    for block in text.split("\n\n"):
        candidate = (buf + "\n\n" + block) if buf else block
        if len(candidate) <= limit:
            buf = candidate
        else:
            if buf:
                parts.append(buf)
            # одиночный блок длиннее лимита — режем по строкам
            while len(block) > limit:
                cut = block.rfind("\n", 0, limit)
                cut = cut if cut > 0 else limit
                parts.append(block[:cut])
                block = block[cut:].lstrip("\n")
            buf = block
    if buf:
        parts.append(buf)
    return parts


def send_message(text: str, disable_preview: bool = True) -> dict:
    if not configured():
        raise TelegramNotConfigured(
            "не заданы TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в ~/.regwatch.env")
    last = {}
    for chunk in _split(text):
        last = _call("sendMessage", {
            "chat_id": chat_id(), "text": chunk, "parse_mode": "HTML",
            "disable_web_page_preview": disable_preview,
        })
        if not last.get("ok"):
            raise RuntimeError(f"Telegram отклонил сообщение: {last.get('description')}")
    return last


def send_document(path: Path, caption: str = "") -> dict:
    """Отправка файла. multipart собираем вручную — без внешних библиотек."""
    if not configured():
        raise TelegramNotConfigured("не заданы TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID")
    path = Path(path)
    boundary = uuid.uuid4().hex
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"

    def field(name, value):
        return (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n'
                f"\r\n{value}\r\n").encode("utf-8")

    body = field("chat_id", chat_id())
    if caption:
        body += field("caption", caption[:1024]) + field("parse_mode", "HTML")
    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="document"; '
             f'filename="{path.name}"\r\nContent-Type: {ctype}\r\n\r\n').encode("utf-8")
    body += path.read_bytes() + f"\r\n--{boundary}--\r\n".encode("utf-8")

    req = urllib.request.Request(
        API.format(token=token(), method="sendDocument"), data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST")
    with urllib.request.urlopen(req, timeout=90) as r:
        d = json.loads(r.read().decode("utf-8"))
    if not d.get("ok"):
        raise RuntimeError(f"Telegram отклонил файл: {d.get('description')}")
    return d


def render(buckets: dict, period_label: str, deadlines=None) -> str:
    """Компактный дайджест под Telegram: заголовок, блоки по срочности, сроки."""
    from ..report import ORDER, URGENCY_META, _row_meta
    from ..util import parse_dt, squeeze

    lines = [f"<b>Мониторинг регулирования частных инвестиций</b>",
             f"<i>{_esc(period_label)}</i>"]
    total = sum(len(v) for v in buckets.values())
    if not total:
        lines.append("\nЗа период значимых изменений нет.")
        return "\n".join(lines)

    for u in ORDER:
        entries = buckets.get(u) or []
        if not entries:
            continue
        lines.append(f"\n{URGENCY_MARK.get(u, '·')} <b>{URGENCY_META[u][0]}</b> · {len(entries)}")
        for e in entries[:12]:
            r, meta = e["row"], _row_meta(e["row"])
            title = _esc(squeeze(r["title"], 150))
            link = f'<a href="{_esc(r["url"])}">{title}</a>' if r["url"] else title
            bits = [_esc(r["authority"])]
            pub = parse_dt(r["published_at"])
            if pub:
                bits.append(f"{pub:%d.%m.%Y}")
            if r["stage"]:
                bits.append(_esc(squeeze(r["stage"], 60)))
            lines.append(f"\n• {link}\n  <i>{' · '.join(bits)}</i>")
            if meta.get("comments_until"):
                lines.append(f"  ⏳ замечания до {_esc(meta['comments_until'])}")
        if len(entries) > 12:
            lines.append(f"\n  <i>…и ещё {len(entries) - 12} — в приложенном отчёте</i>")

    if deadlines:
        lines.append("\n⏳ <b>Ближайшие сроки замечаний</b>")
        for dt, r, meta in deadlines[:6]:
            lines.append(f"• {dt:%d.%m} — {_esc(squeeze(r['title'], 110))}")
    return "\n".join(lines)


def deliver(buckets: dict, period_label: str, report_path: Path,
            deadlines=None, attach: bool = True) -> str:
    text = render(buckets, period_label, deadlines)
    send_message(text)
    note = "сообщение отправлено"
    if attach and sum(len(v) for v in buckets.values()):
        # PDF предпочтительнее: открывается одинаково везде и пересылается
        # дальше без вопросов. HTML — запасной вариант, если PDF не собрался.
        for suffix, label in ((".pdf", "Полный отчёт"), (".html", "Полный отчёт")):
            f = Path(report_path).with_suffix(suffix)
            if not f.exists():
                continue
            try:
                send_document(f, caption=label)
                note += f", отчёт приложен ({suffix.lstrip('.').upper()})"
            except Exception as e:
                note += f", файл не ушёл ({type(e).__name__})"
            break
    return note
