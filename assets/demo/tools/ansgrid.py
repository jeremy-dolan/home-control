"""Parse a .ans file into rows of (char, style) cells and serialize back."""
import re
from dataclasses import dataclass, replace

SGR = re.compile(r"\x1b\[([0-9;:]*)m")


@dataclass(frozen=True)
class Style:
    fg: tuple | None = None   # None | ('ansi', n) | ('256', n) | ('rgb', r, g, b)
    bold: bool = False
    dim: bool = False


def apply(style: Style, params: str) -> Style:
    toks = [t for t in re.split(r"[;:]", params)] if params else ["0"]
    toks = [int(t) if t else 0 for t in toks]
    i = 0
    while i < len(toks):
        p = toks[i]
        if p == 0:
            style = Style()
        elif p == 1:
            style = replace(style, bold=True)
        elif p == 2:
            style = replace(style, dim=True)
        elif p == 22:
            style = replace(style, bold=False, dim=False)
        elif 30 <= p <= 37:
            style = replace(style, fg=("ansi", p - 30))
        elif 90 <= p <= 97:
            style = replace(style, fg=("ansi", p - 90 + 8))
        elif p in (23, 24, 25, 27, 49):
            pass
        elif p == 39:
            style = replace(style, fg=None)
        elif p == 38:
            if toks[i + 1] == 5:
                style = replace(style, fg=("256", toks[i + 2]))
                i += 2
            elif toks[i + 1] == 2:
                style = replace(style, fg=("rgb", *toks[i + 2:i + 5]))
                i += 4
        else:
            raise ValueError(f"unhandled SGR {p} in {params!r}")
        i += 1
    return style


def parse(text: str) -> list[list[tuple[str, Style]]]:
    rows: list[list[tuple[str, Style]]] = []
    style = Style()
    for line in text.split("\n"):
        row = []
        pos = 0
        for m in SGR.finditer(line):
            row += [(ch, style) for ch in line[pos:m.start()]]
            style = apply(style, m.group(1))
            pos = m.end()
        row += [(ch, style) for ch in line[pos:]]
        rows.append(row)
    return rows


def sgr(style: Style) -> str:
    parts = ["0"]
    if style.bold:
        parts.append("1")
    if style.dim:
        parts.append("2")
    fg = style.fg
    if fg is not None:
        if fg[0] == "ansi":
            parts.append(str(30 + fg[1]) if fg[1] < 8 else str(90 + fg[1] - 8))
        elif fg[0] == "256":
            parts += ["38", "5", str(fg[1])]
        else:
            parts += ["38", "2", *map(str, fg[1:])]
    return "\x1b[" + ";".join(parts) + "m"


def serialize_row(row) -> str:
    out = []
    cur = None
    for ch, st in row:
        if ch == " " and cur is not None:
            out.append(ch)       # foreground-only styling: a space looks the same in any style
            continue
        if st != cur:
            out.append(sgr(st))
            cur = st
        out.append(ch)
    if cur is not None and cur != Style():
        out.append("\x1b[0m")
    return "".join(out)


def plain(row) -> str:
    return "".join(ch for ch, _ in row)
