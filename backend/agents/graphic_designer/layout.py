"""Pillow/deterministic code never draws poster CONTENT text (headline,
quote, stat, etc.) - that's either AI-generated (the image model bakes the
headline into the image itself, verbatim per the prompt) or not shown at
all (no provider configured, or the generation call failed). The only
things this module ever stamps on are branding (name + logo,
add_brand_strip) and source attribution (add_source_line) - both
independently optional per BrandKit toggles, since they're factual data
that must render exactly as saved regardless of what the image model did.

- render_fallback_poster: background (AI-generated if one was produced,
  else a neutral gradient) + brand strip. No headline.
- add_brand_strip: the bottom brand strip - name/logo/handles/website.
- add_source_line: a small "Source: X" line, applied last regardless of
  which path produced the image.
"""

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from backend.agents.graphic_designer.fonts import load_font

WIDTH = 1080
HEIGHT = 1080
STRIP_HEIGHT = 90

_FONTS_DIR = Path("backend/app/static/fonts")
_FALLBACK_TOP = (15, 23, 42)  # slate-900
_FALLBACK_BOTTOM = (51, 65, 85)  # slate-700 - neutral, brand kit carries no colors

# Font Awesome 6 Free (icons CC BY 4.0, font SIL OFL) - vendored under
# static/fonts/. Codepoints confirmed by inspecting the font's cmap
# directly (fontTools) and rendering each, not guessed from docs.
_ICON_FONT_FILES = {"brands": "fa-brands-400.ttf", "solid": "fa-solid-900.ttf"}
PLATFORM_ICONS = {
    "instagram": ("brands", ""),
    "facebook": ("brands", ""),
    "x": ("brands", ""),
    "youtube": ("brands", ""),
    "tiktok": ("brands", ""),
}
WEBSITE_ICON = ("solid", "")  # globe


def _load_icon_font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(_FONTS_DIR / _ICON_FONT_FILES[kind]), size)


def _neutral_gradient() -> Image.Image:
    base = Image.new("RGB", (WIDTH, HEIGHT), _FALLBACK_TOP)
    bottom = Image.new("RGB", (WIDTH, HEIGHT), _FALLBACK_BOTTOM)
    mask = Image.new("L", (WIDTH, HEIGHT))
    mask.putdata([int(255 * (y / HEIGHT)) for y in range(HEIGHT) for _ in range(WIDTH)])
    return Image.composite(bottom, base, mask)


def _strip_segments(brand_name: str, social_handles: dict, website_url: str | None) -> list[tuple]:
    """Each segment is either ("text", string) for the brand name, or
    ("icon", icon_font_kind, icon_char, label_string) for a handle/website -
    drawn as icon glyph + handle text side by side, not spelled-out platform
    names."""
    segments: list[tuple] = []
    if brand_name:
        segments.append(("text", brand_name))
    for platform in ("instagram", "facebook", "x", "youtube", "tiktok"):
        handle = (social_handles or {}).get(platform)
        if not handle:
            continue
        label = handle.lstrip("@").strip()
        kind, char = PLATFORM_ICONS[platform]
        segments.append(("icon", kind, char, label))
    if website_url:
        kind, char = WEBSITE_ICON
        segments.append(("icon", kind, char, website_url))
    return segments


