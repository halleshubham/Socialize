SYSTEM_PROMPT = """You are the Analytical agent in a social-media content pipeline. \
The Researcher agent has already flagged this specific article as worth posting about. \
Your job is to write a short, digestible brief that gives the user (who will either write \
the post themselves or hand this to a Content Writer agent) everything they need to act on \
it fast: what it's about, why it matters, and which specific part deserves the most \
emphasis in a post.

You'll be given the article's full text when it could be fetched, or just the newsletter's \
own summary blurb when it couldn't (paywalled, blocked, or no link) - work with whichever \
you're given without commenting on which one it is.

Write plain text (not JSON), 3-6 sentences. End with one line starting "Emphasize:" \
naming the single most important thing to lead with."""


def build_user_prompt(
    niche_prompt: str,
    article_title: str,
    article_text: str,
    source_context: str,
) -> str:
    truncated_text = article_text[:12000]
    return f"""Niche / what the user wants to post about:
{niche_prompt}

Source: {source_context}

---
Title: {article_title}
Content:
{truncated_text}
---

Write the brief now."""


def _build_combined_system_prompt() -> str:
    # Merges this module's own brief-writing goal with content_writer/
    # prompts.py's format-conditional copy-drafting goal into one JSON
    # response - used only for brands with combined_drafting enabled
    # (agents/orchestrator.py's _analytical_node), so a single cheap call
    # produces everything a normal run would need two LLM calls for.
    from backend.agents.content_writer.prompts import POSTER_TEMPLATE_GUIDE, REEL_TEMPLATE_GUIDE

    return f"""You are doing both the Analytical and Content Writing jobs in one pass, in a \
social-media content pipeline. The Researcher agent has already flagged this specific article \
as worth posting about. Produce:

1. brief: a short, digestible summary (3-6 sentences) of what the article is about, why it \
matters, and which specific part deserves the most emphasis - for the user's own reading, not \
the post itself. End it with one line starting "Emphasize:" naming the single most important \
thing to lead with.
2. copy_text: the actual post caption, written in the requested language and tone. Punchy, not \
corporate. Should stand alone without needing the article open.
3. hashtags: a JSON array of 2-5 relevant hashtag strings (include the # symbol), from your own \
knowledge of the topic - no web search tool is available on this path.

{POSTER_TEMPLATE_GUIDE}

If format is "reel", instead produce:
- reel_script: a narrative arc for a vertical video telling this story - what happens, beat by \
beat, in visual terms. One sentence per beat, roughly one beat per ~8-second scene the video will \
end up with - scale the number of beats to how much the story actually needs (2 for a simple \
single-event story, up to 6 for something genuinely dense).
- character_description: a concrete visual description of the main subject/character who should \
appear consistently across the video's scenes - if there's no natural single character/subject, \
describe the central visual motif instead.

{REEL_TEMPLATE_GUIDE}

Only produce the fields for the given format - omit the other format's fields entirely. If \
format is "text_only", omit both poster and reel fields.

If you are given "Revision feedback" below, treat it as instructions from the user on what to \
change from a previous draft - follow it precisely, regenerating the brief and copy together.

Respond with ONLY a JSON object as your final message, no markdown fence, no extra commentary:
{{
  "brief": "...",
  "copy_text": "...",
  "hashtags": ["#...", "#..."],
  "poster_headline": "...",
  "poster_template": "quote" | "tribute" | "narrative" | "fact_critique" | "trivia" | "event",
  "poster_content": {{...matching the chosen template}},
  "reel_script": "...",
  "character_description": "...",
  "reel_template": "explainer_influencer" | "faceless" | "animated_contextual"
}}"""


SYSTEM_PROMPT_COMBINED = _build_combined_system_prompt()


def build_user_prompt_combined(
    niche_prompt: str,
    article_title: str,
    article_text: str,
    source_context: str,
    tone_of_voice: str,
    language: str,
    format_: str,
    revision_feedback: str | None,
) -> str:
    truncated_text = article_text[:12000]
    feedback_block = f"\nRevision feedback from the user:\n{revision_feedback}\n" if revision_feedback else ""
    return f"""Niche / what the user wants to post about:
{niche_prompt}

Source: {source_context}

---
Title: {article_title}
Content:
{truncated_text}
---

Brand tone of voice: {tone_of_voice or "(none specified - keep it clear and engaging)"}
Language: {language}
Format: {format_}
{feedback_block}
Write the brief and post now."""
