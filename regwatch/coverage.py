"""Измерение охвата: сколько значимого агент находит и сколько лишнего приносит.

Зачем. Отчёт показывает то, что агент счёл значимым. Чего он НЕ счёл —
не показывает нигде и никогда. 92% собранного получает нулевую оценку,
и до сих пор не было способа узнать, что в этих 92% лежит: только шум
или ещё и пропущенные проекты указаний. Инструмент, про который нельзя
сказать, что он пропускает, юристу верить не на что.

Как. Берём выборку, которую человек в силах просмотреть целиком, —
нормативные документы четырёх ключевых органов, — и размечаем её руками
один раз: значим документ или нет. Разметка живёт в эталон.json и
переживает любые правки словаря. После каждой правки topics.json
измерение повторяется одной командой, и видно не «стало лучше на глаз»,
а «полнота 62% → 94%, точность не упала».

Что считаем:
  полнота  — какую долю значимого агент показал (пропуски — цена ошибки);
  точность — какая доля показанного действительно значима (лишнее — шум).

Третья метка, «?», — для документов, значимость которых по названию
не определить: «О внесении изменений в Указание № 6032-У» не говорит
ни о чём. Они не пропуск словаря, а пропуск источника: у таких записей
нет ни описания, ни текста, и ловить в них нечего. Считаем их отдельно,
чтобы не прятать проблему внутри полноты.
"""
from __future__ import annotations

import json
from pathlib import Path

# Нормативные документы: то, что меняет правила. Новости и анонсы
# в эталон не берём — их значимость субъективна, а объём необозрим.
KINDS = ("draft_act", "draft_law", "law", "published_act", "strategy", "consultation")
AUTHORITIES = ("ЦБ", "ГД", "Минфин", "СФ")

YES, NO, UNKNOWN = "да", "нет", "?"
VALID = (YES, NO, UNKNOWN)

REFERENCE = "эталон.json"


def reference_path(root: Path) -> Path:
    return root / REFERENCE


def load_reference(root: Path) -> dict:
    p = reference_path(root)
    if not p.exists():
        return {"_comment": "", "labels": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"_comment": "", "labels": {}}
    data.setdefault("labels", {})
    return data


def save_reference(root: Path, data: dict) -> None:
    data["_comment"] = (
        "Эталонная разметка для измерения охвата: значим документ или нет. "
        "Заполняется человеком один раз и переживает правки topics.json — "
        "иначе «стало лучше» проверить нечем. «?» — по названию не определить."
    )
    reference_path(root).write_text(
        json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
        encoding="utf-8")


def population(store, kinds=KINDS, authorities=AUTHORITIES) -> list:
    """Документы, по которым меряем. Возвращает строки с оценкой."""
    k = ",".join("?" * len(kinds))
    a = ",".join("?" * len(authorities))
    return store.db.execute(
        f"""SELECT i.id, i.title, i.authority, i.kind, i.published_at,
                   LENGTH(COALESCE(i.summary,'')) AS n_summary,
                   LENGTH(COALESCE(i.body,'')) AS n_body,
                   COALESCE(s.relevance, 0) AS relevance, s.rationale
            FROM items i LEFT JOIN scores s ON s.item_id = i.id
            WHERE i.kind IN ({k}) AND i.authority IN ({a})
            ORDER BY i.authority, i.kind, COALESCE(i.published_at, i.first_seen) DESC""",
        (*kinds, *authorities)).fetchall()


def thematic(relevance: float, published_at) -> float:
    """Оценка без возрастной скидки: «а если бы документ вышел сегодня?».

    Мерить полноту по итоговой оценке нельзя: она умножена на возрастной
    коэффициент, и редакция ОНРФР 2022 года выглядела бы пропуском, хотя
    не показывать её сегодня — правильно. Скидка отвечает за «старое
    не шумит», словарь — за «нужное узнаётся». Это разные вопросы,
    и мерить их надо порознь, иначе непонятно, что чинить.
    """
    from .relevance import recency_factor

    k = recency_factor(published_at) or 1.0
    return min(1.0, relevance / k)


