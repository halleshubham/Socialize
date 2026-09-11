"""Per-brand full-text overrides of a pipeline stage's system prompt - see
agents/prompt_registry.py for the fixed list of overridable prompt_key
values and their hardcoded default text (used for the Brand Kit "Prompts"
panel). No global-default row (unlike llm/provider.py's AgentModelConfig) -
a brand with no row here just uses whichever default_text its own graph.py
call site passes in (that module's own SYSTEM_PROMPT* constant), so there's
nothing to seed and "reset to default" is just deleting the row.

Full-text replacement, not an append-only "extra instructions" field - an
explicit user decision, trading safety (a bad edit CAN break JSON-output
parsing, or an image model's rendering instructions) for real flexibility.
There is no rules-preserving safety net beyond "reset to default".
"""

import uuid

from sqlalchemy.orm import Session

from backend.app.db.models import AgentPromptConfig


def get_brand_prompt_overrides(db: Session, brand_kit_id: uuid.UUID) -> dict[str, str]:
    """{prompt_key: prompt_text} for whichever slots this brand has an
    override row for - used to pre-fill the Brand Kit Prompts panel's
    textareas. Slots with no override just show the hardcoded default."""
    rows = db.query(AgentPromptConfig).filter(AgentPromptConfig.brand_kit_id == brand_kit_id).all()
    return {row.prompt_key: row.prompt_text for row in rows}


def resolve_prompt(db: Session, brand_kit_id: uuid.UUID | None, prompt_key: str, default_text: str) -> str:
    """The brand's own override text if set, else default_text (that call
    site's own hardcoded SYSTEM_PROMPT* constant) - called from each
    agent's graph.py in place of using the constant directly. brand_kit_id
    can be None for brand-less/legacy calls, in which case this is a no-op."""
    if not brand_kit_id:
        return default_text
    row = (
        db.query(AgentPromptConfig)
        .filter(AgentPromptConfig.brand_kit_id == brand_kit_id, AgentPromptConfig.prompt_key == prompt_key)
        .first()
    )
    return row.prompt_text if row and row.prompt_text.strip() else default_text


def set_prompt_override(db: Session, brand_kit_id: uuid.UUID, prompt_key: str, prompt_text: str) -> None:
    row = (
        db.query(AgentPromptConfig)
        .filter(AgentPromptConfig.brand_kit_id == brand_kit_id, AgentPromptConfig.prompt_key == prompt_key)
        .first()
    )
    if row:
        row.prompt_text = prompt_text
    else:
        db.add(AgentPromptConfig(brand_kit_id=brand_kit_id, prompt_key=prompt_key, prompt_text=prompt_text))
    db.commit()


def clear_prompt_override(db: Session, brand_kit_id: uuid.UUID, prompt_key: str) -> None:
    db.query(AgentPromptConfig).filter(
        AgentPromptConfig.brand_kit_id == brand_kit_id, AgentPromptConfig.prompt_key == prompt_key
    ).delete()
    db.commit()
