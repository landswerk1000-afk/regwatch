"""PDF-версия отчёта — то, что уходит руководителю.

HTML хорош на экране, но переслать его неудобно: файл открывается по-разному
в разных браузерах, а в почте и мессенджерах выглядит вложением непонятного
рода. PDF открывается одинаково везде и печатается без сюрпризов.

Сборка вынесена в отдельный процесс (.venv-pdf), чтобы reportlab не попал
в зависимости ядра: задача по расписанию должна переживать обновления системы.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .. import brand as B

RENDERER = Path(__file__).with_name("_pdf_render.py")
VENV = ".venv-pdf"


class PdfNotConfigured(Exception):
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
    return _interpreter(root, "reportlab")


def configured(root: Path) -> bool:
    return venv_python(root) is not None and RENDERER.exists()


def check(root: Path) -> str | None:
    if venv_python(root) is None:
        return ("нет reportlab — создайте окружение: "
                "python3 -m venv .venv-pdf && ./.venv-pdf/bin/pip install reportlab")
    if not RENDERER.exists():
        return f"нет сборщика {RENDERER.name}"
    return None


def _brand_payload() -> dict:
    return {"colors": B.COLORS, "urgency": B.URGENCY, "authority": B.AUTHORITY}


def build(root: Path, model: dict, out_path: Path) -> dict:
    """Собирает PDF из модели отчёта. Возвращает сведения о результате."""
    problem = check(root)
    if problem:
        raise PdfNotConfigured(problem)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    task = {"model": model, "brand": _brand_payload(), "out": str(out_path)}
    proc = subprocess.run(
        [str(venv_python(root)), str(RENDERER)],
        input=json.dumps(task, ensure_ascii=False),
        capture_output=True, text=True, timeout=180)

    try:
        res = json.loads((proc.stdout or "").strip().splitlines()[-1])
    except Exception:
        raise RuntimeError(
            f"сборщик не ответил: {(proc.stderr or proc.stdout or '')[:300]}")
    if res.get("fatal"):
        raise RuntimeError(res["fatal"])
    return res
