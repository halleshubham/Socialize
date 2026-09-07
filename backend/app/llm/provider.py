"""Thin adapter over LiteLLM so every agent calls one interface regardless of
provider, and the model used per agent/task is a DB-editable string, not a
hardcoded constant. Image/video generation are NOT routed through here -
LiteLLM's coverage of those is inconsistent across providers; see
llm/image_provider.py and llm/video_provider.py (added in Phase 2/3) instead.
"""

import time
import uuid
from dataclasses import dataclass, field

import litellm
from sqlalchemy.orm import Session

from backend.app.db.models import AgentModelConfig, LlmCallLog
from backend.app.llm.user_keys import resolve_api_key

# Fallback defaults if a task has no row yet in agent_model_config.
# Provider prefixes follow LiteLLM's "<provider>/<model>" convention.
DEFAULT_MODELS: dict[str, tuple[str, str]] = {
    "researcher_triage": ("anthropic", "anthropic/claude-haiku-4-5"),
    "researcher_niche_evolution": ("anthropic", "anthropic/claude-sonnet-5"),
    "analytical_brief": ("anthropic", "anthropic/claude-sonnet-5"),
    # Combined-drafting brands (brand_kit.combined_drafting) skip the
    # separate Content Writer call entirely - this one call does brief +
    # copy/hashtags/poster-or-reel fields together, on the cheap model, by
    # design (see analytical/graph.py::write_brief_and_copy).
    "analytical_combined_draft": ("anthropic", "anthropic/claude-sonnet-5"),
    "content_writer": ("anthropic", "anthropic/claude-opus-5"),
    # Used instead of "content_writer" for language="hi"/"mr" - see
    # content_writer/graph.py's _agent_task_for_language. User-reported:
    # Claude's Marathi output quality wasn't good enough; GPT-5.4 swapped in
    # for those two languages specifically, Claude stays default elsewhere.
    "content_writer_localized": ("openai", "openai/gpt-5.4"),
    "graphic_designer_direction": ("anthropic", "anthropic/claude-sonnet-5"),
    "reel_shotlist": ("anthropic", "anthropic/claude-sonnet-5"),
}

# Curated so the brand-kit UI offers a dropdown, not free text - a typo'd
# model_id would silently break generation with no useful error until the
# next real content item hits it. (provider, model_id) -> display label.
MODEL_CHOICES: dict[tuple[str, str], str] = {
    ("anthropic", "anthropic/claude-opus-5"): "Claude Opus 5 (highest quality, priciest)",
    ("anthropic", "anthropic/claude-sonnet-5"): "Claude Sonnet 5 (balanced)",
    ("anthropic", "anthropic/claude-haiku-4-5"): "Claude Haiku 4.5 (fastest, cheapest)",
    ("openai", "openai/gpt-5.4"): "GPT-5.4",
}

# Which agent_tasks are exposed on the Brand Kit page for a per-brand
# override, and their display labels - the "analysis / content writing /
# image generation / reels generation" steps that are actually litellm chat
# calls (image/video GENERATION itself is a separate provider SDK, not
# litellm - see llm/image_provider.py / video_provider.py; the reel video
# TIER is already brand-configurable via reel_editor/graph.py's
# get_video_model_key, exposed on the board's settings panel instead).
CONFIGURABLE_AGENT_TASKS: dict[str, str] = {
    "researcher_triage": "Researcher (triage which articles are worth posting)",
    "analytical_brief": "Analytical (writes the brief)",
    "analytical_combined_draft": "Combined drafting (brief + post together)",
    "content_writer": "Content Writer",
    "content_writer_localized": "Content Writer (Hindi/Marathi)",
    "graphic_designer_direction": "Graphic Designer (creative-direction prompt)",
    "reel_shotlist": "Reel Editor (shot-listing)",
}


def get_brand_model_overrides(db: Session, brand_kit_id: uuid.UUID) -> dict[str, tuple[str, str]]:
    """{agent_task: (provider, model_id)} for whichever of CONFIGURABLE_AGENT_TASKS
    this brand has an explicit override row for - used to pre-fill the
    Brand Kit page's dropdowns. Tasks with no override just show the
    global/default choice instead."""
    rows = (
        db.query(AgentModelConfig)
        .filter(
            AgentModelConfig.brand_kit_id == brand_kit_id,
            AgentModelConfig.agent_task.in_(CONFIGURABLE_AGENT_TASKS),
        )
        .all()
    )
    return {row.agent_task: (row.provider, row.model_id) for row in rows}


