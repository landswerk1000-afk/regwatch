"""Значок приложения в цветах ФГИИС.

Рисуем со сглаживанием: считаем цвет по сетке 4×4 внутри каждой точки
и усредняем. Без этого круги выходят лесенкой — на экране телефона
это сразу видно.

Знак свой, а не логотип фонда: повторять чужую марку нельзя. Взяты
геометрия (круг) и цвета — бирюза и графит.
"""
import struct, zlib, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from regwatch import brand as B

SS = 4  # сглаживание: 4×4 выборки на точку


def hx(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


BG   = hx("#F6F7F8")
CYAN = hx(B.ACCENT)
INK  = hx(B.INK)


def sample(x, y, size):
    """Цвет в точке (x, y) — координаты в долях от размера."""
    cx = cy = 0.5
    dx, dy = x - cx, y - cy
    r = (dx * dx + dy * dy) ** 0.5

    # Внешнее бирюзовое кольцо — как у эмблемы фонда
    if 0.360 <= r <= 0.418:
        return CYAN

    # Внутри кольца — три графитовых столбца разной высоты:
    # мониторинг, наблюдение за движением. Читается и на 40 точках.
    if r < 0.33:
        for i, (bx, h) in enumerate(((-0.125, 0.075), (0.0, 0.125), (0.125, 0.175))):
            if abs(dx - bx) <= 0.038 and -h <= dy <= 0.098:
                return INK
        # Опорная черта под столбцами
        if abs(dy - 0.133) <= 0.020 and abs(dx) <= 0.172:
            return CYAN
    return BG


def render(size):
    rows = bytearray()
    step = 1.0 / (size * SS)
    for py in range(size):
        rows.append(0)
        for px in range(size):
            acc = [0, 0, 0]
            for sy in range(SS):
                for sx in range(SS):
                    x = (px * SS + sx + 0.5) * step
                    y = (py * SS + sy + 0.5) * step
                    c = sample(x, y, size)
                    acc[0] += c[0]; acc[1] += c[1]; acc[2] += c[2]
            n = SS * SS
            rows += bytes(v // n for v in acc)
    return bytes(rows)


def chunk(t, d):
    return (struct.pack(">I", len(d)) + t + d
            + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff))


def write(path, size):
    data = (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(render(size), 9))
            + chunk(b"IEND", b""))
    open(path, "wb").write(data)
    return len(data)


for s in (192, 512):
    out = Path(__file__).resolve().parent.parent / "webapp" / f"icon-{s}.png"
    n = write(out, s)
    print(f"  {out.name} — {n} байт")
