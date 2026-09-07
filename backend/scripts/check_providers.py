"""Phase-0 verification: round-trip a trivial prompt through all three LLM
providers via the ChatProvider adapter, to confirm credentials + LiteLLM
wiring actually work before building agents on top of it.

Usage: python -m backend.scripts.check_providers
Requires ANTHROPIC_API_KEY / OPENAI_API_KEY / GOOGLE_API_KEY in .env.
"""

from backend.app.db.session import SessionLocal
from backend.app.llm.provider import ChatProvider

CHECK_MODELS = {
    "anthropic": "anthropic/claude-haiku-4-5",
    "openai": "openai/gpt-5-mini",
    "google": "gemini/gemini-2.5-flash",
}

PROMPT = [{"role": "user", "content": "Reply with exactly one word: pong"}]


def main() -> None:
    db = SessionLocal()
    provider = ChatProvider(db)
    for name, model_id in CHECK_MODELS.items():
        try:
            result = provider.complete(
                agent_task=f"__check_{name}",
                messages=PROMPT,
                model_override=model_id,
                provider_override=name,
            )
        except Exception as exc:  # noqa: BLE001 - diagnostic script
            print(f"[{name}] FAILED: {exc}")
            continue
        print(f"[{name}] {model_id} -> {result.text!r} (${result.cost_usd:.5f}, {result.latency_ms}ms)")
    db.close()


if __name__ == "__main__":
    main()
