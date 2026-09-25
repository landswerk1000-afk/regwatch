"""Сборка отчёта: Markdown для архива, HTML для чтения, печати и PDF.

Отчёт читает руководитель, поэтому построен по принципу «сначала то, что
требует решения»: сводка цифрами, затем сроки подачи замечаний (единственное,
что имеет жёсткий дедлайн), и только потом документы по убыванию срочности.
"""
from __future__ import annotations

import html as _html
import json
import re
from collections import Counter, defaultdict
from datetime import timedelta

from . import brand as B
from .util import now_msk, parse_dt, squeeze

C = B.COLORS
U = B.URGENCY

# Совместимость: сюда ходит модуль push-уведомлений. Значения теперь
# производные от фирменной палитры, а не отдельный набор цветов.
URGENCY_META = {k: (v["label"], v["color"], v["soft"], v["soft"])
                for k, v in U.items()}
ORDER = ["critical", "high", "normal", "low"]
EVENT_LABEL = {"new": "новый", "changed": "обновлён", "stage_change": "смена стадии"}

AUTHORITY_COLOR = B.AUTHORITY

MONTHS = ("января февраля марта апреля мая июня июля августа сентября "
          "октября ноября декабря").split()


def ru_date(dt) -> str:
    return f"{dt.day} {MONTHS[dt.month - 1]} {dt.year}" if dt else ""


def _row_meta(row) -> dict:
    try:
        return json.loads(row["meta"] or "{}")
    except Exception:
        return {}


def _topics(row) -> list:
    try:
        return json.loads(row["topics"] or "[]")
    except Exception:
        return []


def _stage_short(stage: str | None) -> tuple:
    """Разделяет «3.3 Рассмотрение ... (первое чтение)» на код и суть."""
    if not stage:
        return None, None
    m = re.match(r"^(\d+(?:\.\d+)?)\s+(.*)$", stage.strip(), re.S)
    if not m:
        return None, squeeze(stage, 150)
    return m.group(1), squeeze(m.group(2), 150)


def group(rows, thresholds: dict) -> dict:
    """Группирует события по срочности, схлопывая повторы одного документа."""
    best: dict = {}
    for r in rows:
        key = r["id"]
        cur = best.get(key)
        if cur is None or (r["relevance"] or 0) > (cur["row"]["relevance"] or 0):
            best[key] = {"row": r, "events": []}
        best[key]["events"].append(r["event_type"])

    buckets = defaultdict(list)
    for entry in best.values():
        buckets[entry["row"]["urgency"] or "low"].append(entry)
    for b in buckets.values():
        b.sort(key=lambda e: (-(e["row"]["relevance"] or 0), e["row"]["published_at"] or ""))

    cap = thresholds.get("max_items_per_report", 60)
    total, trimmed = 0, {}
    for u in ORDER:
        if u not in buckets:
            continue
        room = max(cap - total, 0)
        trimmed[u] = buckets[u][:room] if u in ("normal", "low") else buckets[u]
        total += len(trimmed[u])
    return trimmed


def deadlines(rows, days: int = 14) -> list:
    out, seen = [], set()
    horizon = now_msk() + timedelta(days=days)
    for r in rows:
        meta = _row_meta(r)
        raw = meta.get("comments_until")
        if not raw or r["id"] in seen:
            continue
        dt = parse_dt(raw)
        if dt and now_msk() - timedelta(days=1) <= dt <= horizon:
            seen.add(r["id"])
            out.append((dt, r, meta))
    return sorted(out, key=lambda x: x[0])


def _counts(buckets: dict) -> dict:
    return {u: len(buckets.get(u) or []) for u in ORDER}


def _by_authority(buckets: dict) -> list:
    entries = [e for b in buckets.values() for e in b]
    return Counter(e["row"]["authority"] for e in entries).most_common()


# ---------------- Markdown ----------------

