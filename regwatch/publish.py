"""Публикация веб-приложения на GitHub Pages.

Зачем. Агент кладёт свежие отчёты в `webapp/`, но на сайт они сами не
попадают: GitHub Pages отдаёт то, что в репозитории. Пока обновление делалось
руками, приложение на телефоне показывало вчерашний отчёт — ради чего тогда
автономность.

Как. Держим рабочую копию репозитория в `data/publish/` (папка `data/`
и так не выкладывается), после каждого прогона переносим туда содержимое
`webapp/`, коммитим и отправляем. Пустой коммит не делается: если ничего
не изменилось, отправки нет.

Токен. Живёт только в `~/.regwatch.env` и передаётся git через GIT_ASKPASS —
в аргументы процесса и в .git/config он не попадает.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

ASKPASS = Path(__file__).with_name("_git_askpass.py")


class PublishNotConfigured(Exception):
    pass


def settings() -> dict:
    return {
        "token": os.environ.get("GITHUB_TOKEN", "").strip(),
        "repo": os.environ.get("GITHUB_REPO", "").strip(),
        "branch": os.environ.get("GITHUB_BRANCH", "").strip() or "main",
    }


def configured() -> bool:
    # На сервере сборки публикует сам рабочий процесс: он уже находится
    # внутри репозитория. Второй механизм здесь только мешал бы —
    # два коммита подряд и гонка при отправке.
    if os.environ.get("GITHUB_ACTIONS") == "true":
        return False
    s = settings()
    return bool(s["token"] and s["repo"])


def check() -> str | None:
    s = settings()
    if not s["repo"]:
        return "нет GITHUB_REPO в ~/.regwatch.env (вида «логин/regwatch»)"
    if "/" not in s["repo"]:
        return f"GITHUB_REPO должен быть вида «логин/repo», а не «{s['repo']}»"
    if not s["token"]:
        return "нет GITHUB_TOKEN в ~/.regwatch.env"
    if shutil.which("git") is None:
        return "git не установлен"
    return None


def _run(args, cwd=None, token=None, timeout=120):
    env = dict(os.environ)
    if token:
        env["REGWATCH_GIT_TOKEN"] = token
        env["GIT_ASKPASS"] = str(ASKPASS)
        env["GIT_TERMINAL_PROMPT"] = "0"
    p = subprocess.run(["git", *args], cwd=str(cwd) if cwd else None,
                       capture_output=True, text=True, timeout=timeout, env=env)
    if p.returncode != 0:
        err = (p.stderr or p.stdout or "").strip()
        # Токен в сообщение об ошибке попасть не должен ни при каких условиях.
        if token:
            err = err.replace(token, "***")
        raise RuntimeError(f"git {args[0]}: {err[:300]}")
    return p.stdout


# Имя отчёта, которое даёт agent.run(): 2026-09-27_0946_daily.html
REPORT_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{4}_(daily|alert)\.html$")


def _sync(src: Path, dst: Path, keep_reports: int = 12) -> None:
    """Обновляет файлы сайта в рабочей копии.

    ВАЖНО: трогает только сайт. Прежняя версия чистила рабочую копию
    подчистую и клала туда одно содержимое webapp/. Это было верно, пока
    в репозитории лежал только сайт; после переезда агента туда же такой
    прогон снёс бы и код, и файл расписания — агент перестал бы
    запускаться вовсе. Не выстрелило лишь потому, что на сервере
    публикация отключена, а локально шли сухие прогоны.

    Отчёты не сверяем с локальной папкой. Сервер и ноутбук ведут свои
    истории: утренний прогон в облаке кладёт отчёт, которого на ноутбуке
    нет и не будет. Правило «нет локально — удалить» снесло бы его с сайта.
    Поэтому берём объединение и оставляем N самых свежих по имени —
    имя начинается с даты, так что это и есть «самые свежие».
    """
    for f in src.iterdir():
        if f.name.startswith(".") or f.is_dir():
            continue
        shutil.copy2(f, dst / f.name)

    names = sorted((p.name for p in dst.iterdir()
                    if p.is_file() and REPORT_NAME.match(p.name)), reverse=True)
    for stale in names[keep_reports:]:
        (dst / stale).unlink()


def publish(root: Path) -> dict:
    """Выкладывает webapp/ на GitHub Pages. Возвращает сводку."""
    problem = check()
    if problem:
        raise PublishNotConfigured(problem)

    s = settings()
    token, repo, branch = s["token"], s["repo"], s["branch"]
    url = f"https://github.com/{repo}.git"
    work = root / "data" / "publish"
    webapp = root / "webapp"
    if not webapp.exists():
        raise PublishNotConfigured("нет папки webapp/")

    if not (work / ".git").exists():
        work.parent.mkdir(parents=True, exist_ok=True)
        if work.exists():
            shutil.rmtree(work)
        try:
            _run(["clone", "--depth", "1", "--branch", branch, url, str(work)],
                 token=token, timeout=300)
        except RuntimeError:
            # Пустой репозиторий клонировать по ветке нельзя: ветки ещё нет,
            # она появляется с первым коммитом. Начинаем историю сами.
            if work.exists():
                shutil.rmtree(work)
            work.mkdir(parents=True)
            _run(["init", "-q"], cwd=work)
            _run(["checkout", "-q", "-B", branch], cwd=work)
    else:
        # Сбрасываемся на то, что в репозитории: наша копия — не источник
        # истории, а лишь способ её пополнить.
        try:
            _run(["fetch", "--depth", "1", url, branch], cwd=work, token=token, timeout=180)
            _run(["reset", "--hard", "FETCH_HEAD"], cwd=work)
            _run(["clean", "-fd"], cwd=work)
        except RuntimeError:
            # На той стороне ещё нет ветки — нечего подтягивать.
            _run(["checkout", "-q", "-B", branch], cwd=work)

    # Личность нужна только этому репозиторию — глобальные настройки не трогаем.
    _run(["config", "user.email", "regwatch@local"], cwd=work)
    _run(["config", "user.name", "Регмонитор"], cwd=work)
    # Отчёты — мелочь, но отправка идёт тем же путём, что и база состояния,
    # а та на 4.5 МБ обрывалась с «RPC failed; HTTP 400».
    _run(["config", "http.postBuffer", "524288000"], cwd=work)
    _run(["config", "http.version", "HTTP/1.1"], cwd=work)

    _sync(webapp, work)
    _run(["add", "-A"], cwd=work)

    files = len([f for f in webapp.iterdir() if f.is_file() and not f.name.startswith(".")])
    changed = bool(_run(["status", "--porcelain"], cwd=work).strip())
    if changed:
        _run(["commit", "-m", f"Отчёты и приложение: {files} файлов"], cwd=work)

    # «Нет изменений в файлах» и «нечего отправлять» — разные вещи: коммит
    # мог остаться неотправленным, если прошлая попытка упала на отправке.
    # Поэтому решает сравнение с тем, что реально лежит на той стороне.
    try:
        local = _run(["rev-parse", "HEAD"], cwd=work).strip()
    except RuntimeError:
        return {"pushed": False, "detail": "публиковать нечего — нет ни одного коммита"}
    try:
        remote_out = _run(["ls-remote", url, branch], cwd=work, token=token, timeout=120)
        remote = remote_out.split()[0] if remote_out.strip() else ""
    except RuntimeError:
        remote = ""

    if remote == local:
        return {"pushed": False, "detail": "сайт уже содержит эти файлы"}

    _run(["push", url, f"HEAD:{branch}"], cwd=work, token=token, timeout=300)
    return {"pushed": True, "detail": f"опубликовано {files} файлов",
            "url": f"https://{repo.split('/')[0]}.github.io/{repo.split('/')[1]}/"}
