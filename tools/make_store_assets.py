"""Generate clean, non-AI graphic design icon (512x512) and Itch.io cover (630x500)."""
from __future__ import annotations

import math
import os
from PIL import Image, ImageDraw, ImageFont


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        ["C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/calibrib.ttf"]
        if bold else
        ["C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/calibri.ttf"]
    )
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return ImageFont.load_default()


def _star_points(cx: float, cy: float, r_outer: float, r_inner: float, points: int = 5) -> list[tuple[float, float]]:
    pts = []
    for i in range(points * 2):
        angle = -math.pi / 2 + i * math.pi / points
        r = r_outer if i % 2 == 0 else r_inner
        pts.append((cx + math.cos(angle) * r, cy + math.sin(angle) * r))
    return pts


def draw_badge(size: int = 1024) -> Image.Image:
    """Render high-res square icon at `size` (downsample with LANCZOS for crisp anti-aliasing)."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    pad = int(size * 0.04)
    rad = int(size * 0.20)

    # Outer dark slate tile + subtle border
    d.rounded_rectangle(
        [pad, pad, size - pad, size - pad],
        radius=rad,
        fill=(18, 22, 31, 255),
        outline=(58, 74, 104, 255),
        width=max(2, int(size * 0.016)),
    )

    # Inner recessed panel
    in_pad = int(size * 0.085)
    in_rad = int(size * 0.155)
    d.rounded_rectangle(
        [in_pad, in_pad, size - in_pad, size - in_pad],
        radius=in_rad,
        fill=(24, 30, 44, 255),
        outline=(40, 52, 75, 255),
        width=max(1, int(size * 0.008)),
    )

    # Top accent bar glow strip
    strip_w = int(size * 0.42)
    strip_h = int(size * 0.018)
    d.rounded_rectangle(
        [(size - strip_w) // 2, in_pad - strip_h // 2, (size + strip_w) // 2, in_pad + strip_h // 2],
        radius=strip_h // 2,
        fill=(91, 141, 239, 255),
    )

    # Three mod load-order cards inside the badge
    rows = [
        # (y_frac, dot_color, bar_fill, border_color, width_frac)
        (0.24, (74, 222, 128, 255), (42, 56, 82, 255), (75, 110, 165, 255), 0.72),
        (0.44, (91, 141, 239, 255), (42, 56, 82, 255), (91, 141, 239, 255), 0.62),
        (0.64, (245, 185, 66, 255), (42, 56, 82, 255), (75, 100, 145, 255), 0.50),
    ]
    card_x0 = int(size * 0.16)
    card_h = int(size * 0.135)
    card_r = int(size * 0.035)

    for y_frac, dot_col, card_fill, border_col, w_frac in rows:
        y0 = int(size * y_frac)
        y1 = y0 + card_h
        x1 = int(size * (0.16 + w_frac * 0.90))
        d.rounded_rectangle(
            [card_x0, y0, x1, y1],
            radius=card_r,
            fill=card_fill,
            outline=border_col,
            width=max(2, int(size * 0.008)),
        )
        # Status dot
        dot_r = int(card_h * 0.24)
        dot_cx = card_x0 + int(card_h * 0.52)
        dot_cy = (y0 + y1) // 2
        d.ellipse([dot_cx - dot_r, dot_cy - dot_r, dot_cx + dot_r, dot_cy + dot_r], fill=dot_col)

        # Text bar inside row
        bar_x0 = dot_cx + dot_r + int(size * 0.035)
        bar_x1 = x1 - int(size * 0.045)
        bar_h = int(card_h * 0.22)
        if bar_x1 > bar_x0 + bar_h:
            d.rounded_rectangle(
                [bar_x0, dot_cy - bar_h // 2, bar_x1, dot_cy + bar_h // 2],
                radius=bar_h // 2,
                fill=(228, 235, 248, 255),
            )

    # Bottom-right RimWorld-inspired colony star medallion
    med_cx = int(size * 0.73)
    med_cy = int(size * 0.69)
    med_r = int(size * 0.175)
    d.ellipse(
        [med_cx - med_r, med_cy - med_r, med_cx + med_r, med_cy + med_r],
        fill=(18, 23, 34, 255),
        outline=(234, 179, 8, 255),
        width=max(2, int(size * 0.015)),
    )
    inner_r = int(med_r * 0.82)
    d.ellipse(
        [med_cx - inner_r, med_cy - inner_r, med_cx + inner_r, med_cy + inner_r],
        fill=(29, 38, 56, 255),
    )
    star = _star_points(med_cx, med_cy, med_r * 0.58, med_r * 0.25, 5)
    d.polygon(star, fill=(245, 194, 66, 255))

    return img


def make_itch_cover(icon_hi: Image.Image) -> Image.Image:
    """Render 630x500 Itch.io cover image (at 2x 1260x1000 then downsample)."""
    W, H = 1260, 1000
    img = Image.new("RGBA", (W, H), (14, 17, 24, 255))
    d = ImageDraw.Draw(img)

    # Subtle grid lines for a technical desktop utility feel
    grid_col = (22, 27, 38, 255)
    for gx in range(0, W, 60):
        d.line([(gx, 0), (gx, H)], fill=grid_col, width=2)
    for gy in range(0, H, 60):
        d.line([(0, gy), (W, gy)], fill=grid_col, width=2)

    # Outer frame border
    d.rounded_rectangle(
        [24, 24, W - 24, H - 24],
        radius=28,
        outline=(48, 62, 88, 255),
        width=4,
    )

    # Top accent pill ("WINDOWS DESKTOP UTILITY · RIMWORLD 1.4 / 1.5 / 1.6")
    pill_font = _load_font(22, bold=True)
    pill_text = "RIMWORLD 1.4 – 1.6  ·  STANDALONE .EXE"
    d.rounded_rectangle([72, 68, 555, 114], radius=12, fill=(26, 36, 56, 255), outline=(72, 108, 178, 255), width=2)
    d.text((94, 78), pill_text, font=pill_font, fill=(142, 184, 255, 255))

    # Place badge on right-top / center-left layout
    badge_size = 340
    badge = icon_hi.resize((badge_size, badge_size), Image.Resampling.LANCZOS)
    img.alpha_composite(badge, (72, 150))

    # Title & subtitle next to badge
    title_font = _load_font(68, bold=True)
    sub_font = _load_font(30, bold=False)
    d.text((444, 175), "RimWorld", font=title_font, fill=(242, 246, 252, 255))
    d.text((444, 255), "Mod Manager", font=title_font, fill=(91, 141, 239, 255))
    d.text((446, 352), "Fast topological load-order sorter,", font=sub_font, fill=(168, 180, 201, 255))
    d.text((446, 394), "SteamCMD downloader & modlist builder.", font=sub_font, fill=(168, 180, 201, 255))

    # Bottom feature cards row
    cards = [
        ("Auto-Sort & Rules", "Community load-order DB\n+ instant cycle diagnostics", (74, 222, 128, 255)),
        ("Built-in SteamCMD", "Download & update mods\nwithout Steam running", (91, 141, 239, 255)),
        ("Modlist Profiles", "Diffs, snapshots, RimPy /\nRimSort import & export", (245, 185, 66, 255)),
    ]
    cw = 356
    ch = 210
    cy0 = 540
    c_head_font = _load_font(28, bold=True)
    c_body_font = _load_font(23, bold=False)

    for idx, (head, body, accent) in enumerate(cards):
        cx0 = 72 + idx * (cw + 24)
        d.rounded_rectangle(
            [cx0, cy0, cx0 + cw, cy0 + ch],
            radius=18,
            fill=(22, 28, 40, 255),
            outline=(46, 58, 82, 255),
            width=3,
        )
        # Left accent bar
        d.rounded_rectangle(
            [cx0 + 18, cy0 + 26, cx0 + 26, cy0 + 62],
            radius=4,
            fill=accent,
        )
        d.text((cx0 + 42, cy0 + 26), head, font=c_head_font, fill=(238, 242, 250, 255))
        for b_idx, line in enumerate(body.split("\n")):
            d.text((cx0 + 24, cy0 + 92 + b_idx * 34), line, font=c_body_font, fill=(154, 168, 192, 255))

    # Bottom status strip
    foot_y = 805
    d.rounded_rectangle(
        [72, foot_y, W - 72, foot_y + 115],
        radius=16,
        fill=(19, 24, 34, 255),
        outline=(40, 52, 74, 255),
        width=2,
    )
    f_bold = _load_font(25, bold=True)
    f_reg = _load_font(23, bold=False)
    d.ellipse([104, foot_y + 44, 128, foot_y + 68], fill=(74, 222, 128, 255))
    d.text((146, foot_y + 26), "No installer · No account · Single portable executable", font=f_bold, fill=(230, 236, 246, 255))
    d.text((146, foot_y + 62), "Reads your existing RimWorld + Steam Workshop folders automatically", font=f_reg, fill=(142, 156, 180, 255))

    return img.resize((630, 500), Image.Resampling.LANCZOS)


def main() -> None:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    dist_dir = os.path.join(root, "dist")
    os.makedirs(dist_dir, exist_ok=True)

    hi_badge = draw_badge(1024)
    icon_512 = hi_badge.resize((512, 512), Image.Resampling.LANCZOS)
    icon_path = os.path.join(dist_dir, "icon_512x512.png")
    icon_512.save(icon_path, "PNG")

    cover = make_itch_cover(hi_badge)
    cover_path = os.path.join(dist_dir, "itch_cover_630x500.png")
    cover.save(cover_path, "PNG")

    print(f"wrote {icon_path}")
    print(f"wrote {cover_path}")


if __name__ == "__main__":
    main()