def set_brand_model_override(db: Session, brand_kit_id: uuid.UUID, agent_task: str, provider: str, model_id: str) -> None:
    row = (
        db.query(AgentModelConfig)
        .filter(AgentModelConfig.agent_task == agent_task, AgentModelConfig.brand_kit_id == brand_kit_id)
        .first()
    )
    if row:
        row.provider, row.model_id = provider, model_id
    else:
        db.add(AgentModelConfig(agent_task=agent_task, brand_kit_id=brand_kit_id, provider=provider, model_id=model_id))
    db.commit()


def clear_brand_model_override(db: Session, brand_kit_id: uuid.UUID, agent_task: str) -> None:
    db.query(AgentModelConfig).filter(
        AgentModelConfig.agent_task == agent_task, AgentModelConfig.brand_kit_id == brand_kit_id
    ).delete()
    db.commit()


@dataclass
class ChatResult:
    text: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: int
    model_id: str
    provider: str
    raw: dict = field(default_factory=dict)


class ChatProvider:
    """Usage:
        provider = ChatProvider(db, brand_kit_id)
        result = provider.complete("analytical_brief", messages=[...])
    `agent_task` must match a row in agent_model_config (seeded by
    scripts/seed_agent_models.py) or fall back to DEFAULT_MODELS above.

    brand_kit_id (optional) is who to bill this call to - resolve_api_key
    uses the brand owner's own key if they've set one (see llm/user_keys.py),
    falling back to the shared .env-configured key otherwise. Omit it (e.g.
    scripts/check_providers.py, which has no brand context) to go straight
    to the .env fallback.
    """

    def __init__(self, db: Session, brand_kit_id: uuid.UUID | None = None):
        self.db = db
        self.brand_kit_id = brand_kit_id

    def _resolve_model(self, agent_task: str) -> tuple[str, str, dict]:
        # This brand's own override first (agent_model_config.brand_kit_id
        # matching), then the global default row (brand_kit_id IS NULL),
        # then the hardcoded DEFAULT_MODELS fallback if neither exists.
        if self.brand_kit_id:
            row = (
                self.db.query(AgentModelConfig)
                .filter(
                    AgentModelConfig.agent_task == agent_task,
                    AgentModelConfig.brand_kit_id == self.brand_kit_id,
                )
                .first()
            )
            if row:
                return row.provider, row.model_id, row.params or {}

        row = (
            self.db.query(AgentModelConfig)
            .filter(AgentModelConfig.agent_task == agent_task, AgentModelConfig.brand_kit_id.is_(None))
            .first()
        )
        if row:
            return row.provider, row.model_id, row.params or {}
        if agent_task not in DEFAULT_MODELS:
            raise ValueError(
                f"No agent_model_config row and no default for task '{agent_task}'"
            )
        provider, model_id = DEFAULT_MODELS[agent_task]
        return provider, model_id, {}

    def complete(
        self,
        agent_task: str,
        messages: list[dict],
        content_item_id=None,
        model_override: str | None = None,
        provider_override: str | None = None,
        **extra_params,
    ) -> ChatResult:
        if model_override:
            provider, model_id, params = provider_override or "unknown", model_override, {}
        else:
            provider, model_id, params = self._resolve_model(agent_task)
        call_params = {**params, **extra_params}
        api_key = resolve_api_key(self.db, self.brand_kit_id, provider)
        if api_key:
            call_params["api_key"] = api_key

        started = time.monotonic()
        response = litellm.completion(model=model_id, messages=messages, **call_params)
        latency_ms = int((time.monotonic() - started) * 1000)

        usage = response.get("usage", {}) or {}
        input_tokens = usage.get("prompt_tokens", 0)
        output_tokens = usage.get("completion_tokens", 0)
        try:
            cost_usd = litellm.completion_cost(completion_response=response)
        except Exception:
            cost_usd = 0.0

        text = response["choices"][0]["message"]["content"] or ""

        self.db.add(
            LlmCallLog(
                agent_task=agent_task,
                provider=provider,
                model_id=model_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
                latency_ms=latency_ms,
                content_item_id=content_item_id,
            )
        )
        self.db.commit()

        return ChatResult(
            text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            model_id=model_id,
            provider=provider,
            raw=response,
        )
