"""Сборка PDF. Запускается ИНТЕРПРЕТАТОРОМ ИЗ .venv-pdf, не системным.

reportlab в стандартную библиотеку не входит, а ядро агента должно
оставаться без зависимостей: задача по расписанию обязана переживать
обновления системы. Поэтому зависимость изолирована здесь, а основной код
передаёт сюда описание отчёта через JSON на stdin.

Формат входа:  {"model": {...}, "brand": {...}, "out": "путь.pdf"}
Формат выхода: {"ok": true, "path": "...", "pages": N} либо {"fatal": "..."}
"""
import json
import os
import sys

# Шрифт. Inter — фирменный, но в macOS его нет: если вы установили его
# в ~/Library/Fonts, он подхватится сам. Иначе берём Helvetica Neue —
# из системных она ближе всего к Inter по рисунку и ширине знаков.
FONT_CANDIDATES = [
    ("Inter", os.path.expanduser("~/Library/Fonts/Inter-Regular.ttf"),
     os.path.expanduser("~/Library/Fonts/Inter-SemiBold.ttf"),
     os.path.expanduser("~/Library/Fonts/Inter-Bold.ttf"), None),
    ("Inter", os.path.expanduser("~/Library/Fonts/InterDisplay-Regular.ttf"),
     os.path.expanduser("~/Library/Fonts/InterDisplay-SemiBold.ttf"),
     os.path.expanduser("~/Library/Fonts/InterDisplay-Bold.ttf"), None),
    ("HelveticaNeue", "/System/Library/Fonts/HelveticaNeue.ttc",
     "/System/Library/Fonts/HelveticaNeue.ttc",
     "/System/Library/Fonts/HelveticaNeue.ttc", (0, 1, 1)),
    ("Arial", "/System/Library/Fonts/Supplemental/Arial.ttf",
     "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
     "/System/Library/Fonts/Supplemental/Arial Bold.ttf", None),
    # Linux: агент может работать не на Mac, а на сервере сборки.
    ("Inter", "/usr/share/fonts/truetype/inter/Inter-Regular.ttf",
     "/usr/share/fonts/truetype/inter/Inter-SemiBold.ttf",
     "/usr/share/fonts/truetype/inter/Inter-Bold.ttf", None),
    ("Inter", "/usr/share/fonts/opentype/inter/Inter-Regular.otf",
     "/usr/share/fonts/opentype/inter/Inter-SemiBold.otf",
     "/usr/share/fonts/opentype/inter/Inter-Bold.otf", None),
    # Liberation Sans повторяет метрики Arial — вёрстка не поедет.
    ("LiberationSans", "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
     "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
     "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf", None),
    ("DejaVuSans", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", None),
]

REG, SEMI, BOLD = "rw-regular", "rw-semibold", "rw-bold"


def register_font():
    """Возвращает имя выбранного семейства или падает с понятным текстом."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    for family, r_path, s_path, b_path, idx in FONT_CANDIDATES:
        if not (os.path.exists(r_path) and os.path.exists(b_path)):
            continue
        try:
            ri, si, bi = idx or (None, None, None)
            for name, path, sub in ((REG, r_path, ri), (SEMI, s_path, si), (BOLD, b_path, bi)):
                kw = {"subfontIndex": sub} if sub is not None else {}
                pdfmetrics.registerFont(TTFont(name, path, **kw))
            # Проверяем делом: шрифт без кириллицы выдал бы пустые прямоугольники
            # уже в готовом файле, когда исправлять поздно.
            if pdfmetrics.stringWidth("Проект указания", REG, 10) <= 0:
                continue
            return family
        except Exception:
            continue
    raise RuntimeError("не нашёл ни одного шрифта с кириллицей")


def build(model, brand, out_path):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (BaseDocTemplate, Frame, KeepTogether, PageTemplate,
                                    Paragraph, Spacer, Table, TableStyle)

    family = register_font()
    C = brand["colors"]
    ink = colors.HexColor(C["ink"])
    ink65 = colors.HexColor(C["ink_65"])
    accent = colors.HexColor(C["accent"])
    line = colors.HexColor(C["line"])
    line_soft = colors.HexColor(C["line_soft"])
    accent_soft = colors.HexColor(C["accent_soft"])

    PAGE_W, PAGE_H = A4
    M_L = M_R = 16 * mm
    M_T, M_B = 16 * mm, 18 * mm
    W = PAGE_W - M_L - M_R

    def st(name, size, leading=None, color=ink65, font=REG, space_after=0,
           space_before=0, left=0):
        return ParagraphStyle(name, fontName=font, fontSize=size,
                              leading=leading or size * 1.42, textColor=color,
                              spaceAfter=space_after, spaceBefore=space_before,
                              leftIndent=left, alignment=TA_LEFT)

    S = {
        "verdict": st("verdict", 11.5, 16.5, ink, SEMI),
        "title": st("title", 11, 15, ink, SEMI),
        "body": st("body", 9, 13),
        "meta": st("meta", 7.6, 10.5, ink65),
        "chip": st("chip", 7.4, 10, ink65),
        "sec": st("sec", 8, 11, ink, BOLD),
        "foot": st("foot", 7.2, 10.5, ink65),
        "dl_when": st("dl_when", 8.6, 12, ink, SEMI),
        "dl_what": st("dl_what", 9, 12.6, ink),
    }

    def esc(s):
        return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;"))

    story = []

    # ---------- шапка: светлая, как на fgiis.ru ----------
    accent_ink = colors.HexColor(C["accent_ink"])
    head_rows = [
        [Paragraph("МОНИТОРИНГ&nbsp;&nbsp;РЕГУЛИРОВАНИЯ",
                   st("eyebrow", 7, 10, accent_ink, BOLD))],
        [Paragraph("Частные инвестиции", st("h1", 21, 25, ink, BOLD))],
        [Paragraph("Банк России &middot; Минфин &middot; Государственная Дума "
                   "&middot; Совет Федерации", st("horg", 8.6, 12, ink65))],
        [Paragraph(f'{esc(model["label"])} &nbsp;·&nbsp; сформировано '
                   f'{esc(model["generated"])}',
                   st("hmeta", 7.6, 11, colors.HexColor(C["ink_45"])))],
    ]
    head = Table(head_rows, colWidths=[W])
    head.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.white),
        ("BOX", (0, 0), (-1, -1), 0.5, line),
        ("LEFTPADDING", (0, 0), (-1, -1), 14), ("RIGHTPADDING", (0, 0), (-1, -1), 14),
        ("TOPPADDING", (0, 0), (0, 0), 16), ("BOTTOMPADDING", (0, 0), (0, 0), 6),
        ("TOPPADDING", (0, 1), (0, 1), 0), ("BOTTOMPADDING", (0, 1), (0, 1), 3),
        ("TOPPADDING", (0, 2), (0, 2), 0), ("BOTTOMPADDING", (0, 2), (0, 2), 10),
        # Тонкая линия отделяет выходные данные от названия — так делают
        # в служебных документах, и взгляд не путает их с подзаголовком.
        ("LINEABOVE", (0, 3), (0, 3), 0.5, line_soft),
        ("TOPPADDING", (0, 3), (0, 3), 8), ("BOTTOMPADDING", (0, 3), (0, 3), 14),
    ]))
    story.append(head)
    rule = Table([[""]], colWidths=[W], rowHeights=[3])
    rule.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), accent)]))
    story.append(rule)

    # ---------- вердикт и цифры ----------
    verdict = Table([[Paragraph(esc(model["verdict"]), S["verdict"])]], colWidths=[W])
    verdict.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.white),
        ("BOX", (0, 0), (-1, -1), 0.5, line),
        ("LINEBEFORE", (0, 0), (0, -1), 2.5, accent),
        ("LEFTPADDING", (0, 0), (-1, -1), 14), ("RIGHTPADDING", (0, 0), (-1, -1), 14),
        ("TOPPADDING", (0, 0), (-1, -1), 11), ("BOTTOMPADDING", (0, 0), (-1, -1), 11),
    ]))
    story.append(verdict)

    cnt, U = model["counts"], brand["urgency"]
    figs = [(cnt["critical"], "ТРЕБУЕТ РЕШЕНИЯ", U["critical"]["color"]),
            (cnt["high"], "ВАЖНОЕ", U["high"]["color"]),
            (cnt["info"], "К СВЕДЕНИЮ", U["normal"]["color"]),
            (cnt["total"], "ВСЕГО", C["accent_ink"])]
    top, bot = [], []
    for v, label, col in figs:
        faded = col if v else C["line"]
        top.append(Paragraph(f'<font color="{faded}" size="19"><b>{v}</b></font>',
                             st("fv", 19, 21, colors.HexColor(faded), BOLD)))
        bot.append(Paragraph(f'<font color="{C["ink_65"] if v else C["line"]}">'
                             f'{label}</font>', st("fl", 6.6, 9, ink65, SEMI)))
    fig = Table([top, bot], colWidths=[W / 4.0] * 4)
    fig.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.white),
        ("BOX", (0, 0), (-1, -1), 0.5, line),
        ("LINEBEFORE", (1, 0), (-1, -1), 0.5, line_soft),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, 0), 11), ("BOTTOMPADDING", (0, 0), (-1, 0), 2),
        ("TOPPADDING", (0, 1), (-1, 1), 0), ("BOTTOMPADDING", (0, 1), (-1, 1), 11),
    ]))
    story.append(fig)

    if model["authorities"]:
        bits = " &nbsp;&nbsp;|&nbsp;&nbsp; ".join(
            f'<font color="{brand["authority"].get(k, C["ink"])}"><b>{esc(k)}</b></font> {v}'
            for k, v in model["authorities"])
        orgs = Table([[Paragraph(bits, st("orgs", 8.2, 11, ink65))]], colWidths=[W])
        orgs.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.white),
            ("BOX", (0, 0), (-1, -1), 0.5, line),
            ("LEFTPADDING", (0, 0), (-1, -1), 14), ("RIGHTPADDING", (0, 0), (-1, -1), 14),
            ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ]))
        story.append(orgs)

    # ---------- сроки ----------
    if model["deadlines"]:
        story.append(Spacer(1, 14))
        rows = [[Paragraph("СРОКИ ПОДАЧИ ЗАМЕЧАНИЙ", S["sec"]), ""]]
        for d in model["deadlines"]:
            when_col = U["critical"]["color"] if d["soon"] else C["ink_65"]
            rows.append([
                Paragraph(f'{esc(d["date"])}<br/><font color="{when_col}" size="7.6">'
                          f'{esc(d["when"])}</font>', S["dl_when"]),
                Paragraph(esc(d["title"]) + (
                    f'<br/><font size="7.6" color="{C["ink_65"]}">{esc(d["contact"])}</font>'
                    if d["contact"] else ""), S["dl_what"]),
            ])
        t = Table(rows, colWidths=[30 * mm, W - 30 * mm])
        style = [
            ("BACKGROUND", (0, 0), (-1, -1), colors.white),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor(C["accent_edge"])),
            ("LINEBEFORE", (0, 0), (0, -1), 3, accent),
            ("SPAN", (0, 0), (1, 0)),
            ("BACKGROUND", (0, 0), (1, 0), accent_soft),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 12), ("RIGHTPADDING", (0, 0), (-1, -1), 12),
            ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ]
        for i in range(1, len(rows)):
            style.append(("LINEABOVE", (0, i), (-1, i), 0.5, line_soft))
        t.setStyle(TableStyle(style))
        story.append(t)

    # ---------- документы ----------
    for sec in model["sections"]:
        story.append(Spacer(1, 16))
        hdr = Table([[Paragraph(f'<font color="{sec["color"]}">'
                                f'{esc(sec["label"].upper())}</font>', S["sec"]),
                      Paragraph(f'<font color="{sec["color"]}"><b>{len(sec["items"])}</b></font>',
                                st("cnt", 8, 11, colors.HexColor(sec["color"]), BOLD))]],
                    colWidths=[W - 16 * mm, 16 * mm])
        hdr.setStyle(TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("ALIGN", (1, 0), (1, 0), "RIGHT"),
            ("LINEBELOW", (0, 0), (-1, -1), 0.7, colors.HexColor(C["accent_edge"])),
        ]))
        # Заголовок раздела копим вместе с первой карточкой: иначе
        # «ВАЖНОЕ 9» остаётся внизу страницы, а документы уходят на следующую.
        pending = [hdr, Spacer(1, 7)]

        for it in sec["items"]:
            inner = []
            meta_bits = [f'<font color="{it["authority_color"]}"><b>'
                         f'{esc(it["authority"].upper())}</b></font>']
            if it["date"]:
                meta_bits.append(esc(it["date"]))
            if it["events"]:
                meta_bits.append(f'<i>{esc(it["events"])}</i>')
            inner.append(Paragraph(f' <font color="{C["line"]}">·</font> '.join(meta_bits),
                                   S["meta"]))
            inner.append(Spacer(1, 4))
            inner.append(Paragraph(esc(it["title"]), S["title"]))

            if it["stage"]:
                code = (f'<font color="{C["ink"]}"><b>{esc(it["stage_code"])}</b></font>&nbsp;&nbsp;'
                        if it["stage_code"] else "")
                stage_t = Table([[Paragraph(code + esc(it["stage"]),
                                            st("stg", 8.4, 11.5, ink))]],
                                colWidths=[W - 24])
                stage_t.setStyle(TableStyle([
                    ("BACKGROUND", (0, 0), (-1, -1), accent_soft),
                    ("LEFTPADDING", (0, 0), (-1, -1), 7), ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                    ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ]))
                inner.append(Spacer(1, 5))
                inner.append(stage_t)

            if it["summary"]:
                inner.append(Spacer(1, 5))
                inner.append(Paragraph(esc(it["summary"]), S["body"]))

            if it["topics"]:
                inner.append(Spacer(1, 5))
                inner.append(Paragraph(
                    " &nbsp; ".join(f'<font color="{C["accent_ink"]}">· {esc(t)}</font>'
                                    for t in it["topics"]), S["chip"]))

            if it["deadline"]:
                note = f'Замечания принимаются до {esc(it["deadline"])}'
                if it["contact"]:
                    note += f' · {esc(it["contact"])}'
                nt = Table([[Paragraph(note, st("note", 8.4, 11.5, ink, SEMI))]],
                           colWidths=[W - 24])
                nt.setStyle(TableStyle([
                    ("BACKGROUND", (0, 0), (-1, -1), accent_soft),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ]))
                inner.append(Spacer(1, 6))
                inner.append(nt)

            if it["url"]:
                inner.append(Spacer(1, 6))
                # Ссылка в PDF должна быть и кликабельной, и читаемой на бумаге.
                inner.append(Paragraph(
                    f'<link href="{esc(it["url"])}"><font color="{C["accent_ink"]}">'
                    f'<b>Первоисточник →</b></font></link> '
                    f'<font color="{C["ink_65"]}" size="6.8">{esc(it["url"])}</font>',
                    st("src", 8.4, 11.5, ink65)))

            card = Table([[inner]], colWidths=[W])
            card.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), colors.white),
                ("BOX", (0, 0), (-1, -1), 0.5, line),
                ("LINEBEFORE", (0, 0), (0, -1), 2.2, colors.HexColor(sec["color"])),
                ("LEFTPADDING", (0, 0), (-1, -1), 12), ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                ("TOPPADDING", (0, 0), (-1, -1), 10), ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ]))
            # Карточку не режем между страницами: разорванный документ
            # читается как два разных.
            block = pending + [card, Spacer(1, 6)]
            pending = []
            story.append(KeepTogether(block))

    if not model["counts"]["total"]:
        story.append(Spacer(1, 20))
        story.append(Paragraph(
            "За период значимых изменений не зафиксировано. Источники опрошены, "
            "документы по темам мониторинга не найдены.", st("empty", 10, 15, ink65)))

    story.append(Spacer(1, 16))
    foot_rule = Table([[""]], colWidths=[W], rowHeights=[1.6])
    foot_rule.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), accent)]))
    story.append(foot_rule)
    story.append(Spacer(1, 8))
    story.append(Paragraph(
        f'<font color="{C["ink"]}"><b>Источники:</b></font> Банк России, '
        'Министерство финансов, Государственная Дума, Совет Федерации, портал '
        'официального опубликования, профильные СМИ.<br/>'
        'Отчёт сформирован автоматически; сведения приведены по данным '
        'первоисточников на момент сбора.', S["foot"]))

    # ---------- документ и колонтитул ----------
    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(REG, 7)
        canvas.setFillColor(ink65)
        canvas.drawString(M_L, M_B - 9 * mm, "Мониторинг регулирования частных инвестиций")
        canvas.drawRightString(PAGE_W - M_R, M_B - 9 * mm, f"{doc.page}")
        canvas.setStrokeColor(line)
        canvas.setLineWidth(0.5)
        canvas.line(M_L, M_B - 6 * mm, PAGE_W - M_R, M_B - 6 * mm)
        canvas.restoreState()

    doc = BaseDocTemplate(out_path, pagesize=A4,
                          leftMargin=M_L, rightMargin=M_R,
                          topMargin=M_T, bottomMargin=M_B,
                          title="Мониторинг регулирования частных инвестиций",
                          author="Регмонитор", subject=model["label"])
    frame = Frame(M_L, M_B, W, PAGE_H - M_T - M_B, id="main",
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id="p", frames=[frame], onPage=footer)])
    doc.build(story)
    return family


def main() -> int:
    try:
        from reportlab.lib.pagesizes import A4  # noqa: F401
    except ImportError:
        print(json.dumps({"fatal": "reportlab не установлен в .venv-pdf"}))
        return 2
    try:
        task = json.load(sys.stdin)
    except Exception as e:
        print(json.dumps({"fatal": f"не разобрал вход: {e}"}))
        return 2
    try:
        family = build(task["model"], task["brand"], task["out"])
    except Exception as e:
        print(json.dumps({"fatal": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
        return 1
    size = os.path.getsize(task["out"]) if os.path.exists(task["out"]) else 0
    print(json.dumps({"ok": True, "path": task["out"], "font": family, "bytes": size},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
