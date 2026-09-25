"""Отправка отчёта по SMTP. Пароль — только из переменной окружения."""
from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid


class EmailNotConfigured(Exception):
    pass


def _validate(cfg: dict, password: str) -> None:
    missing = [k for k in ("smtp_host", "from_addr") if not cfg.get(k)]
    if not cfg.get("to"):
        missing.append("to")
    if not password:
        missing.append(f"пароль в ${cfg.get('password_env', 'REGWATCH_SMTP_PASSWORD')}")
    if missing:
        raise EmailNotConfigured("не заполнено: " + ", ".join(missing))


def send_email(cfg: dict, password: str, subject: str, html_body: str,
               text_body: str, dry_run: bool = False) -> str:
    _validate(cfg, password)

    msg = EmailMessage()
    msg["Subject"] = f"{cfg.get('subject_prefix', '').strip()} {subject}".strip()
    msg["From"] = formataddr(("Регмонитор", cfg["from_addr"]))
    msg["To"] = ", ".join(cfg["to"])
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=cfg["from_addr"].split("@")[-1])
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    if dry_run:
        return f"dry-run: письмо готово ({len(html_body)} байт HTML) → {msg['To']}"

    host, port = cfg["smtp_host"], int(cfg.get("smtp_port", 465))
    ctx = ssl.create_default_context()
    if cfg.get("use_ssl", True):
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=45) as s:
            s.login(cfg.get("username") or cfg["from_addr"], password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=45) as s:
            s.ehlo()
            if cfg.get("use_starttls", True):
                s.starttls(context=ctx)
                s.ehlo()
            s.login(cfg.get("username") or cfg["from_addr"], password)
            s.send_message(msg)
    return f"отправлено → {msg['To']}"
