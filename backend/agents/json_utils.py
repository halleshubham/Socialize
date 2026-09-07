import json
import re

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)

# Observed live (content_writer, Claude + web_search): citation-grounded
# sentences occasionally come back wrapped in leftover citation-tag syntax
# instead of plain prose, e.g. '(cite index="3-3,3-4">Protected lanes cut
# cyclist risk by 34%...</cite>' - malformed (a stray "(" instead of "<" on
# the opening tag) but the pattern is consistent enough to catch. Root cause
# not confirmed (whether this is Claude imitating an internal citation
# format, or a LiteLLM/Anthropic content-block-join artifact), so this is a
# defensive strip applied to every text field in the parsed response rather
# than a fix at the source - keeps the real (correct) cited sentence, only
# removes the tag wrapper around it.
_CITATION_TAG_RE = re.compile(r'[<(]cite[^>]*>(.*?)</cite>', re.IGNORECASE | re.DOTALL)
_STRAY_CITE_RE = re.compile(r'</?cite[^>]*>', re.IGNORECASE)


def strip_citation_artifacts(text: str) -> str:
    if not text:
        return text
    text = _CITATION_TAG_RE.sub(r"\1", text)
    text = _STRAY_CITE_RE.sub("", text)
    return text


def sanitize_llm_json(value):
    """Recursively applies strip_citation_artifacts to every string in a
    parsed LLM JSON response (dict/list of any shape) - call on the whole
    extract_json(...) result so every field (copy_text, poster_content's
    nested strings, reel_script, ...) is covered uniformly, not just
    whichever field happened to leak it last time."""
    if isinstance(value, str):
        return strip_citation_artifacts(value)
    if isinstance(value, list):
        return [sanitize_llm_json(v) for v in value]
    if isinstance(value, dict):
        return {k: sanitize_llm_json(v) for k, v in value.items()}
    return value


def extract_json(text: str) -> dict:
    """LLMs asked for JSON often wrap it in a ```json fence anyway, or - when
    a tool like web_search is involved - narrate their reasoning before the
    JSON despite being told not to. Try a straight parse first, then a fenced
    code block, then the outermost {...} span in the text."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    fence_match = _FENCE_RE.search(text)
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except json.JSONDecodeError:
            pass

    brace_start, brace_end = text.find("{"), text.rfind("}")
    if brace_start != -1 and brace_end > brace_start:
        return json.loads(text[brace_start : brace_end + 1])

    raise json.JSONDecodeError("No JSON object found in model output", text, 0)