def measure(store, root: Path, floor: float = 0.45) -> dict:
    """Считает полноту и точность по размеченной части выборки."""
    labels = load_reference(root)["labels"]
    rows = population(store)

    shown_yes = missed = shown_no = quiet_no = 0
    unknown_shown = unknown_quiet = 0
    unlabelled = []
    misses, false_hits, aged = [], [], []

    for r in rows:
        mark = (labels.get(r["id"]) or {}).get("значим")
        t = thematic(r["relevance"], r["published_at"])
        shown = t >= floor
        if mark not in VALID:
            unlabelled.append(r)
            continue
        if mark == UNKNOWN:
            unknown_shown += shown
            unknown_quiet += not shown
            continue
        if mark == YES:
            if shown:
                shown_yes += 1
                # Узнан по теме, но сегодня в отчёт не попал бы: так и задумано,
                # и всё же это стоит видеть — иначе цифра полноты приукрашивает.
                if r["relevance"] < floor:
                    aged.append(r)
            else:
                missed += 1
                misses.append(dict(r, thematic=t))
        else:
            if shown:
                shown_no += 1
                false_hits.append(dict(r, thematic=t))
            else:
                quiet_no += 1

    relevant = shown_yes + missed
    shown = shown_yes + shown_no
    return {
        "labelled": relevant + shown_no + quiet_no,
        "unlabelled": unlabelled,
        "total": len(rows),
        "relevant": relevant,
        "shown": shown,
        "recall": (shown_yes / relevant) if relevant else None,
        "precision": (shown_yes / shown) if shown else None,
        "missed": missed,
        "misses": misses,
        "false_hits": false_hits,
        "unknown": unknown_shown + unknown_quiet,
        "unknown_quiet": unknown_quiet,
        "aged": aged,
        # Документы, размеченные когда-то, но выбывшие из базы: чистка
        # их не трогает (см. store.prune), так что это сигнал о поломке.
        "gone": sorted(set(labels) - {r["id"] for r in rows}),
    }


def render(m: dict) -> str:
    """Человеческий вывод: числа плюс то, что за ними стоит."""
    L = []
    pct = lambda v: "—" if v is None else f"{v:.0%}"          # noqa: E731
    L.append(f"Выборка: {m['total']} нормативных документов "
             f"({', '.join(AUTHORITIES)}), размечено {m['labelled']}.")
    if m["unlabelled"]:
        L.append(f"Не размечено: {len(m['unlabelled'])} — они в счёт не идут.")
    L.append("Считаем по оценке без возрастной скидки: вопрос «узнает ли агент")
    L.append("такой документ», а не «покажет ли он документ 2019 года».")
    L.append("")
    L.append(f"  Полнота   {pct(m['recall']):>5}   "
             f"узнано {m['relevant'] - m['missed']} значимых из {m['relevant']}")
    L.append(f"  Точность  {pct(m['precision']):>5}   "
             f"из {m['shown']} узнанных значимы {m['relevant'] - m['missed']}")
    if m["aged"]:
        L.append("")
        L.append(f"  Возрастная скидка увела ниже порога: {len(m['aged'])} — "
                 "это намеренно,")
        L.append("  архивные редакции не должны вытеснять действующие документы.")
    if m["unknown"]:
        L.append("")
        L.append(f"  По названию не определить: {m['unknown']} "
                 f"(из них молча пропущено {m['unknown_quiet']}).")
        L.append("  Это пробел источника, а не словаря: у таких записей")
        L.append("  нет ни описания, ни текста — ловить нечего.")
    if m["misses"]:
        L.append("")
        L.append("Пропущено значимое:")
        for r in m["misses"]:
            why = "нет текста" if not (r["n_summary"] or r["n_body"]) else "словарь"
            L.append(f"  · [{r['authority']}, {why}] {r['title'][:80]}")
    if m["false_hits"]:
        L.append("")
        L.append("Показано лишнее:")
        for r in m["false_hits"]:
            L.append(f"  · {r['relevance']:.2f} [{r['authority']}] {r['title'][:82]}")
    if m["gone"]:
        L.append("")
        L.append(f"Размечено, но исчезло из базы: {len(m['gone'])} — "
                 "чистка не должна была их трогать.")
    return "\n".join(L)


def review_file(store, root: Path, out: Path) -> int:
    """Выписывает неразмеченное в файл, готовый к заполнению руками."""
    labels = load_reference(root)["labels"]
    rows = [r for r in population(store)
            if (labels.get(r["id"]) or {}).get("значим") not in VALID]
    out.write_text(json.dumps({
        "_как_заполнять": "В поле «значим» поставьте «да», «нет» или «?» "
                          "(если по названию не определить). Затем: "
                          "regwatch coverage --apply <этот файл>",
        "labels": {r["id"]: {"значим": "", "название": r["title"],
                             "орган": r["authority"], "вид": r["kind"],
                             "оценка": round(r["relevance"], 2)}
                   for r in rows},
    }, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return len(rows)


def apply_file(root: Path, src: Path) -> tuple:
    """Переносит заполненный файл в эталон. Возвращает (принято, пропущено)."""
    data = json.loads(src.read_text(encoding="utf-8"))
    ref = load_reference(root)
    taken = skipped = 0
    for iid, rec in (data.get("labels") or {}).items():
        mark = (rec or {}).get("значим", "").strip()
        if mark not in VALID:
            skipped += 1
            continue
        ref["labels"][iid] = {"значим": mark, "название": rec.get("название", ""),
                              "кто": rec.get("кто", "человек")}
        if rec.get("почему"):
            ref["labels"][iid]["почему"] = rec["почему"]
        taken += 1
    save_reference(root, ref)
    return taken, skipped
