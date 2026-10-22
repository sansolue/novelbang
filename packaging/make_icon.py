"""Draws packaging/icon.png (1024×1024): a cream open book on NovelBang's terracotta.

    uv run --group build python packaging/make_icon.py
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

SIZE = 1024
ACCENT = (154, 79, 42)  # --accent in app.css
ACCENT_DARK = (118, 56, 28)
PAPER = (253, 251, 247)  # --paper
SHADE = (232, 222, 206)
INK = (154, 79, 42, 150)


def main() -> None:
    s = 4  # draw large, then downsample for smooth edges
    W = SIZE * s
    img = Image.new("RGBA", (W, W), (0, 0, 0, 0))

    # macOS icon grid: an 824 px rounded square centred on the 1024 canvas, with a soft shadow.
    inset, radius = 100 * s, 185 * s
    shadow = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        (inset, inset + 12 * s, W - inset, W - inset + 12 * s), radius, fill=(0, 0, 0, 90)
    )
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(18 * s)))
    tile = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    grad = Image.linear_gradient("L").resize((W, W))
    body = Image.composite(Image.new("RGBA", (W, W), ACCENT_DARK + (255,)), Image.new("RGBA", (W, W), ACCENT + (255,)), grad)
    mask = Image.new("L", (W, W), 0)
    ImageDraw.Draw(mask).rounded_rectangle((inset, inset, W - inset, W - inset), radius, fill=255)
    tile.paste(body, (0, 0), mask)
    img.alpha_composite(tile)

    d = ImageDraw.Draw(img)
    cx, top, bottom = W // 2, 330 * s, 700 * s
    spread, sag = 300 * s, 40 * s

    def page(side: int, fill) -> list[tuple[int, int]]:
        outer = cx + side * spread
        pts = [(cx, top + sag), (cx + side * spread // 2, top - 10 * s), (outer, top + 10 * s),
               (outer, bottom), (cx + side * spread // 2, bottom - 30 * s), (cx, bottom + sag)]
        d.polygon(pts, fill=fill)
        return pts

    # page edges under the open pages
    for side in (-1, 1):
        outer = cx + side * (spread + 14 * s)
        d.polygon([(cx, bottom + sag + 26 * s), (outer, bottom + 22 * s), (outer, top + 30 * s),
                   (cx, top + sag + 30 * s)], fill=SHADE)
    page(-1, PAPER)
    page(1, PAPER)
    d.line([(cx, top + sag), (cx, bottom + sag)], fill=SHADE, width=10 * s)

    # lines of text on each page
    lines = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    ld = ImageDraw.Draw(lines)
    for side in (-1, 1):
        for i in range(6):
            y = top + 70 * s + i * 52 * s
            near, far = cx + side * 50 * s, cx + side * (spread - 50 * s - (90 * s if i == 5 else 0))
            x0, x1 = sorted((near, far))
            ld.rounded_rectangle((x0, y, x1, y + 14 * s), 7 * s, fill=INK)
    img.alpha_composite(lines)

    out = Path(__file__).with_name("icon.png")
    img.resize((SIZE, SIZE), Image.LANCZOS).save(out)
    print(out)


if __name__ == "__main__":
    main()
