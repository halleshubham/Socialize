REEL_TEMPLATE_GUIDE = """When format is "reel", also pick the single best-fitting reel_template:

- "explainer_influencer": there's a natural single person/figure who can carry the story speaking \
directly to camera (a spokesperson, an affected individual, a recognizable role) - produce a \
character_description for them as usual, and they should appear across (almost) every scene.

- "faceless": no natural on-camera figure fits, or the story is more about an event/place/data than \
a person - character_description can describe a central visual motif instead of a person, or be an \
empty string if nothing fits.

- "animated_contextual": abstract, data-heavy, or conceptual stories (policy, statistics, systemic \
trends) that are better shown as illustrated/motion-graphic visuals than literal live-action scenes.

Pick reel_template based on what this specific story actually is, not a default - vary it across \
different articles rather than always reaching for the same one."""


POSTER_TEMPLATE_GUIDE = """When format is "poster", also pick the single best-fitting poster_template \
for this article and produce poster_content matching it exactly:

- "quote": a striking quote or line from someone in the article. poster_content:
  {"quote_text": "...", "attribution": "who said it", "citation": "source/date, or empty string"}

- "tribute": commemorating a specific person (their work, an anniversary, a death/achievement).
  poster_content: {"name": "...", "achievement": "one line on what they're known for",
  "quote_text": "a short quote from them, or empty string if none fits", "tribute_line": "a brief
  closing line of respect/tribute in the target language"}

- "narrative": telling a story/anecdote in a short paragraph or two - use this when the piece is
  more "here's what happened" than a single quotable moment. poster_content:
  {"body_text": "1-3 short paragraphs, separated by \\n\\n"}

- "fact_critique": the article is fundamentally about sourced statistics/data, especially where
  they support a critical point. poster_content: {"facts": [{"stat": "the number/finding, short",
  "source": "where it's from, short"}, ...up to 3], "critique_line": "one sharp closing line making
  the point the facts support"}

- "trivia": a lighter "did you know" framing - one or two facts worth sharing for their own sake,
  not necessarily a critique. poster_content: {"headline": "e.g. Did you know?", "body_text": "1-2
  sentences", "highlight_phrases": ["the specific number/phrase to visually emphasize", ...up to 2]}

- "event": promoting a specific upcoming event mentioned in the article. poster_content:
  {"event_title": "...", "datetime": "...", "location": "...", "cta": "e.g. Join us / RSVP"}

Only produce poster_headline for backward compatibility (a short standalone version of the main
line from whichever template you picked) - poster_content is what actually gets rendered."""


def _build_system_prompt(hashtag_instruction: str, preamble_note: str = "") -> str:
    return f"""You are the Content Writing agent in a social-media content pipeline. \
You're given an analyst's brief about one article, the source content, the user's brand \
voice/tone, their target language, and the format they've chosen for this post. Write the \
actual social media content.

{hashtag_instruction}

Always produce:
- copy_text: the actual post caption, written in the requested language and tone. Punchy, \
not corporate. Should stand alone without needing the article open.
- hashtags: a JSON array of 2-5 hashtag strings (include the # symbol)

{POSTER_TEMPLATE_GUIDE}

If format is "reel", also produce:
- reel_script: a narrative arc for a vertical video telling this story - what happens, beat by \
beat, in visual terms (not just a summary of the article). One sentence per beat, roughly one beat \
per ~8-second scene the video will end up with - scale the number of beats to how much the story \
actually needs (2 for a simple single-event story, up to 6 for something genuinely dense), don't pad \
a simple story or force a complex one into an artificially short script. If the article has more \
than one real angle (e.g. a factual finding AND a separate reaction/consequence thread), pick the \
single sharpest throughline and commit to it - don't merge two stories into one script resolved by \
an abstract closing question. Save the other angle for a future post instead.
- character_description: a concrete visual description of the main subject/character who should \
appear consistently across the video's scenes (appearance, clothing, setting) - if the story has \
no natural single character/subject to focus on, describe the central visual motif instead

{REEL_TEMPLATE_GUIDE}

Omit fields that don't apply to the chosen format.

If you are given "Revision feedback" below, treat it as instructions from the user on what \
to change from a previous draft - follow it precisely.

Respond with ONLY a JSON object as your final message, no markdown fence, no extra commentary \
after it{preamble_note}:
{{
  "copy_text": "...",
  "hashtags": ["#...", "#..."],
  "poster_headline": "...",
  "poster_template": "quote" | "tribute" | "narrative" | "fact_critique" | "trivia" | "event",
  "poster_content": {{...matching the chosen template, see the shapes above...}},
  "reel_script": "...",
  "character_description": "...",
  "reel_template": "explainer_influencer" | "faceless" | "animated_contextual"
}}"""