def render_markdown(buckets, rows, health, period_label: str, dls) -> str:
    # health принимается, но не отображается — параметр оставлен,
    # чтобы не менять вызовы во всех каналах доставки.
    c = _counts(buckets)
    total = sum(c.values())
    L = ["# Мониторинг регулирования частных инвестиций",
         f"**{period_label}** · сформировано {now_msk():%d.%m.%Y %H:%M} МСК", ""]

    if not total:
        L += ["За период значимых изменений не зафиксировано.", ""]
    else:
        L += ["| Требует решения | Важное | К сведению | Всего |",
              "|---:|---:|---:|---:|",
              f"| {c['critical']} | {c['high']} | {c['normal'] + c['low']} | {total} |", "",
              "По органам: " + " · ".join(f"{k} — {v}" for k, v in _by_authority(buckets)), ""]

    if dls:
        L += ["## Сроки подачи замечаний", ""]
        for dt, r, meta in dls:
            contact = f" — {meta['contact']}" if meta.get("contact") else ""
            L.append(f"- **до {ru_date(dt)}** · {squeeze(r['title'], 150)}{contact}")
        L.append("")

    for u in ORDER:
        entries = buckets.get(u) or []
        if not entries:
            continue
        L += [f"## {URGENCY_META[u][0]} ({len(entries)})", ""]
        for e in entries:
            r, meta = e["row"], _row_meta(e["row"])
            code, stage = _stage_short(r["stage"])
            pub = parse_dt(r["published_at"])
            L.append(f"### {squeeze(r['title'], 260)}")
            head = [f"**{r['authority']}**"]
            if pub:
                head.append(ru_date(pub))
            head.append(", ".join(sorted({EVENT_LABEL.get(x, x) for x in e["events"]})))
            L.append(" · ".join(head))
            if stage:
                L += ["", f"*Стадия:* {(code + ' ') if code else ''}{stage}"]
            if r["summary"]:
                L += ["", squeeze(r["summary"], 700)]
            tops = _topics(r)
            if tops:
                L += ["", f"*Темы:* {', '.join(tops[:4])}"]
            if meta.get("comments_until"):
                contact = f" — {meta['contact']}" if meta.get("contact") else ""
                L += ["", f"**Замечания до {meta['comments_until']}**{contact}"]
            if r["url"]:
                L += ["", f"[Первоисточник]({r['url']})"]
            L.append("")

    # Состояние источников в отчёт не выводим — см. render_html.
    return "\n".join(L)


# ---------------- HTML ----------------

def _esc(s) -> str:
    return _html.escape(str(s or ""), quote=True)


def _plural(n: int, one: str, few: str, many: str) -> str:
    """Русские числительные: 1 документ, 2 документа, 5 документов."""
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return one
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return few
    return many


def _verdict(c: dict, total: int, dls) -> str:
    """Одна фраза, ради которой руководитель и открывает отчёт."""
    if not total:
        return "Значимых изменений за период не зафиксировано."
    parts = []
    if c["critical"]:
        parts.append(f"{c['critical']} {_plural(c['critical'], 'документ требует', 'документа требуют', 'документов требуют')} решения")
    if c["high"]:
        parts.append(f"{c['high']} {_plural(c['high'], 'важный', 'важных', 'важных')}")
    rest = c["normal"] + c["low"]
    if rest and not parts:
        parts.append(f"{rest} {_plural(rest, 'документ', 'документа', 'документов')} к сведению")
    elif rest:
        parts.append(f"{rest} к сведению")
    text = ", ".join(parts) + "."
    if dls:
        n = len(dls)
        text += (f" По {n} {_plural(n, 'документу', 'документам', 'документам')} "
                 f"истекает срок подачи замечаний.")
    return text[0].upper() + text[1:]


