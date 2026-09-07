"""Fixed, vendored font list for posters/reel text-cards - brand kit offers
a dropdown over this set rather than free-text font names, since Pillow
needs an actual .ttf file on disk to render anything (a free-text "Inter"
from a brand kit form is useless without the file). All OFL-licensed, from
the Google Fonts repo, with named weight instances where available; poster
text is rendered at the "Bold" instance for legibility over a background.

The original four (Inter/Oswald/Playfair/Roboto) have NO Devanagari glyphs
(verified live: all four produced ".notdef" tofu boxes rendering Hindi/
Marathi text). The rest were added on request and DO carry real Devanagari
coverage confirmed two ways, not just by cmap presence (a font can claim a
codepoint via cmap while still shaping conjuncts wrong) - checked live by
actually rendering a real Marathi sentence with each and reading the
result, the same standard as the original Devanagari bug fix. Two fonts
from that request are NOT included: "Mangal" is a proprietary Microsoft
font with no legal open redistribution, and "Samyak Devanagari" isn't
published on Google Fonts' repo (or anywhere else with a clearly verifiable
open license) - vendoring either would be either illegal or unverifiable.

Devanagari selection: when the text being rendered contains Devanagari
characters, load_font uses font_key's OWN file if that font has Devanagari
coverage (DEVANAGARI_CAPABLE) - all fonts added for this request do, so
picking e.g. "Rajdhani" as the brand's poster font now styles Marathi/Hindi
posters in Rajdhani too, not a fixed substitute. Only the original four
(no Devanagari glyphs at all) fall back to DEFAULT_DEVANAGARI_FONT_KEY.
"""

from pathlib import Path

from PIL import ImageFont

_FONTS_DIR = Path("backend/app/static/fonts")

# key -> (display name, filename)
FONT_CHOICES: dict[str, tuple[str, str]] = {
    "inter": ("Inter", "Inter-Bold.ttf"),
    "oswald": ("Oswald", "Oswald-Bold.ttf"),
    "playfair": ("Playfair Display", "PlayfairDisplay-Bold.ttf"),
    "roboto": ("Roboto", "Roboto-Bold.ttf"),
    # Devanagari-capable, body/text style
    "noto_sans_devanagari": ("Noto Sans Devanagari", "NotoSansDevanagari-Bold.ttf"),
    "noto_serif_devanagari": ("Noto Serif Devanagari", "NotoSerifDevanagari-Bold.ttf"),
    "mukta": ("Mukta", "Mukta-Bold.ttf"),
    # Devanagari-capable, display/headline style
    "rozha_one": ("Rozha One", "RozhaOne-Regular.ttf"),
    "yatra_one": ("Yatra One", "YatraOne-Regular.ttf"),
    "khand": ("Khand", "Khand-Bold.ttf"),
    "rajdhani": ("Rajdhani", "Rajdhani-Bold.ttf"),
    "teko": ("Teko", "Teko-Bold.ttf"),
    "tiro_devanagari_hindi": ("Tiro Devanagari Hindi", "TiroDevanagariHindi-Regular.ttf"),
    "poppins": ("Poppins", "Poppins-Bold.ttf"),
}
DEFAULT_FONT_KEY = "inter"

# Grouping for the brand-kit dropdown (<optgroup>s) - purely presentational.
FONT_GROUPS: list[tuple[str, list[str]]] = [
    ("General", ["inter", "oswald", "playfair", "roboto"]),
    ("Devanagari - body", ["noto_sans_devanagari", "noto_serif_devanagari", "mukta"]),
    (
        "Devanagari - display",
        ["rozha_one", "yatra_one", "khand", "rajdhani", "teko", "tiro_devanagari_hindi", "poppins"],
    ),
]

DEVANAGARI_CAPABLE: set[str] = {
    "noto_sans_devanagari", "noto_serif_devanagari", "mukta",
    "rozha_one", "yatra_one", "khand", "rajdhani", "teko", "tiro_devanagari_hindi", "poppins",
}
DEFAULT_DEVANAGARI_FONT_KEY = "noto_sans_devanagari"


def contains_devanagari(text: str) -> bool:
    """Hindi and Marathi both use the Devanagari block (U+0900-U+097F)."""
    return any("ऀ" <= ch <= "ॿ" for ch in text)


def load_font(font_key: str, size: int, text: str = "", weight: str = "Bold") -> ImageFont.FreeTypeFont:
    """text (the actual string about to be rendered) decides whether
    Devanagari coverage is required; font_key (the brand's style choice) is
    honored either way as long as it actually has the glyphs - only falls
    back to DEFAULT_DEVANAGARI_FONT_KEY when it doesn't (see DEVANAGARI_CAPABLE)."""
    if contains_devanagari(text) and font_key not in DEVANAGARI_CAPABLE:
        font_key = DEFAULT_DEVANAGARI_FONT_KEY
    _, filename = FONT_CHOICES.get(font_key, FONT_CHOICES[DEFAULT_FONT_KEY])
    font = ImageFont.truetype(str(_FONTS_DIR / filename), size)
    try:
        font.set_variation_by_name(weight)
    except OSError:
        pass  # falls back to the font's default instance
    return font
