"""idea_to_angle is the free-text "Draft from an idea" source's angle
builder - the manual-input sibling of researcher/github_angles.py and
researcher/product_angles.py's extract_angles, except with no LLM call:
the user's own text already is the angle. Confirms it produces the exact
shape create_content_items expects ({title, url, summary, full_text,
suitable_for_social, priority_score, rationale})."""

from backend.app.api.routes_board import idea_to_angle


def test_uses_first_line_as_title():
    angle = idea_to_angle("Announce the new refund policy\nAlways refunded within 3 days now.")
    assert angle["title"] == "Announce the new refund policy"
    assert angle["summary"] == "Announce the new refund policy\nAlways refunded within 3 days now."


def test_always_suitable_with_max_priority():
    angle = idea_to_angle("Some idea")
    assert angle["suitable_for_social"] is True
    assert angle["priority_score"] == 100
    assert angle["url"] is None


def test_extra_context_folded_into_full_text_not_summary():
    angle = idea_to_angle("Some idea", extra_context="Mention the 20% discount code")
    assert angle["summary"] == "Some idea"
    assert angle["full_text"] == "Some idea\n\nMention the 20% discount code"


def test_no_extra_context_full_text_matches_idea():
    angle = idea_to_angle("Some idea", extra_context="   ")
    assert angle["full_text"] == "Some idea"


def test_blank_idea_gets_placeholder_title():
    angle = idea_to_angle("   ")
    assert angle["title"] == "Untitled idea"
    assert angle["summary"] == ""


def test_long_first_line_truncated_to_500_safe_title_length():
    long_idea = "x" * 300
    angle = idea_to_angle(long_idea)
    assert len(angle["title"]) == 120
