"""One render function per poster_template (see templates.py for the
registry/shapes). All draw over an AI-generated background if one's given,
else a template-tinted gradient. Every function returns raw PNG bytes with
the brand strip already stamped on - graphic_designer/graph.py calls
add_brand_strip separately only for the AI-drew-its-own-text (English quote/
tribute) fast path; these functions always draw their own text, so they
stamp the strip themselves before returning, matching render_fallback_poster.
"""

import io

from PIL import Image, ImageDraw, ImageFont

from backend.agents.graphic_designer.layout import (
    HEIGHT,
    STRIP_HEIGHT,
    WIDTH,
    _fit_font,
    add_brand_strip,
    load_font,
)

_WHITE = (255, 255, 255, 255)
_MUTED_WHITE = (255, 255, 255, 170)
_ACCENT = (249, 115, 22, 255)  # orange-500 - the one accent color used across all templates
STRIP_HEIGHT_ALLOWANCE = STRIP_HEIGHT  # keep template layouts clear of the brand strip


def _wrap_by_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """Pixel-width word wrap - more accurate than character-count wrapping,
    which badly misjudges width for Devanagari (glyph widths vary a lot)."""
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


def _draw_block(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    x: int,
    y: int,
    max_width: int,
    line_height: int,
    fill: tuple,
    align: str = "left",
) -> int:
    """Draws wrapped text starting at (x, y), returns the y position after it."""
    for line in _wrap_by_width(draw, text, font, max_width):
        line_width = draw.textlength(line, font=font)
        if align == "center":
            lx = x + (max_width - line_width) / 2
        elif align == "right":
            lx = x + max_width - line_width
        else:
            lx = x
        draw.text((lx, y), line, font=font, fill=fill)
        y += line_height
    return y


def _block_height(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int, line_height: int) -> int:
    return line_height * len(_wrap_by_width(draw, text, font, max_width))


