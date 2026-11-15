"""Draws the app icon into client/quest/res/mipmap-*/ (ic_launcher.png, ic_launcher_round.png) at the Android launcher
densities. The mark: two parallel gripper jaws in the app's ink colour closing on a violet point, inside a violet
ring, on the app's dark background (colours from client/core/ui.h). Needs Pillow.

    python tools/make_icon.py
"""
import os

from PIL import Image, ImageDraw

VOID = (6, 7, 9, 255)            # kVoid  #060709
SURFACE = (12, 13, 17, 255)      # kSurface
INK = (243, 244, 246, 255)       # kInk   #f3f4f6
ACCENT = (141, 144, 255, 255)    # kAccent #8d90ff
SIZES = {"mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192}
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "client", "quest", "res")


def draw(n: int = 1024, round_shape: bool = False) -> Image.Image:
    im = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    if round_shape:
        d.ellipse([0, 0, n - 1, n - 1], fill=VOID)
    else:
        d.rounded_rectangle([0, 0, n - 1, n - 1], radius=int(0.18 * n), fill=VOID)
    c, r = n / 2, 0.34 * n
    d.ellipse([c - r, c - r, c + r, c + r], outline=ACCENT, width=int(0.045 * n))          # the ring
    jw, jh, gap = 0.075 * n, 0.30 * n, 0.085 * n                                          # the two jaws
    for s in (-1, 1):
        x0 = c + s * gap - (jw if s < 0 else 0)
        d.rounded_rectangle([x0, c - jh / 2, x0 + jw, c + jh / 2], radius=int(0.02 * n), fill=INK)
    p = 0.045 * n                                                                          # what they hold
    d.ellipse([c - p, c - p, c + p, c + p], fill=ACCENT)
    return im


def main():
    big, big_round = draw(), draw(round_shape=True)
    for dens, px in SIZES.items():
        out = os.path.join(ROOT, f"mipmap-{dens}")
        os.makedirs(out, exist_ok=True)
        big.resize((px, px), Image.LANCZOS).save(os.path.join(out, "ic_launcher.png"), optimize=True)
        big_round.resize((px, px), Image.LANCZOS).save(os.path.join(out, "ic_launcher_round.png"), optimize=True)
    big.resize((512, 512), Image.LANCZOS).save(os.path.join(ROOT, "..", "..", "..", "docs", "img", "icon.png"), optimize=True)
    print(f"icons written to {os.path.normpath(ROOT)} and docs/img/icon.png")


if __name__ == "__main__":
    main()
