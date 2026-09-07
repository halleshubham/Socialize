"""Seed agent_model_config with the Phase-0 defaults from llm/provider.py's
DEFAULT_MODELS, so they're visible/editable in the DB (and UI) from day one
instead of only existing as code fallbacks.

Usage: python -m backend.scripts.seed_agent_models
"""

from backend.app.db.models import AgentModelConfig
from backend.app.db.session import SessionLocal
from backend.app.llm.provider import DEFAULT_MODELS


def main() -> None:
    db = SessionLocal()
    try:
        for agent_task, (provider, model_id) in DEFAULT_MODELS.items():
            existing = (
                db.query(AgentModelConfig)
                .filter(AgentModelConfig.agent_task == agent_task, AgentModelConfig.brand_kit_id.is_(None))
                .first()
            )
            if existing:
                continue
            db.add(
                AgentModelConfig(agent_task=agent_task, provider=provider, model_id=model_id)
            )
            print(f"seeded {agent_task} -> {model_id}")
        db.commit()
    finally:
        db.close()


if __name__ == "__main__":
    main()