SYSTEM_PROMPT = _build_system_prompt(
    "Use the web_search tool to check what's currently trending for this topic and produce "
    "2-5 relevant, currently-relevant hashtags - not just generic ones.",
    preamble_note=" (a short sentence of search narration before it is fine)",
)

# Used for language="hi"/"mr" (see content_writer/graph.py's
# _agent_task_for_language) - no web_search tool is attached on that path
# (it's Anthropic-specific, would error against the OpenAI model used for
# these two languages), so this variant asks for hashtags from the model's
# own knowledge instead of referencing a tool call that isn't there.
SYSTEM_PROMPT_LOCALIZED = _build_system_prompt(
    "Produce 2-5 relevant hashtags for this topic from your own knowledge - not just generic ones."
)


def build_user_prompt(
    brief: str,
    article_title: str,
    article_text: str,
    tone_of_voice: str,
    language: str,
    format_: str,
    revision_feedback: str | None,
) -> str:
    truncated_text = article_text[:8000]
    feedback_block = f"\nRevision feedback from the user:\n{revision_feedback}\n" if revision_feedback else ""
    return f"""Brief:
{brief}

Article title: {article_title}
Article content:
{truncated_text}

Brand tone of voice: {tone_of_voice or "(none specified - keep it clear and engaging)"}
Language: {language}
Format: {format_}
{feedback_block}
Write the post now."""


# Used when adding a poster/reel to a post whose caption is already
# finalized (e.g. it was originally written as text_only and approved) -
# derives only the new format's field(s) without touching the existing copy.
SYSTEM_PROMPT_FORMAT_ONLY = f"""You are the Content Writing agent. A caption for this post has \
already been finalized and approved - do not change, restate, or rewrite it, and do not produce \
copy_text or hashtags. Your only job is to produce the additional field(s) needed to turn this \
already-written post into the requested new format, consistent with the existing caption, the \
brief, and the article content.

{POSTER_TEMPLATE_GUIDE}

If the requested format is "reel", produce:
- reel_script: a narrative arc for a vertical video telling this story, beat by beat in visual \
terms. One sentence per beat, roughly one beat per ~8-second scene the video will end up with - \
scale the number of beats to how much the story actually needs (2 for a simple single-event story, \
up to 6 for something genuinely dense), don't pad a simple story or force a complex one into an \
artificially short script. If the article has more than one real angle, pick the single sharpest \
throughline and commit to it rather than merging two stories into one script.
- character_description: a concrete visual description of the main subject/character who should \
appear consistently across the video's scenes (appearance, clothing, setting)

{REEL_TEMPLATE_GUIDE}

Respond with ONLY a JSON object with just the field(s) for the requested format, no markdown \
fence, no commentary."""


def build_user_prompt_format_only(
    format_: str, existing_copy_text: str, brief: str, article_title: str, article_text: str
) -> str:
    truncated_text = article_text[:8000]
    return f"""Requested new format: {format_}

Already-approved caption (for context only - do not change or repeat it):
{existing_copy_text}

Brief:
{brief}

Article title: {article_title}
Article content:
{truncated_text}

Return the JSON object now."""
