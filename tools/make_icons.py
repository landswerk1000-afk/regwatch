"""Значок приложения — тот же знак, что в webapp/logo.svg.

Знак про то, чем занят агент: поток документов, один из них важен,
и у него есть срок, после которого говорить уже поздно. Отсюда
две тусклые черты, бирюзовая полоса и коралловая отсечка на её конце.

Рисуем со сглаживанием: считаем цвет по сетке 4×4 внутри каждой точки
и усредняем. Без этого скругления выходят лесенкой — на экране телефона
это сразу видно.

Плитка здесь во весь квадрат, без скруглений: телефон обрезает значок
своей маской, и собственные углы остались бы белой каймой под ней.
Скруглённая плитка есть в logo.svg — она для сайта и документов.

Знак свой, а не логотип фонда: повторять чужую марку нельзя. Взяты
только цвета — бирюза, графит и коралл.
"""
import struct, zlib, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from regwatch import brand as B

SS = 4  # сглаживание: 4×4 выборки на точку


def hx(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


BG    = hx(B.INK)           # плитка во весь квадрат
CYAN  = hx(B.ACCENT)
CORAL = hx(B.CORAL)
MUTED = hx("#6f7074")


def round_rect(x, y, x0, y0, x1, y1, r):
    """Точка внутри прямоугольника со скруглёнными углами.

    Доли, а не точки: один и тот же расчёт годится для любого размера.
    """
    if not (x0 <= x <= x1 and y0 <= y <= y1):
        return False
    dx = max(x0 + r - x, 0.0, x - (x1 - r))
    dy = max(y0 + r - y, 0.0, y - (y1 - r))
    return dx * dx + dy * dy <= r * r


# Те же доли, что в webapp/logo.svg: сетка 100×100, делённая на 100.
MARKS = (
    (MUTED, 0.23, 0.28, 0.67, 0.33, 0.025),   # черта потока документов
    (MUTED, 0.23, 0.40, 0.51, 0.45, 0.025),   # вторая, короче
    (CYAN,  0.23, 0.53, 0.63, 0.64, 0.055),   # окно: пока можно повлиять
    (CORAL, 0.68, 0.46, 0.77, 0.71, 0.045),   # отсечка: срок истёк
)


def sample(x, y, size):
    """Цвет в точке (x, y) — координаты в долях от размера."""
    for color, x0, y0, x1, y1, r in MARKS:
        if round_rect(x, y, x0, y0, x1, y1, r):
            return color
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
