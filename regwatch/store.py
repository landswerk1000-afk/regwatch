"""Состояние агента: SQLite. Дедупликация, история изменений, журнал доставки."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .util import now_utc, iso, sha

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  id            TEXT PRIMARY KEY,
  source        TEXT NOT NULL,
  authority     TEXT NOT NULL,
  kind          TEXT NOT NULL,
  external_id   TEXT,
  url           TEXT,
  title         TEXT NOT NULL,
  summary       TEXT,
  body          TEXT,
  stage         TEXT,
  published_at  TEXT,
  first_seen    TEXT NOT NULL,
  last_seen     TEXT NOT NULL,
  content_hash  TEXT NOT NULL,
  meta          TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_items_pub    ON items(published_at DESC);
CREATE INDEX IF NOT EXISTS ix_items_source ON items(source);
CREATE INDEX IF NOT EXISTS ix_items_seen   ON items(first_seen DESC);

CREATE TABLE IF NOT EXISTS events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id     TEXT NOT NULL REFERENCES items(id),
  event_type  TEXT NOT NULL,           -- new | changed | stage_change
  detail      TEXT,
  old_value   TEXT,
  new_value   TEXT,
  detected_at TEXT NOT NULL,
  reported    INTEGER NOT NULL DEFAULT 0,
  alerted     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_events_unreported ON events(reported, detected_at);
CREATE INDEX IF NOT EXISTS ix_events_item       ON events(item_id);

CREATE TABLE IF NOT EXISTS scores (
  item_id    TEXT PRIMARY KEY REFERENCES items(id),
  relevance  REAL NOT NULL,
  urgency    TEXT NOT NULL,            -- critical | high | normal | low
  topics     TEXT NOT NULL DEFAULT '[]',
  matched    TEXT NOT NULL DEFAULT '[]',
  rationale  TEXT,
  scored_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  status      TEXT,
  stats       TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS source_health (
  source      TEXT PRIMARY KEY,
  last_ok     TEXT,
  last_error  TEXT,
  error_text  TEXT,
  ok_streak   INTEGER DEFAULT 0,
  fail_streak INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS push_subscriptions (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  endpoint    TEXT UNIQUE NOT NULL,
  p256dh      TEXT NOT NULL,
  auth        TEXT NOT NULL,
  device      TEXT,
  created_at  TEXT NOT NULL,
  last_ok     TEXT,
  last_error  TEXT,
  fail_streak INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS deliveries (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  kind      TEXT NOT NULL,             -- daily | alert
  channel   TEXT NOT NULL,
  sent_at   TEXT NOT NULL,
  status    TEXT NOT NULL,
  detail    TEXT,
  item_count INTEGER DEFAULT 0,
  path      TEXT
);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self):
        self.db.close()

    @contextmanager
    def tx(self):
        try:
            yield self.db
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    # ---------- items ----------
    def upsert_item(self, item: dict) -> list[dict]:
        """Вставляет или обновляет запись. Возвращает список порождённых событий."""
        now = iso(now_utc())
        chash = sha(item.get("title"), item.get("summary"), item.get("stage"), item.get("url"))
        row = self.db.execute("SELECT * FROM items WHERE id=?", (item["id"],)).fetchone()
        events: list[dict] = []

        if row is None:
            self.db.execute(
                """INSERT INTO items (id, source, authority, kind, external_id, url, title,
                   summary, body, stage, published_at, first_seen, last_seen, content_hash, meta)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (item["id"], item["source"], item.get("authority", ""), item.get("kind", "doc"),
                 item.get("external_id"), item.get("url"), item["title"], item.get("summary"),
                 item.get("body"), item.get("stage"), item.get("published_at"),
                 now, now, chash, json.dumps(item.get("meta", {}), ensure_ascii=False)))
            events.append(self._add_event(item["id"], "new", "Новый документ"))
        else:
            if row["content_hash"] != chash:
                if (row["stage"] or "") != (item.get("stage") or "") and item.get("stage"):
                    events.append(self._add_event(
                        item["id"], "stage_change", "Изменилась стадия",
                        row["stage"], item.get("stage")))
                else:
                    events.append(self._add_event(
                        item["id"], "changed", "Документ обновлён",
                        (row["title"] or "")[:200], (item.get("title") or "")[:200]))
                self.db.execute(
                    """UPDATE items SET title=?, summary=?, body=?, stage=?, url=?,
                       published_at=COALESCE(?, published_at), last_seen=?, content_hash=?, meta=?
                       WHERE id=?""",
                    (item["title"], item.get("summary"), item.get("body"), item.get("stage"),
                     item.get("url"), item.get("published_at"), now, chash,
                     json.dumps(item.get("meta", {}), ensure_ascii=False), item["id"]))
            else:
                # дата публикации может приехать позже самого документа — дозаполняем молча
                if item.get("published_at") and not row["published_at"]:
                    self.db.execute(
                        "UPDATE items SET last_seen=?, published_at=? WHERE id=?",
                        (now, item["published_at"], item["id"]))
                else:
                    self.db.execute("UPDATE items SET last_seen=? WHERE id=?",
                                    (now, item["id"]))
        return events

    def _add_event(self, item_id: str, etype: str, detail: str,
                   old=None, new=None) -> dict:
        cur = self.db.execute(
            """INSERT INTO events (item_id, event_type, detail, old_value, new_value, detected_at)
               VALUES (?,?,?,?,?,?)""",
            (item_id, etype, detail, old, new, iso(now_utc())))
        return {"id": cur.lastrowid, "item_id": item_id, "event_type": etype, "detail": detail}

    def set_score(self, item_id: str, relevance: float, urgency: str,
                  topics, matched, rationale: str) -> None:
        self.db.execute(
            """INSERT INTO scores (item_id, relevance, urgency, topics, matched, rationale, scored_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(item_id) DO UPDATE SET
                 relevance=excluded.relevance, urgency=excluded.urgency,
                 topics=excluded.topics, matched=excluded.matched,
                 rationale=excluded.rationale, scored_at=excluded.scored_at""",
            (item_id, relevance, urgency, json.dumps(topics, ensure_ascii=False),
             json.dumps(matched, ensure_ascii=False), rationale, iso(now_utc())))

    def pending(self, min_relevance: float = 0.0, alert_mode: bool = False) -> list[sqlite3.Row]:
        """Неотправленные события с оценкой релевантности."""
        flag = "alerted" if alert_mode else "reported"
        return self.db.execute(
            f"""SELECT e.id AS event_id, e.event_type, e.detail, e.old_value, e.new_value,
                       e.detected_at, i.*, s.relevance, s.urgency, s.topics, s.matched, s.rationale
                FROM events e
                JOIN items  i ON i.id = e.item_id
                LEFT JOIN scores s ON s.item_id = e.item_id
                WHERE e.{flag} = 0 AND COALESCE(s.relevance, 0) >= ?
                ORDER BY COALESCE(s.relevance,0) DESC, i.published_at DESC""",
            (min_relevance,)).fetchall()

    def mark(self, event_ids, alert_mode: bool = False) -> None:
        if not event_ids:
            return
        col = "alerted" if alert_mode else "reported"
        self.db.executemany(f"UPDATE events SET {col}=1 WHERE id=?",
                            [(i,) for i in event_ids])

    # ---------- health / runs ----------
    def health(self, source: str, ok: bool, error: str | None = None) -> None:
        now = iso(now_utc())
        cur = self.db.execute("SELECT * FROM source_health WHERE source=?", (source,)).fetchone()
        if cur is None:
            self.db.execute(
                """INSERT INTO source_health (source,last_ok,last_error,error_text,ok_streak,fail_streak)
                   VALUES (?,?,?,?,?,?)""",
                (source, now if ok else None, None if ok else now, error,
                 1 if ok else 0, 0 if ok else 1))
        else:
            self.db.execute(
                """UPDATE source_health SET last_ok=?, last_error=?, error_text=?,
                   ok_streak=?, fail_streak=? WHERE source=?""",
                (now if ok else cur["last_ok"], cur["last_error"] if ok else now,
                 None if ok else error,
                 (cur["ok_streak"] + 1) if ok else 0,
                 0 if ok else (cur["fail_streak"] + 1), source))

    def health_all(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM source_health ORDER BY source").fetchall()

    def start_run(self) -> int:
        return self.db.execute("INSERT INTO runs (started_at) VALUES (?)",
                               (iso(now_utc()),)).lastrowid

    def finish_run(self, run_id: int, status: str, stats: dict) -> None:
        self.db.execute("UPDATE runs SET finished_at=?, status=?, stats=? WHERE id=?",
                        (iso(now_utc()), status, json.dumps(stats, ensure_ascii=False), run_id))

    def log_delivery(self, kind, channel, status, detail=None, count=0, path=None) -> None:
        self.db.execute(
            """INSERT INTO deliveries (kind, channel, sent_at, status, detail, item_count, path)
               VALUES (?,?,?,?,?,?,?)""",
            (kind, channel, iso(now_utc()), status, detail, count, path))

    # ---------- подписки на push ----------
    def add_subscription(self, endpoint: str, p256dh: str, auth: str,
                         device: str | None = None) -> str:
        """Одно устройство — одна подписка. Повторная регистрация обновляет ключи."""
        exists = self.db.execute(
            "SELECT id FROM push_subscriptions WHERE endpoint=?", (endpoint,)).fetchone()
        self.db.execute(
            """INSERT INTO push_subscriptions (endpoint, p256dh, auth, device, created_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(endpoint) DO UPDATE SET
                 p256dh=excluded.p256dh, auth=excluded.auth,
                 device=COALESCE(excluded.device, device),
                 fail_streak=0, last_error=NULL""",
            (endpoint, p256dh, auth, device, iso(now_utc())))
        self.db.commit()
        return "обновлена" if exists else "добавлена"

    def subscriptions(self) -> list:
        return self.db.execute(
            "SELECT * FROM push_subscriptions ORDER BY created_at").fetchall()

    def drop_subscription(self, endpoint: str) -> None:
        """Push-сервис ответил 404/410 — устройство отписалось или исчезло."""
        self.db.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (endpoint,))
        self.db.commit()

    def subscription_result(self, endpoint: str, ok: bool, error=None) -> None:
        now = iso(now_utc())
        if ok:
            self.db.execute(
                "UPDATE push_subscriptions SET last_ok=?, fail_streak=0, last_error=NULL"
                " WHERE endpoint=?", (now, endpoint))
        else:
            self.db.execute(
                "UPDATE push_subscriptions SET last_error=?, fail_streak=fail_streak+1"
                " WHERE endpoint=?", (str(error)[:300], endpoint))
        self.db.commit()

    def stats(self) -> dict:
        q = lambda s: self.db.execute(s).fetchone()[0]
        return {
            "items": q("SELECT COUNT(*) FROM items"),
            "events": q("SELECT COUNT(*) FROM events"),
            "unreported": q("SELECT COUNT(*) FROM events WHERE reported=0"),
            "runs": q("SELECT COUNT(*) FROM runs"),
        }