def add_brand_strip(
    image_bytes: bytes,
    font_key: str = "inter",
    brand_name: str = "",
    social_handles: dict | None = None,
    website_url: str | None = None,
    logo_bytes: bytes | None = None,
    show_brand_name: bool = True,
) -> bytes:
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB").resize((WIDTH, HEIGHT))

    margin = 50
    strip_top_left = (0, HEIGHT - STRIP_HEIGHT)
    strip_bottom_right = (WIDTH, HEIGHT)
    # Frosted-glass strip: blur + darken the actual art already in this
    # region, instead of stamping a flat opaque rectangle over it - reads as
    # a designed panel integrated with the poster rather than a sticker over
    # a finished image, while a light dark veil on top keeps the text drawn
    # afterward just as legible as the old solid-black bar. Falls back to
    # that flat-black rectangle if the blur step raises for any reason (a
    # degenerate crop on a tiny/malformed source image).
    try:
        strip_region = img.crop((*strip_top_left, *strip_bottom_right)).filter(ImageFilter.GaussianBlur(radius=18))
        strip_region = Image.blend(strip_region, Image.new("RGB", strip_region.size, (0, 0, 0)), alpha=0.4)
        img.paste(strip_region, strip_top_left)
        ImageDraw.Draw(img, "RGBA").rectangle(
            [strip_top_left, strip_bottom_right], fill=(0, 0, 0, 70)
        )
    except Exception:
        ImageDraw.Draw(img, "RGBA").rectangle([strip_top_left, strip_bottom_right], fill=(0, 0, 0, 200))

    draw = ImageDraw.Draw(img, "RGBA")
    mid_y = HEIGHT - STRIP_HEIGHT / 2

    # Logo, if any, pasted at the strip's left edge, vertically centered -
    # reserves its own width so the text segments below never overlap it.
    logo_img = None
    logo_reserved_width = 0.0
    logo_gap = 18
    if logo_bytes:
        try:
            logo_img = Image.open(io.BytesIO(logo_bytes)).convert("RGBA")
            logo_height = STRIP_HEIGHT - 30
            logo_width = int(logo_img.width * (logo_height / logo_img.height))
            logo_img = logo_img.resize((logo_width, logo_height))
            logo_reserved_width = logo_width + logo_gap
        except Exception:
            logo_img = None

    # Logo and name are independently optional (brand-level toggles, see
    # BrandKit.show_logo_on_posters/show_brand_name_on_posters) - no longer
    # mutually exclusive.
    segments = _strip_segments(brand_name if show_brand_name else "", social_handles or {}, website_url)
    if segments:
        gap = 22  # between segments
        icon_gap = 10  # between an icon and its own label
        size = 30
        text_font = load_font(font_key, size, text=brand_name or "")
        icon_fonts = {kind: _load_icon_font(kind, size) for kind in _ICON_FONT_FILES}
        available_width = WIDTH - 2 * margin - logo_reserved_width

        # Measure, shrinking the font size until everything fits on one line.
        while size > 16:
            text_font = load_font(font_key, size, text=brand_name or "")
            icon_fonts = {kind: _load_icon_font(kind, size) for kind in _ICON_FONT_FILES}
            total_width = 0.0
            for seg in segments:
                if seg[0] == "text":
                    total_width += draw.textlength(seg[1], font=text_font)
                else:
                    _, kind, char, label = seg
                    total_width += (
                        draw.textlength(char, font=icon_fonts[kind])
                        + icon_gap
                        + draw.textlength(label, font=text_font)
                    )
            total_width += gap * (len(segments) - 1)
            if total_width <= available_width:
                break
            size -= 2

        x = margin + logo_reserved_width + (available_width - total_width) / 2
        # anchor="lm" centers each glyph on its font's own vertical metrics
        # (ascent+descent midpoint) rather than Pillow's default anchor (the
        # ascender line) - needed because the icon font (Font Awesome) and
        # the brand text font have different ascender-to-glyph-top spacing,
        # so drawing both at the same nominal y with the default anchor left
        # icons sitting visibly higher than the text next to them.
        for seg in segments:
            if seg[0] == "text":
                draw.text((x, mid_y), seg[1], font=text_font, fill=(255, 255, 255, 230), anchor="lm")
                x += draw.textlength(seg[1], font=text_font) + gap
            else:
                _, kind, char, label = seg
                icon_font = icon_fonts[kind]
                draw.text((x, mid_y), char, font=icon_font, fill=(255, 255, 255, 230), anchor="lm")
                x += draw.textlength(char, font=icon_font) + icon_gap
                draw.text((x, mid_y), label, font=text_font, fill=(255, 255, 255, 230), anchor="lm")
                x += draw.textlength(label, font=text_font) + gap

    if logo_img is not None:
        logo_y = int(mid_y - logo_img.height / 2)
        img.paste(logo_img, (margin, logo_y), logo_img)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def add_source_line(image_bytes: bytes, font_key: str, source_name: str | None) -> bytes:
    """Stamps a small "Source: {name}" line just above the brand strip -
    deterministic and applied as the last step regardless of which path
    generated the image (AI full-design, AI background + Pillow headline, or
    the fully-deterministic fallback), so attribution to the original
    publication is guaranteed rather than dependent on the image model
    remembering to include it. Minor/muted by design - this is credit, not
    a headline. No-op if there's no known source (e.g. an email with no
    sender, or a manually-written post)."""
    if not source_name:
        return image_bytes
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB").resize((WIDTH, HEIGHT))
    draw = ImageDraw.Draw(img, "RGBA")

    text = f"Source: {source_name}"
    size = 22
    font = load_font(font_key, size, text=text)
    margin = 50
    y = HEIGHT - STRIP_HEIGHT - size - 16
    text_w = draw.textlength(text, font=font)
    x = WIDTH - margin - text_w
    # A translucent backing keeps this legible over any background/art,
    # not just the neutral gradient.
    draw.rounded_rectangle(
        [(x - 10, y - 6), (x + text_w + 10, y + size + 8)], radius=6, fill=(0, 0, 0, 120)
    )
    draw.text((x, y), text, font=font, fill=(255, 255, 255, 210))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def render_fallback_poster(
    font_key: str = "inter",
    brand_name: str = "",
    social_handles: dict | None = None,
    website_url: str | None = None,
    background_bytes: bytes | None = None,
    logo_bytes: bytes | None = None,
    show_brand_name: bool = True,
) -> bytes:
    """No headline/content text - used whenever there's no AI-generated
    image with the headline already baked in (no provider configured, or
    the generation call failed). Pillow only ever stamps branding (name +
    logo) here; content text is either AI-drawn or not shown at all - see
    docs/architecture.md's "no programmatic content text" policy."""
    if background_bytes:
        # Crop-to-fill, not stretch - fine for an AI background generated
        # at exactly (WIDTH, HEIGHT), but a real photo (product photos,
        # any other non-square source) has its own aspect ratio and would
        # visibly distort with a naive resize.
        img = ImageOps.fit(Image.open(io.BytesIO(background_bytes)).convert("RGB"), (WIDTH, HEIGHT))
    else:
        img = _neutral_gradient()

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return add_brand_strip(
        buf.getvalue(), font_key, brand_name, social_handles, website_url, logo_bytes, show_brand_name
    )