STYLE = f"""
  *{{box-sizing:border-box;}}
  body{{margin:0;padding:0;background:{C['paper']};
    font-family:{B.FONT_STACK};color:{C['ink_65']};
    font-size:15px;line-height:1.55;
    -webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale;}}
  .sheet{{max-width:780px;margin:0 auto;padding:36px 22px 52px;}}
  .num{{{B.FONT_NUMERIC}}}
  a{{color:{C['accent_ink']};text-decoration:none;}}
  a:hover{{text-decoration:underline;}}

  /* ---------- шапка: светлая, как на fgiis.ru ---------- */
  .head{{background:{C['white']};border:1px solid {C['line']};
    border-bottom:none;padding:34px 36px 26px;}}
  .eyebrow{{font-size:10px;font-weight:600;letter-spacing:2.4px;
    text-transform:uppercase;color:{C['accent_ink']};margin-bottom:14px;}}
  .head h1{{margin:0 0 6px;font-size:29px;line-height:1.16;font-weight:700;
    color:{C['ink']};letter-spacing:-0.6px;}}
  .head .org{{font-size:13px;color:{C['ink_65']};margin-bottom:16px;}}
  .head .meta{{font-size:12px;color:{C['ink_45']};padding-top:14px;
    border-top:1px solid {C['line_soft']};{B.FONT_NUMERIC}}}
  .rule{{height:3px;background:{C['accent']};}}

  /* ---------- сводка ---------- */
  .panel{{background:{C['white']};border:1px solid {C['line']};border-top:none;}}
  .verdict{{padding:22px 36px;font-size:16px;line-height:1.5;
    color:{C['ink']};font-weight:500;border-bottom:1px solid {C['line_soft']};}}
  .figures{{display:flex;}}
  .fig{{flex:1;padding:20px 10px 22px;text-align:center;
    border-left:1px solid {C['line_soft']};}}
  .fig:first-child{{border-left:none;}}
  .fig .v{{font-size:32px;font-weight:700;line-height:1;letter-spacing:-1.2px;}}
  .fig .l{{font-size:9.5px;font-weight:600;letter-spacing:1.3px;text-transform:uppercase;
    color:{C['ink_45']};margin-top:9px;}}
  .fig.zero{{opacity:.3;}}
  .orgs{{padding:15px 36px;border-top:1px solid {C['line_soft']};
    background:{C['surface']};font-size:12.5px;color:{C['ink_65']};}}
  .orgs b{{font-weight:600;}}
  .orgs .sep{{color:{C['line']};margin:0 10px;}}

  /* ---------- сроки ---------- */
  .dl{{margin:28px 0 0;background:{C['white']};border:1px solid {C['line']};
    border-top:3px solid {C['accent']};}}
  .dl h2{{margin:0;padding:15px 24px;font-size:10.5px;font-weight:700;
    letter-spacing:1.8px;text-transform:uppercase;color:{C['ink']};
    border-bottom:1px solid {C['line_soft']};}}
  .dl .row{{display:flex;gap:18px;padding:13px 24px;
    border-top:1px solid {C['line_soft']};}}
  .dl .row:first-of-type{{border-top:none;}}
  .dl .when{{flex:0 0 104px;font-size:13px;font-weight:600;
    color:{C['ink']};{B.FONT_NUMERIC}}}
  .dl .when span{{display:block;font-weight:400;font-size:11.5px;
    color:{C['ink_45']};margin-top:2px;}}
  .dl .soon span{{color:{U['critical']['color']};font-weight:600;}}
  .dl .what{{font-size:13.5px;color:{C['ink']};}}
  .dl .contact{{font-size:12px;color:{C['ink_45']};margin-top:3px;}}

  /* ---------- разделы ---------- */
  .sec{{display:flex;align-items:baseline;gap:12px;margin:36px 0 14px;
    padding-bottom:9px;border-bottom:1px solid {C['line']};}}
  .sec .t{{font-size:10.5px;font-weight:700;letter-spacing:1.8px;text-transform:uppercase;}}
  .sec .n{{font-size:11px;font-weight:700;{B.FONT_NUMERIC}}}
  .sec .line{{flex:1;}}

  /* ---------- карточка ---------- */
  .card{{background:{C['white']};border:1px solid {C['line']};border-left:2px solid;
    padding:20px 24px;margin-bottom:10px;}}
  .card .top{{font-size:10.5px;margin-bottom:10px;display:flex;flex-wrap:wrap;
    align-items:center;gap:9px;}}
  .card .org{{font-weight:700;letter-spacing:.9px;text-transform:uppercase;}}
  .card .dot{{color:{C['line']};}}
  .card .when{{color:{C['ink_45']};{B.FONT_NUMERIC}}}
  .card .ev{{color:{C['ink_45']};}}
  .card h3{{margin:0 0 11px;font-size:16px;line-height:1.4;font-weight:600;
    color:{C['ink']};letter-spacing:-0.1px;}}
  .card .stage{{display:inline-block;font-size:12.5px;color:{C['ink']};
    background:{C['surface']};border-left:2px solid {C['accent']};
    padding:6px 12px;margin-bottom:11px;}}
  .card .stage.wide{{display:block;}}
  .card .code{{display:inline-block;font-size:10.5px;font-weight:700;
    color:{C['accent_ink']};margin-right:9px;{B.FONT_NUMERIC}}}
  .card .sum{{font-size:13.5px;line-height:1.6;color:{C['ink_65']};}}
  .card .chips{{margin-top:13px;font-size:10.5px;color:{C['accent_ink']};}}
  .card .chip{{margin-right:14px;white-space:nowrap;}}
  .card .note{{margin-top:13px;padding:10px 14px;background:{C['accent_soft']};
    border-left:2px solid {C['accent']};font-size:12.5px;color:{C['ink']};font-weight:500;}}
  .card .src{{margin-top:14px;font-size:12.5px;font-weight:600;}}

  /* ---------- подвал ---------- */
  .foot{{margin-top:36px;padding-top:18px;border-top:2px solid {C['accent']};
    font-size:11px;line-height:1.75;color:{C['ink_45']};}}
  .foot b{{color:{C['ink']};font-weight:600;}}
  .empty{{background:{C['white']};border:1px solid {C['line']};border-top:none;
    padding:38px;text-align:center;font-size:14.5px;color:{C['ink_65']};}}

  /* ---------- печать и PDF ---------- */
  @page{{size:A4;margin:14mm 12mm 16mm;}}
  @media print{{
    html,body{{background:#fff;font-size:10.5pt;}}
    .sheet{{max-width:none;padding:0;}}
    /* Подложки и линии — часть фирменного стиля, а не украшение:
       без этого правила браузер печатает их белыми. */
    *{{-webkit-print-color-adjust:exact;print-color-adjust:exact;}}
    .card,.dl .row,.dl{{break-inside:avoid;page-break-inside:avoid;}}
    .sec{{break-after:avoid;page-break-after:avoid;}}
    .no-print{{display:none!important;}}
    /* Адрес первоисточника на бумаге по ссылке не откроешь. */
    a.src::after{{content:" (" attr(href) ")";font-weight:400;font-size:9pt;
      color:{C['ink_45']};word-break:break-all;}}
  }}
"""