def _centered_top(total_height: int, top_margin: int = 90, bottom_margin: int | None = None) -> int:
    """Starting y so a block of the given total height sits vertically
    centered in the space above the brand strip, never crossing top_margin."""
    bottom_margin = STRIP_HEIGHT_ALLOWANCE + 40 if bottom_margin is None else bottom_margin
    available = HEIGHT - top_margin - bottom_margin
    return top_margin + max(0, (available - total_height) // 2)


def _base_image(background_bytes: bytes | None, tint_top: tuple, tint_bottom: tuple) -> Image.Image:
    if background_bytes:
        return Image.open(io.BytesIO(background_bytes)).convert("RGB").resize((WIDTH, HEIGHT))
    base = Image.new("RGB", (WIDTH, HEIGHT), tint_top)
    bottom = Image.new("RGB", (WIDTH, HEIGHT), tint_bottom)
    mask = Image.new("L", (WIDTH, HEIGHT))
    mask.putdata([int(255 * (y / HEIGHT)) for y in range(HEIGHT) for _ in range(WIDTH)])
    return Image.composite(bottom, base, mask)


def _finish(img: Image.Image, font_key: str, brand_name: str, social_handles: dict | None, website_url: str | None, logo_bytes: bytes | None = None) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return add_brand_strip(buf.getvalue(), font_key, brand_name, social_handles, website_url, logo_bytes)


def render_quote(content: dict, headline_fallback: str, font_key: str, background_bytes: bytes | None, brand_name: str, social_handles: dict | None, website_url: str | None, logo_bytes: bytes | None = None) -> bytes:
    quote_text = content.get("quote_text") or headline_fallback
    attribution = content.get("attribution", "")
    citation = content.get("citation", "")

    img = _base_image(background_bytes, (20, 20, 28), (45, 35, 60))
    draw = ImageDraw.Draw(img, "RGBA")
    margin = 90

    # Big decorative quote mark.
    mark_font = load_font(font_key, 160, text="“")
    draw.text((margin - 20, 40), "“", font=mark_font, fill=(255, 255, 255, 60))

    q_font = _fit_font(draw, _wrap_by_width(draw, quote_text, load_font(font_key, 60, text=quote_text), WIDTH - 2 * margin), WIDTH - 2 * margin, font_key, start_size=60)
    line_height = int(q_font.size * 1.3)
    lines = _wrap_by_width(draw, quote_text, q_font, WIDTH - 2 * margin)
    block_height = line_height * len(lines) + (60 if attribution else 0) + (36 if citation else 0)
    y = (HEIGHT - STRIP_HEIGHT_ALLOWANCE - block_height) // 2
    y = _draw_block(draw, quote_text, q_font, margin, y, WIDTH - 2 * margin, line_height, _WHITE)

    if attribution:
        y += 20
        attr_font = load_font(font_key, 34, text=attribution)
        y = _draw_block(draw, f"— {attribution}", attr_font, margin, y, WIDTH - 2 * margin, 44, _ACCENT)
    if citation:
        cite_font = load_font(font_key, 24, text=citation)
        _draw_block(draw, citation, cite_font, margin, y + 4, WIDTH - 2 * margin, 30, _MUTED_WHITE)

    return _finish(img, font_key, brand_name, social_handles, website_url, logo_bytes)


def render_tribute(content: dict, headline_fallback: str, font_key: str, background_bytes: bytes | None, brand_name: str, social_handles: dict | None, website_url: str | None, logo_bytes: bytes | None = None) -> bytes:
    name = content.get("name") or headline_fallback
    achievement = content.get("achievement", "")
    quote_text = content.get("quote_text", "")
    tribute_line = content.get("tribute_line", "")

    img = _base_image(background_bytes, (10, 15, 20), (30, 45, 40))
    draw = ImageDraw.Draw(img, "RGBA")
    margin = 90
    max_width = WIDTH - 2 * margin

    # (font, text, line_height, gap_before, fill) - drawn in order, each
    # preceded by its gap. Measured first so the whole block can be
    # vertically centered rather than always starting at a fixed y.
    blocks: list[tuple] = []
    if quote_text:
        q_font = _fit_font(draw, _wrap_by_width(draw, quote_text, load_font(font_key, 48, text=quote_text), max_width), max_width, font_key, start_size=48)
        blocks.append((q_font, quote_text, int(q_font.size * 1.3), 0, _WHITE))
    name_font = _fit_font(draw, [name], max_width, font_key, start_size=68)
    blocks.append((name_font, name, int(name_font.size * 1.2), 50 if quote_text else 0, _ACCENT))
    if achievement:
        ach_font = load_font(font_key, 32, text=achievement)
        blocks.append((ach_font, achievement, 42, 14, _WHITE))
    if tribute_line:
        tl_font = load_font(font_key, 28, text=tribute_line)
        blocks.append((tl_font, tribute_line, 38, 24, _MUTED_WHITE))

    total_height = sum(gap + _block_height(draw, text, font, max_width, lh) for font, text, lh, gap, _ in blocks)
    y = _centered_top(total_height)
    for font, text, lh, gap, fill in blocks:
        y += gap
        y = _draw_block(draw, text, font, margin, y, max_width, lh, fill)

    return _finish(img, font_key, brand_name, social_handles, website_url, logo_bytes)


def render_narrative(content: dict, headline_fallback: str, font_key: str, background_bytes: bytes | None, brand_name: str, social_handles: dict | None, website_url: str | None, logo_bytes: bytes | None = None) -> bytes:
    body_text = content.get("body_text") or headline_fallback

    img = _base_image(background_bytes, (18, 18, 20), (35, 30, 28))
    draw = ImageDraw.Draw(img, "RGBA")
    margin = 80

    body_font = load_font(font_key, 34, text=body_text)
    line_height = int(body_font.size * 1.45)
    lines = _wrap_by_width(draw, body_text, body_font, WIDTH - 2 * margin)
    # Cap at what fits above the brand strip; a poster isn't the whole article.
    max_lines = (HEIGHT - STRIP_HEIGHT_ALLOWANCE - 140) // line_height
    truncated = len(lines) > max_lines
    lines = lines[:max_lines]
    if truncated and lines:
        lines[-1] = lines[-1].rstrip() + "…"

    block_height = line_height * len(lines)
    panel_top = _centered_top(block_height + 80, top_margin=70)
    panel_bottom = panel_top + block_height + 80
    draw.rectangle([(0, panel_top), (WIDTH, panel_bottom)], fill=(0, 0, 0, 140))
    y = panel_top + 40
    for line in lines:
        draw.text((margin, y), line, font=body_font, fill=_WHITE)
        y += line_height

    return _finish(img, font_key, brand_name, social_handles, website_url, logo_bytes)


def render_fact_critique(content: dict, headline_fallback: str, font_key: str, background_bytes: bytes | None, brand_name: str, social_handles: dict | None, website_url: str | None, logo_bytes: bytes | None = None) -> bytes:
    facts = content.get("facts") or []
    critique_line = content.get("critique_line") or headline_fallback

    img = _base_image(background_bytes, (12, 12, 14), (28, 20, 20))
    draw = ImageDraw.Draw(img, "RGBA")
    margin = 70
    max_width = WIDTH - 2 * margin
    stat_font_size = 30

    # Pre-measure each fact box (font, wrapped lines, box_height) so the
    # whole stack (boxes + critique line) can be vertically centered.
    fact_boxes = []
    for fact in facts[:3]:
        stat = str(fact.get("stat", ""))
        source = str(fact.get("source", ""))
        if not stat:
            continue
        stat_font = load_font(font_key, stat_font_size, text=stat)
        lines = _wrap_by_width(draw, stat, stat_font, max_width - 40)
        line_height = int(stat_font.size * 1.3)
        box_height = line_height * len(lines) + (34 if source else 0) + 36
        fact_boxes.append((stat_font, lines, line_height, source, box_height))

    critique_font = _fit_font(draw, _wrap_by_width(draw, critique_line, load_font(font_key, 40, text=critique_line), max_width), max_width, font_key, start_size=40)
    critique_line_height = int(critique_font.size * 1.3)
    critique_height = 20 + _block_height(draw, critique_line, critique_font, max_width, critique_line_height)

    total_height = sum(box_height + 24 for *_, box_height in fact_boxes) + critique_height
    y = _centered_top(total_height, top_margin=80)

    for stat_font, lines, line_height, source, box_height in fact_boxes:
        draw.rectangle([(margin, y), (WIDTH - margin, y + box_height)], fill=(249, 115, 22, 35), outline=_ACCENT, width=2)
        ty = y + 18
        for line in lines:
            draw.text((margin + 24, ty), line, font=stat_font, fill=_WHITE)
            ty += line_height
        if source:
            src_font = load_font(font_key, 22, text=source)
            draw.text((margin + 24, ty), source, font=src_font, fill=_ACCENT)
        y += box_height + 24

    _draw_block(draw, critique_line, critique_font, margin, y + 20, max_width, critique_line_height, _WHITE)

    return _finish(img, font_key, brand_name, social_handles, website_url, logo_bytes)


def render_trivia(content: dict, headline_fallback: str, font_key: str, background_bytes: bytes | None, brand_name: str, social_handles: dict | None, website_url: str | None, logo_bytes: bytes | None = None) -> bytes:
    headline = content.get("headline") or "Did you know?"
    body_text = content.get("body_text") or headline_fallback
    highlights = content.get("highlight_phrases") or []

    img = _base_image(background_bytes, (25, 18, 35), (55, 40, 30))
    draw = ImageDraw.Draw(img, "RGBA")
    margin = 80
    max_width = WIDTH - 2 * margin

    body_font = load_font(font_key, 40, text=body_text)
    body_line_height = int(body_font.size * 1.35)
    body_height = _block_height(draw, body_text, body_font, max_width, body_line_height)

    # Badge (110) + body text + optional chip row(s) (64 per row, wrapping
    # the same way the draw pass below does) - measured up front to center
    # the whole stack.
    total_height = 110 + body_height
    if highlights:
        chip_font = load_font(font_key, 28, text=" ".join(highlights))
        chip_rows = 1
        x = margin
        for phrase in highlights[:2]:
            w = draw.textlength(phrase, font=chip_font)
            if x + w + 40 > WIDTH - margin:
                chip_rows += 1
                x = margin
            x += w + 32 + 16
        total_height += 30 + chip_rows * 64

    y = _centered_top(total_height)

    label_font = load_font(font_key, 30, text=headline)
    label_w = draw.textlength(headline, font=label_font)
    draw.rounded_rectangle([(margin, y), (margin + label_w + 40, y + 60)], radius=30, fill=_ACCENT)
    draw.text((margin + 20, y + 12), headline, font=label_font, fill=(20, 20, 20, 255))
    y += 110

    y = _draw_block(draw, body_text, body_font, margin, y, max_width, body_line_height, _WHITE)

    if highlights:
        y += 30
        x = margin
        for phrase in highlights[:2]:
            w = draw.textlength(phrase, font=chip_font)
            if x + w + 40 > WIDTH - margin:
                x = margin
                y += 64
            draw.rounded_rectangle([(x, y), (x + w + 32, y + 52)], radius=26, fill=(*_ACCENT[:3], 220))
            draw.text((x + 16, y + 10), phrase, font=chip_font, fill=(20, 20, 20, 255))
            x += w + 32 + 16

    return _finish(img, font_key, brand_name, social_handles, website_url, logo_bytes)


def render_event(content: dict, headline_fallback: str, font_key: str, background_bytes: bytes | None, brand_name: str, social_handles: dict | None, website_url: str | None, logo_bytes: bytes | None = None) -> bytes:
    title = content.get("event_title") or headline_fallback
    datetime_str = content.get("datetime", "")
    location = content.get("location", "")
    cta = content.get("cta", "")

    img = _base_image(background_bytes, (15, 25, 20), (10, 40, 45))
    draw = ImageDraw.Draw(img, "RGBA")
    margin = 80
    max_width = WIDTH - 2 * margin

    title_font = _fit_font(draw, _wrap_by_width(draw, title, load_font(font_key, 68, text=title), max_width), max_width, font_key, start_size=68)
    title_line_height = int(title_font.size * 1.25)
    detail_font = load_font(font_key, 32, text=f"{datetime_str} {location}")

    total_height = 40 + _block_height(draw, title, title_font, max_width, title_line_height)
    if datetime_str:
        total_height += _block_height(draw, datetime_str, detail_font, max_width, 44)
    if location:
        total_height += _block_height(draw, location, detail_font, max_width, 44)
    if cta:
        total_height += 30 + 70

    y = _centered_top(total_height, top_margin=100)

    y = _draw_block(draw, title, title_font, margin, y, max_width, title_line_height, _WHITE)
    y += 40

    if datetime_str:
        y = _draw_block(draw, datetime_str, detail_font, margin, y, max_width, 44, _ACCENT)
    if location:
        y = _draw_block(draw, location, detail_font, margin, y, max_width, 44, _MUTED_WHITE)

    if cta:
        y += 30
        cta_font = load_font(font_key, 32, text=cta)
        cta_w = draw.textlength(cta, font=cta_font)
        draw.rounded_rectangle([(margin, y), (margin + cta_w + 60, y + 70)], radius=10, fill=_ACCENT)
        draw.text((margin + 30, y + 18), cta, font=cta_font, fill=(20, 20, 20, 255))

    return _finish(img, font_key, brand_name, social_handles, website_url, logo_bytes)


RENDERERS = {
    "quote": render_quote,
    "tribute": render_tribute,
    "narrative": render_narrative,
    "fact_critique": render_fact_critique,
    "trivia": render_trivia,
    "event": render_event,
}
