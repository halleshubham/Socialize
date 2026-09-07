"""Renders a "text card" reel scene - a verbatim quote/fact/stat from the
article, Pillow-drawn (correct Devanagari via graphic_designer/fonts.py,
same as posters) with a small "Source: {platform}" attribution, converted
to a short silent-but-audio-track-present video clip via ffmpeg so it
concatenates cleanly alongside Veo-generated scenes.

This exists specifically because the user asked reels to ground themselves
in real article content, but explicitly asked NOT to source real photos or
page screenshots (article images can be generic/abstract, and screenshot
scraping is more moving parts than it's worth) - recreating the key
fact/quote as accurate on-screen text, with a normal citation, was the
chosen approach. See reel_editor/templates.py for how this fits alongside
the three reel templates as a cross-cutting scene type, not a 4th template.
"""

import io
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

from backend.agents.graphic_designer.fonts import load_font

WIDTH = 1080
HEIGHT = 1920  # matches Veo's 9:16 output - see video_provider.py
FPS = 24  # matches Veo's output fps (confirmed via ffprobe on a real generated clip)
DURATION_SECONDS = 4

_BG_TOP = (17, 17, 23)
_BG_BOTTOM = (32, 28, 48)
_ACCENT = (249, 115, 22, 255)  # same accent as poster_render.py's templates
_WHITE = (255, 255, 255, 255)
_MUTED = (255, 255, 255, 170)


