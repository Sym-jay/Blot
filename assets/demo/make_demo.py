"""Render the README / launch-post demo GIF: Blot catching a real Flask regression.

Run:  pip install pillow && python assets/demo/make_demo.py [path/to/monospace.ttf]
Out:  assets/demo/demo.gif

Every line of tool output below was captured from a real run on pallets/flask (d086db85)
with Blot's pre-commit hook installed. Only the typing animation is synthetic.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).parent
W, H = 1080, 580
PAD, LINE_H, FONT_SIZE = 28, 26, 18
VISIBLE_LINES = 20
COLORS = {
    "bg": "#1F1B16", "bar": "#2A251E", "text": "#EDE3D3", "dim": "#8C8273", "prompt": "#A9BC8E",
    "warn": "#E39A5B", "minus": "#E07A6A", "plus": "#9FCB8A", "sha": "#F4E2AE",
}

# (kind, text): "cmd" lines are typed, everything else appears at once.
SCRIPT: list[tuple[str, str]] = [
    ("cmd", "pip install blot-git"),
    ("dim", "Successfully installed blot-git-0.1.0"),
    ("cmd", "blot index"),
    ("text", "blot: indexed 3829 commits (103 load-bearing fixes), full, in 2.0s"),
    ("cmd", "blot install-hook"),
    ("text", "blot: pre-commit hook installed (.git/hooks/pre-commit)"),
    ("note", "# that weakref looks over-complicated... let's simplify it"),
    ("cmd", "git diff -U0"),
    ("dim", "@@ -355 +355 @@ class Flask(App):"),
    ("minus", "-            self_ref = weakref.ref(self)"),
    ("plus", "+            self_ref = self"),
    ("cmd", 'git commit -am "Simplify static route"'),
    ("warn", "blot: 1 warning"),
    ("text", ""),
    ("warn", "  ! removed-fix  src/flask/app.py:355"),
    ("text", "    You're removing or changing a line that a bug fix added."),
    ("text", "    Check that the bug it fixed can't come back."),
    ("sha", '    8efea0c "Break reference cycle created by default in Flask instances." (2020-10-03)'),
    ("dim", '    traced past refactor 0ec7f71 "Split the App and Blueprint into Sansio and IO parts"'),
]
SLEEPY_UNTIL = 3   # Sora dozes while the index builds
ALERT_FROM = 12    # Sora jumps to alert when the warning prints


def _sora():
    spec = importlib.util.spec_from_file_location("build_sprites", HERE.parent / "sora" / "build_sprites.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _font(path: str | None) -> ImageFont.FreeTypeFont:
    candidates = [path] if path else []
    candidates += ["C:/Windows/Fonts/consola.ttf", "/System/Library/Fonts/Menlo.ttc",
                   "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"]
    for c in candidates:
        if c and os.path.exists(c):
            return ImageFont.truetype(c, FONT_SIZE)
    sys.exit("no monospace font found; pass a .ttf path as the first argument")


def _draw_sora(draw: ImageDraw.ImageDraw, sora, face: str, x: int, y: int, scale: int = 5) -> None:
    palette = {**sora.PALETTES["orange"], **sora.FIXED}
    cells = {}
    for row_i, row in enumerate(sora.BODY):
        for col_i, key in enumerate(row):
            if key not in ".S":
                cells[(col_i, row_i + sora.TOP)] = key
    for col_i, row_i, key in sora.FACES[face]:
        cells[(col_i, row_i + sora.TOP)] = key
    for (cx, cy), key in cells.items():
        draw.rectangle([x + cx * scale, y + cy * scale, x + cx * scale + scale - 1, y + cy * scale + scale - 1],
                       fill=palette[key])


def _frame(lines: list[tuple[str, str]], cursor: bool, face: str, font, sora) -> Image.Image:
    img = Image.new("RGB", (W, H), COLORS["bg"])
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 34], fill=COLORS["bar"])
    for i, c in enumerate(("#E07A6A", "#E8A33B", "#9FCB8A")):
        d.ellipse([16 + i * 22, 11, 28 + i * 22, 23], fill=c)
    d.text((W // 2, 17), "blot demo: pallets/flask", font=font, fill=COLORS["dim"], anchor="mm")
    shown = lines[-VISIBLE_LINES:]
    for i, (kind, text) in enumerate(shown):
        y = 50 + i * LINE_H
        if kind == "cmd":
            d.text((PAD, y), "$", font=font, fill=COLORS["prompt"])
            d.text((PAD + 20, y), text, font=font, fill=COLORS["text"])
        else:
            color = COLORS.get(kind, COLORS["text"]) if kind != "note" else COLORS["dim"]
            d.text((PAD, y), text, font=font, fill=color)
    if cursor:
        last_kind, last_text = shown[-1] if shown else ("cmd", "")
        offset = 20 if last_kind == "cmd" else 0
        cx = PAD + offset + int(font.getlength(last_text))
        cy = 50 + (len(shown) - 1) * LINE_H
        d.rectangle([cx + 2, cy + 2, cx + 11, cy + 20], fill=COLORS["text"])
    _draw_sora(d, sora, face, W - 28 * 5 - 28, 40)  # top right: the early lines there are short
    return img


def main() -> None:
    font = _font(sys.argv[1] if len(sys.argv) > 1 else None)
    sora = _sora()
    frames: list[Image.Image] = []
    durations: list[int] = []
    shown: list[tuple[str, str]] = []

    def add(face: str, ms: int, cursor: bool = True) -> None:
        frames.append(_frame(shown, cursor, face, font, sora))
        durations.append(ms)

    for step, (kind, text) in enumerate(SCRIPT):
        face = "sleepy" if step < SLEEPY_UNTIL else ("alert" if step >= ALERT_FROM else "idle")
        if kind == "cmd":
            shown.append(("cmd", ""))
            for n in range(0, len(text) + 1, 3):
                shown[-1] = ("cmd", text[:n])
                add(face, 45)
            shown[-1] = ("cmd", text)
            add(face, 450)
        else:
            shown.append((kind, text))
            add(face, 1100 if step == len(SCRIPT) - 1 else 220, cursor=False)
    add("alert", 4500, cursor=False)

    out = HERE / "demo.gif"
    frames[0].save(out, save_all=True, append_images=frames[1:], duration=durations, loop=0, optimize=True)
    print(f"wrote {out} ({len(frames)} frames, {out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
