"""Minimal decoder for 8-bit RGBA, non-interlaced PNGs (macOS screenshots), so
pixel colours can be sampled without PIL or ImageMagick."""
import struct
import zlib


def _paeth(a: int, b: int, c: int) -> int:
    pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def load(path: str) -> tuple[int, int, bytearray]:
    """Return (width, height, pixels) with pixels as flat RGBA bytes."""
    data = open(path, "rb").read()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    w = h = 0
    idat = b""
    i = 8
    while i < len(data):
        (n,) = struct.unpack(">I", data[i:i + 4])
        kind, body = data[i + 4:i + 8], data[i + 8:i + 8 + n]
        if kind == b"IHDR":
            w, h, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", body)
            assert depth == 8 and ctype == 6 and interlace == 0, "only 8-bit RGBA, non-interlaced"
        elif kind == b"IDAT":
            idat += body
        i += 12 + n

    raw = zlib.decompress(idat)
    bpp, stride = 4, w * 4
    out = bytearray()
    prev = bytearray(stride)
    p = 0
    for _ in range(h):
        filt = raw[p]
        line = bytearray(raw[p + 1:p + 1 + stride])
        p += 1 + stride
        for x in range(stride):
            a = line[x - bpp] if x >= bpp else 0
            b = prev[x]
            c = prev[x - bpp] if x >= bpp else 0
            if filt == 1:
                line[x] = (line[x] + a) & 255
            elif filt == 2:
                line[x] = (line[x] + b) & 255
            elif filt == 3:
                line[x] = (line[x] + (a + b) // 2) & 255
            elif filt == 4:
                line[x] = (line[x] + _paeth(a, b, c)) & 255
        out += line
        prev = line
    return w, h, out