def _wrap_by_width(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.split("\n"):
        words = paragraph.split()
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if draw.textlength(candidate, font=font) <= max_width or not current:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines or [""]


def _fit_font(draw: ImageDraw.ImageDraw, text: str, max_width: int, font_key: str, start_size: int):
    size = start_size
    while size > 28:
        font = load_font(font_key, size, text=text)
        lines = _wrap_by_width(draw, text, font, max_width)
        if len(lines) <= 6 and max(draw.textlength(line, font=font) for line in lines) <= max_width:
            return font, lines
        size -= 4
    font = load_font(font_key, 28, text=text)
    return font, _wrap_by_width(draw, text, font, max_width)


def render_text_card_frame(
    text: str, label: str | None, font_key: str, source_name: str | None
) -> bytes:
    """One PNG frame - vertical gradient background, an optional small
    accent label (e.g. "THE NUMBERS"), the wrapped/centered text, and a
    minor "Source: X" line bottom-right, same attribution convention as
    posters (graphic_designer/graph.py's _platform_name)."""
    img = Image.new("RGB", (WIDTH, HEIGHT), _BG_TOP)
    bottom = Image.new("RGB", (WIDTH, HEIGHT), _BG_BOTTOM)
    mask = Image.new("L", (WIDTH, HEIGHT))
    mask.putdata([int(255 * (y / HEIGHT)) for y in range(HEIGHT) for _ in range(WIDTH)])
    img = Image.composite(bottom, img, mask)
    draw = ImageDraw.Draw(img, "RGBA")

    margin = 90
    max_width = WIDTH - 2 * margin

    text_font, lines = _fit_font(draw, text, max_width, font_key, start_size=64)
    line_height = int(text_font.size * 1.35)
    block_height = line_height * len(lines)
    label_height = 90 if label else 0
    total_height = label_height + block_height
    y = (HEIGHT - total_height) // 2

    if label:
        label_font = load_font(font_key, 32, text=label)
        draw.text((margin, y), label.upper(), font=label_font, fill=_ACCENT)
        y += label_height

    for line in lines:
        line_width = draw.textlength(line, font=text_font)
        draw.text((margin + (max_width - line_width) / 2, y), line, font=text_font, fill=_WHITE)
        y += line_height

    if source_name:
        source_text = f"Source: {source_name}"
        source_font = load_font(font_key, 26, text=source_text)
        sw = draw.textlength(source_text, font=source_font)
        sx, sy = WIDTH - margin - sw, HEIGHT - 130
        draw.rounded_rectangle([(sx - 10, sy - 6), (sx + sw + 10, sy + 34)], radius=6, fill=(0, 0, 0, 130))
        draw.text((sx, sy), source_text, font=source_font, fill=_MUTED)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def render_text_card_clip(text: str, label: str | None, font_key: str, source_name: str | None) -> bytes:
    """PNG frame -> a short MP4 clip (with a silent AAC audio track, so it
    concatenates cleanly with Veo's audio-bearing clips) via ffmpeg."""
    frame_png = render_text_card_frame(text, label, font_key, source_name)
    return _frame_to_clip(frame_png, DURATION_SECONDS)


_TONE_COLORS: dict[str, tuple[int, int, int, int]] = {
    "negative": (239, 68, 68, 255),  # red - debt/loss/before-state
    "positive": (34, 197, 94, 255),  # green - settlement/resolution/after-state
    "neutral": _ACCENT,
}
_BEFORE_PHASE_SECONDS = 2.5
_AFTER_PHASE_SECONDS = 3.5


def _render_stat_phase_frame(
    main_text: str,
    sub_label: str | None,
    color: tuple[int, int, int, int],
    font_key: str,
    highlight_text: str | None,
    source_name: str | None,
) -> bytes:
    """One phase of a before/after stat reveal - a single big number/figure
    in the given accent color, an optional small caption above it, and an
    optional pill-badge callout (e.g. "99.97% HAIRCUT") below - used by
    render_stat_reveal_clip for the "big number changes" moment a plain
    text_card doesn't fit well (a before value and an after value, not one
    static fact)."""
    img = Image.new("RGB", (WIDTH, HEIGHT), _BG_TOP)
    bottom = Image.new("RGB", (WIDTH, HEIGHT), _BG_BOTTOM)
    mask = Image.new("L", (WIDTH, HEIGHT))
    mask.putdata([int(255 * (y / HEIGHT)) for y in range(HEIGHT) for _ in range(WIDTH)])
    img = Image.composite(bottom, img, mask)
    draw = ImageDraw.Draw(img, "RGBA")

    margin = 90
    max_width = WIDTH - 2 * margin

    text_font, lines = _fit_font(draw, main_text, max_width, font_key, start_size=110)
    line_height = int(text_font.size * 1.2)
    block_height = line_height * len(lines)
    label_height = 70 if sub_label else 0
    badge_height = 100 if highlight_text else 0
    total_height = label_height + block_height + badge_height
    y = (HEIGHT - total_height) // 2

    if sub_label:
        label_font = load_font(font_key, 34, text=sub_label)
        lw = draw.textlength(sub_label.upper(), font=label_font)
        draw.text((margin + (max_width - lw) / 2, y), sub_label.upper(), font=label_font, fill=_MUTED)
        y += label_height

    for line in lines:
        line_width = draw.textlength(line, font=text_font)
        draw.text((margin + (max_width - line_width) / 2, y), line, font=text_font, fill=color)
        y += line_height

    if highlight_text:
        y += 30
        badge_font = load_font(font_key, 38, text=highlight_text)
        bw = draw.textlength(highlight_text, font=badge_font)
        bx, by = (WIDTH - bw) / 2, y
        draw.rounded_rectangle(
            [(bx - 26, by - 14), (bx + bw + 26, by + 52)], radius=999, fill=(*color[:3], 60)
        )
        draw.text((bx, by), highlight_text, font=badge_font, fill=_WHITE)

    if source_name:
        source_text = f"Source: {source_name}"
        source_font = load_font(font_key, 26, text=source_text)
        sw = draw.textlength(source_text, font=source_font)
        sx, sy = WIDTH - margin - sw, HEIGHT - 130
        draw.rounded_rectangle([(sx - 10, sy - 6), (sx + sw + 10, sy + 34)], radius=6, fill=(0, 0, 0, 130))
        draw.text((sx, sy), source_text, font=source_font, fill=_MUTED)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def render_stat_reveal_clip(
    before_text: str,
    before_label: str | None,
    after_text: str,
    after_label: str | None,
    font_key: str,
    source_name: str | None,
    highlight_text: str | None = None,
    tone: str = "neutral",
) -> bytes:
    """A two-phase "before value -> after value" reveal (e.g. a debt figure
    cut down to a settlement figure) - a hard cut from a red "before" card to
    a green "after" card (plus an optional highlight badge like a percentage
    change), rather than one static text_card. This exists because a single
    fact/quote text_card doesn't fit a contrast/reveal moment, and because
    Veo itself can't be trusted to render exact figures (same reasoning as
    text_card generally - see this module's docstring), so the before/after
    numbers are Pillow-drawn here instead of described in a Veo prompt.
    Deliberately a hard cut, not an animated counter - simpler to render
    reliably, still reads clearly as a "reveal" at reel pacing."""
    if tone == "neutral":
        before_color = after_color = _TONE_COLORS["neutral"]
    else:
        before_color = _TONE_COLORS["negative"]
        after_color = _TONE_COLORS["positive"]
    before_frame = _render_stat_phase_frame(before_text, before_label, before_color, font_key, None, None)
    after_frame = _render_stat_phase_frame(after_text, after_label, after_color, font_key, highlight_text, source_name)

    before_clip = _frame_to_clip(before_frame, _BEFORE_PHASE_SECONDS)
    after_clip = _frame_to_clip(after_frame, _AFTER_PHASE_SECONDS)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        before_path = tmp_path / "before.mp4"
        after_path = tmp_path / "after.mp4"
        concat_list = tmp_path / "concat.txt"
        output_path = tmp_path / "reveal.mp4"
        before_path.write_bytes(before_clip)
        after_path.write_bytes(after_clip)
        concat_list.write_text(f"file '{before_path}'\nfile '{after_path}'\n")

        subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list), "-c", "copy", str(output_path)],
            check=True,
            capture_output=True,
            timeout=30,
        )
        return output_path.read_bytes()


def _frame_to_clip(frame_png: bytes, duration_seconds: float) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        frame_path = tmp_path / "frame.png"
        output_path = tmp_path / "card.mp4"
        frame_path.write_bytes(frame_png)

        subprocess.run(
            [
                "ffmpeg", "-y",
                "-loop", "1", "-i", str(frame_path),
                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                "-t", str(duration_seconds),
                "-r", str(FPS),
                "-pix_fmt", "yuv420p",
                "-c:v", "libx264", "-c:a", "aac",
                "-shortest",
                str(output_path),
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        return output_path.read_bytes()