def _figure(value: int, label: str, color: str) -> str:
    cls = "fig zero" if not value else "fig"
    return (f'<div class="{cls}"><div class="v num" style="color:{color};">{value}</div>'
            f'<div class="l">{_esc(label)}</div></div>')


def _card(entry, u: str) -> str:
    meta_u = U[u]
    color = meta_u["color"]
    r, meta = entry["row"], _row_meta(entry["row"])
    code, stage = _stage_short(r["stage"])
    pub = parse_dt(r["published_at"])
    org_color = B.AUTHORITY.get(r["authority"], C["ink_65"])

    P = [f'<div class="card" style="border-left-color:{color};">']

    top = [f'<span class="org" style="color:{org_color};">{_esc(r["authority"])}</span>']
    if pub:
        top.append(f'<span class="dot">·</span><span class="when">{ru_date(pub)}</span>')
    ev = ", ".join(sorted({EVENT_LABEL.get(x, x) for x in entry["events"]}))
    if ev:
        top.append(f'<span class="dot">·</span><span class="ev">{_esc(ev)}</span>')
    P.append(f'<div class="top">{"".join(top)}</div>')

    P.append(f'<h3>{_esc(squeeze(r["title"], 300))}</h3>')

    if stage:
        badge = f'<span class="code">{_esc(code)}</span>' if code else ""
        wide = " wide" if len(stage) > 62 else ""
        P.append(f'<div><span class="stage{wide}">{badge}{_esc(stage)}</span></div>')

    if r["summary"]:
        P.append(f'<div class="sum">{_esc(squeeze(r["summary"], 700))}</div>')

    tops = _topics(r)
    if tops:
        chips = "".join(f'<span class="chip">{_esc(t)}</span>' for t in tops[:4])
        P.append(f'<div class="chips">{chips}</div>')

    if meta.get("comments_until"):
        contact = ""
        if meta.get("contact"):
            contact = f' · <a href="mailto:{_esc(meta["contact"])}">{_esc(meta["contact"])}</a>'
        P.append(f'<div class="note">Замечания принимаются до '
                 f'{_esc(meta["comments_until"])}{contact}</div>')

    if r["url"]:
        P.append(f'<div class="src"><a class="src" href="{_esc(r["url"])}">'
                 f'Первоисточник →</a></div>')

    P.append("</div>")
    return "".join(P)


