"""Fixed language list for generated content - same reasoning as
graphic_designer/fonts.py's FONT_CHOICES: a bounded, validated set beats
free text, since arbitrary values here would silently confuse the Content
Writer prompt rather than fail loudly. Limited to English/Hindi/Marathi for
now per product decision; extend this dict (and nothing else) to add a
language later."""

LANGUAGE_CHOICES: dict[str, str] = {
    "en": "English",
    "hi": "हिन्दी (Hindi)",
    "mr": "मराठी (Marathi)",
}
DEFAULT_LANGUAGE = "en"


def display_name(code: str) -> str:
    return LANGUAGE_CHOICES.get(code, LANGUAGE_CHOICES[DEFAULT_LANGUAGE])
