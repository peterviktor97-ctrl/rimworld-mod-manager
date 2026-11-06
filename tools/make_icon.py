"""Write assets/icon.ico (16/32/48/256 px, PNG-in-ICO) with the stdlib only.

Same design as rmm.gui.app.make_icon (the window icon): a rounded accent tile with a three-row "mod list"
glyph (mint dot + white bar), anti-aliased by 4x4 supersampling.
    python tools/make_icon.py [out.ico]
"""
import os
import struct
import sys
import zlib

ACCENT, DOT, BAR = (0x5B, 0x8D, 0xEF), (0xB9, 0xF6, 0xCA), (0xFF, 0xFF, 0xFF)
SIZES = (16, 32, 48, 256)
SS = 4  # supersampling factor


def _shape(x: float, y: float, s: int):
    """Colour of the point (x, y) in an s x s icon, or None outside the tile."""
    r = s / 5
    cx, cy = min(max(x, r), s - r), min(max(y, r), s - r)   # nearest point of the inner rectangle
    if (x - cx) ** 2 + (y - cy) ** 2 > r * r:
        return None
    bar_h = max(1.0, s / 10)
    for i in range(3):
        y0 = s * (27 + i * 17) / 100
        if y0 <= y < y0 + bar_h:
            if s * 0.22 <= x < s * 0.22 + bar_h:
                return DOT
            if s * 0.38 <= x < s * 0.80:
                return BAR
    return ACCENT


def render(s: int) -> bytes:
    """RGBA rows (with PNG filter bytes) for an s x s icon."""
    out = bytearray()
    for py in range(s):
        out.append(0)
        for px in range(s):
            acc, n = [0, 0, 0], 0
            for sy in range(SS):
                for sx in range(SS):
                    c = _shape(px + (sx + 0.5) / SS, py + (sy + 0.5) / SS, s)
                    if c:
                        n += 1
                        for k in range(3):
                            acc[k] += c[k]
            out += bytes((acc[0] // n, acc[1] // n, acc[2] // n, 255 * n // (SS * SS))) if n else b"\0\0\0\0"
    return bytes(out)


def png(s: int) -> bytes:
    try:
        import io
        from PIL import Image
        try:
            from tools.make_store_assets import draw_badge
        except ImportError:
            from make_store_assets import draw_badge
        buf = io.BytesIO()
        draw_badge(max(512, s * 4)).resize((s, s), Image.Resampling.LANCZOS).save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        pass
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", s, s, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(render(s), 9)) + chunk(b"IEND", b""))


def ico(sizes=SIZES) -> bytes:
    images = [png(s) for s in sizes]
    offset = 6 + 16 * len(sizes)
    head = struct.pack("<HHH", 0, 1, len(sizes))
    for s, data in zip(sizes, images):
        head += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    return head + b"".join(images)


def main() -> None:
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "icon.ico")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    data = ico()
    assert data[:4] == b"\0\0\1\0" and data.count(b"\x89PNG") == len(SIZES)
    with open(out, "wb") as f:
        f.write(data)
    print(f"wrote {os.path.abspath(out)} ({len(data):,} bytes)")


if __name__ == "__main__":
    main()