def render_html(buckets, rows, health, period_label: str, dls) -> str:
    c = _counts(buckets)
    total = sum(c.values())

    P = [f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Мониторинг регулирования частных инвестиций — {_esc(period_label)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="{B.FONT_URL}">
<style>{STYLE}</style></head>
<body><div class="sheet">"""]

    # ---------- шапка ----------
    P.append(f"""<div class="head">
 <div class="eyebrow">Мониторинг регулирования</div>
 <h1>Частные инвестиции</h1>
 <div class="org">Банк России &middot; Минфин &middot; Государственная Дума &middot;
  Совет Федерации</div>
 <div class="meta">{_esc(period_label)} &nbsp;·&nbsp; сформировано
  {now_msk():%d.%m.%Y} в {now_msk():%H:%M} МСК</div>
</div><div class="rule"></div>""")

    # ---------- сводка: то, что читается за минуту ----------
    P.append('<div class="panel">')
    P.append(f'<div class="verdict">{_esc(_verdict(c, total, dls))}</div>')
    if total:
        P.append('<div class="figures">')
        P.append(_figure(c["critical"], "требует решения", U["critical"]["color"]))
        P.append(_figure(c["high"], "важное", U["high"]["color"]))
        P.append(_figure(c["normal"] + c["low"], "к сведению", U["normal"]["color"]))
        P.append(_figure(total, "всего", C["accent_ink"]))
        P.append("</div>")
        auth = _by_authority(buckets)
        if auth:
            items = f'<span class="sep">|</span>'.join(
                f'<b style="color:{B.AUTHORITY.get(k, C["ink"])};">{_esc(k)}</b>&nbsp;'
                f'<span class="num">{v}</span>' for k, v in auth)
            P.append(f'<div class="orgs">{items}</div>')
    P.append("</div>")

    # ---------- сроки: единственное с жёстким дедлайном ----------
    if dls:
        P.append('<div class="dl"><h2>Сроки подачи замечаний</h2>')
        for dt, r, meta in dls:
            days = (dt.date() - now_msk().date()).days
            when = "сегодня" if days == 0 else (
                "завтра" if days == 1 else f"через {days} {_plural(days, 'день', 'дня', 'дней')}")
            soon = " soon" if days <= 3 else ""
            title = _esc(squeeze(r["title"], 150))
            link = f'<a href="{_esc(r["url"])}">{title}</a>' if r["url"] else title
            contact = (f'<div class="contact">{_esc(meta["contact"])}</div>'
                       if meta.get("contact") else "")
            P.append(f'<div class="row"><div class="when{soon}">{dt:%d.%m.%Y} '
                     f'<span>{when}</span></div>'
                     f'<div class="what">{link}{contact}</div></div>')
        P.append("</div>")

    # ---------- документы по убыванию срочности ----------
    for u in ORDER:
        entries = buckets.get(u) or []
        if not entries:
            continue
        m = U[u]
        P.append(f'<div class="sec"><span class="t" style="color:{m["color"]};">'
                 f'{_esc(m["label"])}</span>'
                 f'<span class="n num" style="color:{m["color"]};">'
                 f'{len(entries)}</span><span class="line"></span></div>')
        for e in entries:
            P.append(_card(e, u))

    if not total:
        P.append('<div class="empty">За период значимых изменений не зафиксировано.<br>'
                 'Источники опрошены, документы по темам мониторинга не найдены.</div>')

    # Состояние источников в отчёт не попадает: это служебная диагностика,
    # руководителю она ничего не говорит, а доверие к документу подрывает.
    # Смотреть её — `python3 -m regwatch doctor`.

    P.append('<div class="foot"><b>Источники:</b> Банк России, Министерство финансов, '
             'Государственная Дума, Совет Федерации, портал официального опубликования, '
             'профильные СМИ.<br>'
             'Отчёт сформирован автоматически; сведения приведены по данным '
             'первоисточников на момент сбора.</div>')
    P.append("</div></body></html>")
    return "\n".join(P)

# ---------------- модель отчёта ----------------
# HTML и PDF собираются из одного описания. Иначе два оформления неизбежно
# разъезжаются: правка в одном месте забывается в другом, и руководитель
# получает PDF, не совпадающий с тем, что видно в приложении.

def to_model(buckets, rows, health, period_label: str, dls) -> dict:
    """Плоское описание отчёта — пригодное для JSON и для любого движка."""
    c = _counts(buckets)
    total = sum(c.values())

    def item(entry, u):
        r, meta = entry["row"], _row_meta(entry["row"])
        code, stage = _stage_short(r["stage"])
        pub = parse_dt(r["published_at"])
        return {
            "authority": r["authority"],
            "authority_color": B.AUTHORITY.get(r["authority"], C["ink_65"]),
            "date": ru_date(pub) if pub else "",
            "events": ", ".join(sorted({EVENT_LABEL.get(x, x) for x in entry["events"]})),
            "title": squeeze(r["title"], 300),
            "stage_code": code or "",
            "stage": stage or "",
            "summary": squeeze(r["summary"], 700) if r["summary"] else "",
            "topics": _topics(r)[:4],
            "deadline": meta.get("comments_until") or "",
            "contact": meta.get("contact") or "",
            "url": r["url"] or "",
        }

    sections = []
    for u in ORDER:
        entries = buckets.get(u) or []
        if entries:
            sections.append({"key": u, "label": U[u]["label"], "color": U[u]["color"],
                             "soft": U[u]["soft"],
                             "items": [item(e, u) for e in entries]})

    dl_out = []
    for dt, r, meta in dls:
        days = (dt.date() - now_msk().date()).days
        when = "сегодня" if days == 0 else (
            "завтра" if days == 1 else f"через {days} {_plural(days, 'день', 'дня', 'дней')}")
        dl_out.append({"date": f"{dt:%d.%m.%Y}", "when": when, "soon": days <= 3,
                       "title": squeeze(r["title"], 150), "url": r["url"] or "",
                       "contact": meta.get("contact") or ""})

    return {
        "label": period_label,
        "generated": f"{now_msk():%d.%m.%Y} в {now_msk():%H:%M} МСК",
        "verdict": _verdict(c, total, dls),
        "counts": {"critical": c["critical"], "high": c["high"],
                   "info": c["normal"] + c["low"], "total": total},
        "authorities": [[k, v] for k, v in _by_authority(buckets)],
        "deadlines": dl_out,
        "sections": sections,
        # health в модель не кладём: PDF читает руководитель, а диагностика
        # источников — дело эксплуатации (`regwatch doctor`).
    }
