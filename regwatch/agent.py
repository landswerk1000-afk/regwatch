"""Оркестратор: сбор → оценка → отчёт → доставка."""
from __future__ import annotations

import logging
from datetime import timedelta

from . import publish, report as R
from .config import Config
from .deliver import EmailNotConfigured, local, pdf, send_email, telegram, webpush
from .relevance import Relevance
from .sources import ALL_SOURCES, enabled
from .store import Store
from .util import now_msk, squeeze

log = logging.getLogger("regwatch")


class Agent:
    def __init__(self, config: Config):
        self.cfg = config
        self.store = Store(config.db_path)
        self.rel = Relevance(config.topics_path)
        self.http = config.make_http(logger=log)

    def close(self):
        self.store.db.commit()
        self.store.close()

    # ---------- сбор ----------
    def collect(self) -> dict:
        run_id = self.store.start_run()
        stats = {"sources_ok": 0, "sources_failed": 0, "items": 0,
                 "new": 0, "changed": 0, "skipped_proxy": [],
                 # Имена, а не только счётчик: «5 сбоев» не говорит, каких
                 # именно, а на сервере журнал агента не печатается вовсе.
                 "failed": []}

        for src in enabled(self.cfg):
            # Источнику нужен российский адрес. Путей к нему два — ретранслятор
            # в Yandex Cloud и публичный прокси, — и спрашивать надо про оба.
            # Раньше здесь проверялся только прокси, и когда его сменил
            # ретранслятор, СОЗД молча выпал из сбора при рабочем канале.
            if src.requires_proxy:
                probe = "https://" + (self.cfg.proxy_hosts[0] if self.cfg.proxy_hosts else "x")
                if self.http.route(probe) == "напрямую":
                    stats["skipped_proxy"].append(src.id)
                    self.store.health(src.id, False,
                                      "источник включён, но путь к российскому IP не настроен")
                    log.warning("%s: нет ни ретранслятора, ни прокси — пропуск", src.id)
                    continue

            res = src.run(self.http)
            if not res.ok:
                stats["sources_failed"] += 1
                stats["failed"].append({"id": src.id, "error": res.error or "без описания"})
                self.store.health(src.id, False, res.error)
                log.warning("%s: ОШИБКА %s", src.id, res.error)
                continue

            stats["sources_ok"] += 1
            self.store.health(src.id, True)
            for item in res.items:
                events = self.store.upsert_item(item)
                stats["items"] += 1
                for e in events:
                    stats["new" if e["event_type"] == "new" else "changed"] += 1
                if events:
                    v = self.rel.score(item)
                    self.store.set_score(item["id"], v.relevance, v.urgency,
                                         v.topics, v.matched, v.rationale)
            self.store.db.commit()
            log.info("%s: %d документов", src.id, len(res.items))

        self.store.finish_run(run_id, "ok", stats)
        self.store.db.commit()
        return stats

    def rescore_all(self) -> int:
        """Пересчитать релевантность после правки topics.json."""
        rows = self.store.db.execute("SELECT * FROM items").fetchall()
        import json as _json
        for r in rows:
            item = dict(r)
            item["meta"] = _json.loads(r["meta"] or "{}")
            v = self.rel.score(item)
            self.store.set_score(r["id"], v.relevance, v.urgency, v.topics,
                                 v.matched, v.rationale)
        self.store.db.commit()
        return len(rows)

    # ---------- состояние источников ----------
    def notify_health(self, dry_run: bool = False) -> str | None:
        """Сообщает, когда источник замолчал надолго или снова заговорил.

        Это предупреждение для того, кто обслуживает агента, а не для
        руководителя: в отчёт состояние источников намеренно не попадает.
        Канал — Telegram: сообщение уходит без сторонних библиотек и не
        зависит от того, работает ли push.

        Нужно потому, что молчащий источник не отличим от спокойного дня.
        26 сентября пять источников упали в одном прогоне, и узналось это
        только при разборе журналов вручную.
        """
        threshold = int(self.cfg.thresholds.get("source_fail_alert", 2))
        ch = self.store.health_changes(threshold)
        if not ch["broke"] and not ch["fixed"]:
            return None

        # Текст ошибки приходит из исключения и может содержать < > &.
        # Без экранирования Telegram отвергнет всё сообщение целиком,
        # и предупреждение не дойдёт именно тогда, когда оно нужнее всего.
        from html import escape as _e

        lines = []
        for h in ch["broke"]:
            lines.append(f"<b>{_e(squeeze(h['source'], 40))}</b> — молчит "
                         f"{h['fail_streak']} прогона подряд\n"
                         f"<i>{_e(squeeze(h.get('error_text') or 'без описания', 120))}</i>")
        for h in ch["fixed"]:
            lines.append(f"<b>{_e(squeeze(h['source'], 40))}</b> — снова отвечает")

        head = "Регмонитор: состояние источников"
        text = f"<b>{head}</b>\n\n" + "\n\n".join(lines)
        if ch["broke"]:
            text += ("\n\nОстальные источники продолжают работать. "
                     "Подробности: <code>python3 -m regwatch doctor</code>")

        if dry_run:
            return f"состояние источников: dry-run, {len(lines)} изменений"

        try:
            telegram.send_message(text)
            note = (f"сломались: {len(ch['broke'])}, "
                    f"починились: {len(ch['fixed'])}")
        except Exception as e:
            # Не сумели предупредить — это само по себе плохо, но прогон
            # ронять нельзя: отчёт важнее служебного сообщения.
            log.warning("не удалось сообщить о состоянии источников: %s", e)
            return f"состояние источников: не отправлено ({type(e).__name__})"

        # Отмечаем ПОСЛЕ отправки: если сообщение не ушло, попробуем снова
        # в следующий прогон, а не замолчим навсегда.
        for h in ch["broke"]:
            self.store.mark_health_alerted(h["source"], h["fail_streak"])
        for h in ch["fixed"]:
            self.store.mark_health_alerted(h["source"], 0)
        self.store.db.commit()
        return note

    # ---------- отчёт ----------
    def build(self, alert_mode: bool = False):
        th = self.cfg.thresholds
        floor = th["alert_min_relevance"] if alert_mode else th["report_min_relevance"]
        rows = self.store.pending(floor, alert_mode=alert_mode)

        if alert_mode:
            allowed = set(th.get("alert_urgencies", ["critical"]))
            rows = [r for r in rows if (r["urgency"] or "low") in allowed]

        buckets = R.group(rows, th)
        # Выбираем адресно: срок замечаний есть у считанных документов, а
        # «последние N по last_seen» до них не дотягивались — после каждого
        # сбора last_seen одинаков у сотен записей, и порядок среди них случаен.
        dls = R.deadlines(self.store.db.execute(
            "SELECT * FROM items WHERE meta LIKE '%comments_until%'").fetchall())
        health = self.store.health_all()
        label = ("Срочное уведомление" if alert_mode
                 else f"Отчёт за {now_msk():%d.%m.%Y}")
        md = R.render_markdown(buckets, rows, health, label, dls)
        html = R.render_html(buckets, rows, health, label, dls)
        event_ids = [r["event_id"] for r in rows]
        return buckets, rows, md, html, event_ids, label, dls, health

    def subject(self, buckets: dict, alert_mode: bool) -> str:
        crit = len(buckets.get("critical") or [])
        high = len(buckets.get("high") or [])
        total = sum(len(v) for v in buckets.values())
        if alert_mode:
            top = (buckets.get("critical") or [{}])[0]
            title = squeeze(top.get("row", {})["title"], 70) if top else "срочное событие"
            return f"СРОЧНО: {title}"
        if total == 0:
            return f"{now_msk():%d.%m} — без значимых изменений"
        parts = []
        if crit:
            parts.append(f"{crit} срочных")
        if high:
            parts.append(f"{high} важных")
        tail = ", ".join(parts) if parts else f"{total} документов"
        return f"{now_msk():%d.%m} — {tail}"

    # ---------- прогон ----------
    def run(self, alert_mode: bool = False, dry_run: bool = False,
            skip_collect: bool = False, mark: bool = True) -> dict:
        stats = {} if skip_collect else self.collect()

        # До всего остального: срочный прогон без событий выходит из run()
        # досрочно, и предупреждение о молчащем источнике не ушло бы никогда.
        if not skip_collect and telegram.configured():
            note = self.notify_health(dry_run=dry_run)
            if note:
                log.info("%s", note)

        buckets, rows, md, html, event_ids, label, dls, health_rows = self.build(alert_mode)
        total = sum(len(v) for v in buckets.values())

        out = {"stats": stats, "items": total, "label": label,
               "critical": len(buckets.get("critical") or []),
               "high": len(buckets.get("high") or []), "delivery": None, "path": None}

        if alert_mode and total == 0:
            out["delivery"] = "нет срочных событий — письмо не отправлено"
            return out

        self.cfg.reports_dir.mkdir(parents=True, exist_ok=True)
        kind = "alert" if alert_mode else "daily"
        path = self.cfg.reports_dir / f"{now_msk():%Y-%m-%d_%H%M}_{kind}.md"
        path.write_text(md, encoding="utf-8")
        (path.with_suffix(".html")).write_text(html, encoding="utf-8")
        out["path"] = str(path)

        # PDF — то, что уходит руководителю. Сбой сборки не должен
        # останавливать рассылку: остальные каналы работают без него.
        if pdf.configured(self.cfg.root):
            try:
                res = pdf.build(self.cfg.root, R.to_model(buckets, rows, health_rows,
                                                          label, dls),
                                path.with_suffix(".pdf"))
                out["pdf"] = res.get("path")
                log.info("PDF собран (%s, %d байт)", res.get("font"), res.get("bytes", 0))
            except Exception as e:
                log.warning("PDF не собрался: %s: %s", type(e).__name__, e)

        subj = self.subject(buckets, alert_mode)
        push_reached_nobody = False

        channels = []
        # Отчёты кладём в веб-приложение до рассылки: уведомление ведёт на них,
        # и к моменту клика страница уже должна быть актуальной.
        try:
            webpush.export_reports(self.cfg.root, self.cfg.reports_dir)
        except Exception as e:
            log.warning("выгрузка в webapp не удалась: %s", e)

        # Публикация на GitHub Pages. Без неё приложение на телефоне
        # показывает вчерашний отчёт: агент кладёт файлы локально, а сайт
        # отдаёт то, что в репозитории. Сбой публикации прогон не валит —
        # уведомление уже ушло, и текст в нём самодостаточен.
        if not dry_run and publish.configured():
            try:
                res = publish.publish(self.cfg.root)
                channels.append(f"сайт: {res['detail']}")
                self.store.log_delivery(kind, "site",
                                        "ok" if res["pushed"] else "skipped",
                                        res["detail"], total, str(path))
            except Exception as e:
                msg = f"сайт: не опубликовано ({type(e).__name__}: {e})"
                channels.append(msg)
                self.store.log_delivery(kind, "site", "error", str(e)[:300], total, str(path))
                log.warning(msg)

        # Push — основной канал.
        if webpush.configured(self.cfg.root):
            if dry_run:
                channels.append("push: dry-run, не отправлено")
            else:
                try:
                    res = webpush.send(self.cfg.root, self.store, webpush.payload_for(
                        buckets, label, alert_mode, self.cfg.data.get("webapp_url", "")))
                    channels.append(f"push: {res['detail']}")
                    self.store.log_delivery(kind, "push", "ok" if res["sent"] else "no_recipients",
                                            res["detail"], total, str(path))
                    push_reached_nobody = res["sent"] == 0
                except Exception as e:
                    channels.append(f"push: ошибка {type(e).__name__}: {e}")
                    self.store.log_delivery(kind, "push", "error", str(e), total, str(path))
                    push_reached_nobody = True

        # Telegram — пока не отключаем: по плану он снимается только после того,
        # как push отработает полный суточный цикл.
        if telegram.configured():
            if dry_run:
                channels.append("telegram: dry-run, не отправлено")
            else:
                try:
                    note = telegram.deliver(buckets, label, path, dls,
                                            attach=not alert_mode)
                    channels.append(f"telegram: {note}")
                    self.store.log_delivery(kind, "telegram", "ok", note, total, str(path))
                except Exception as e:
                    msg = f"telegram: ошибка {type(e).__name__}: {e}"
                    channels.append(msg)
                    self.store.log_delivery(kind, "telegram", "error", str(e), total, str(path))

        # Резерв. Почта поднимается, если push никого не достал, а событие
        # срочное: остаться незамеченным такое событие не должно.
        escalate = push_reached_nobody and out["critical"] > 0
        if escalate and not self.cfg.email.get("enabled"):
            channels.append("ВНИМАНИЕ: push никого не достал, почта выключена — "
                            "срочное ушло только уведомлением на экран")
            self.store.log_delivery(kind, "email", "unavailable",
                                    "резерв не настроен при сбое push", total, str(path))

        if self.cfg.email.get("enabled") or escalate:
            try:
                msg = send_email(self.cfg.email, self.cfg.smtp_password, subj,
                                 html, md, dry_run=dry_run)
                channels.append(f"почта: {msg}")
                self.store.log_delivery(kind, "email", "ok", msg, total, str(path))
            except EmailNotConfigured as e:
                channels.append(f"почта не настроена: {e}")
                self.store.log_delivery(kind, "email", "not_configured", str(e), total, str(path))
            except Exception as e:
                channels.append(f"почта: ошибка {type(e).__name__}: {e}")
                self.store.log_delivery(kind, "email", "error", str(e), total, str(path))

        # Уведомление macOS показываем всегда: паролей не требует и служит
        # страховкой, если внешние каналы отвалились.
        msg = local.deliver(path, subj, out["critical"], out["high"], total,
                            open_report=alert_mode and not telegram.configured())
        channels.append(msg)
        self.store.log_delivery(kind, "notification", "ok", msg, total, str(path))

        out["delivery"] = " | ".join(channels)

        if mark and not dry_run:
            self.store.mark(event_ids, alert_mode=alert_mode)
            if not alert_mode:
                # Ежедневный отчёт рассмотрел всю очередь: то, что не прошло порог,
                # отклонено осознанно. Иначе подпороговые события копятся вечно.
                # alerted тоже: то, что уже вошло в дневной отчёт, не должно
                # через час прилететь повторно срочным письмом.
                cur = self.store.db.execute(
                    "UPDATE events SET reported=1, alerted=1 WHERE reported=0 OR alerted=0")
                out["filtered_out"] = max(cur.rowcount - len(event_ids), 0)
        self.store.db.commit()
        return out
