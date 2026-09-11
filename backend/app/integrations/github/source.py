"""Resolves a brand's GitHub client and assembles grounding text for the
angle-extraction step (agents/researcher/github_angles.py) - the GitHub
equivalent of what a fetched newsletter email is for the Gmail path.
"""

from backend.app.config import get_settings
from backend.app.db.models import BrandKit
from backend.app.integrations.github.client import GithubClient, GithubError
from backend.app.util.crypto import decrypt

# Best-effort extra docs to fold into the grounding text alongside the
# README - not every repo has these, 404s are silently skipped.
_EXTRA_DOC_PATHS = ["CHANGELOG.md", "docs/architecture.md", "ARCHITECTURE.md"]


def get_github_client(brand_kit: BrandKit | None) -> GithubClient | None:
    """Brand's own PAT (Brand Kit page) falls back to the shared
    GITHUB_TOKEN env var, same pattern as Postiz/Botsab. A client is
    still returned with token=None (works unauthenticated, public repos
    only, 60 req/hr) rather than None, since GitHub's read endpoints don't
    strictly require auth the way Postiz/Botsab's do."""
    settings = get_settings()
    token = (
        decrypt(brand_kit.github_token_encrypted) if brand_kit and brand_kit.github_token_encrypted else None
    ) or settings.github_token or None
    return GithubClient(token)


def list_brand_repos(brand_kit: BrandKit | None) -> list[dict]:
    """Live GET /user/repos for the Brand Kit page's checkbox list -
    best-effort: [] if there's no token configured or the call fails, so
    the page still renders with a "connect a token first" hint."""
    client = get_github_client(brand_kit)
    if not client or not client.token:
        return []
    try:
        return client.list_repos()
    except GithubError:
        return []


def fetch_grounding_text(client: GithubClient, full_name: str) -> str:
    """README + any found extra docs + recent commit messages, joined into
    one labeled blob for the angle-extraction prompt - the real project
    material a post should be grounded in, the same "fetch the real text,
    not just a blurb" principle article_fetch.py already uses for
    newsletter links."""
    sections = []

    readme = client.get_readme(full_name)
    if readme:
        sections.append(f"## README\n\n{readme}")

    for path in _EXTRA_DOC_PATHS:
        content = client.get_file(full_name, path)
        if content:
            sections.append(f"## {path}\n\n{content}")

    commits = client.list_recent_commits(full_name)
    if commits:
        commit_list = "\n".join(f"- {message}" for message in commits)
        sections.append(f"## Recent commits\n\n{commit_list}")

    return "\n\n".join(sections)
