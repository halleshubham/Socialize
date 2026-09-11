"""Client for GitHub's REST API - the grounding source for "Draft from
GitHub" (routes_board.py), which pulls a repo's real README/changelog/
commit history instead of a newsletter to draft posts about a project's
own features. Plain httpx calls, no SDK, same style as PostizClient/
BotsabClient.
"""

import base64

import httpx

_TIMEOUT_SECONDS = 30
_API_VERSION = "2022-11-28"


class GithubError(Exception):
    pass


class GithubClient:
    def __init__(self, token: str | None):
        self.token = token

    def _headers(self) -> dict:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": _API_VERSION,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _get(self, path: str, **params) -> httpx.Response:
        response = httpx.get(
            f"https://api.github.com{path}",
            headers=self._headers(),
            params=params,
            timeout=_TIMEOUT_SECONDS,
        )
        return response

    def list_repos(self, per_page: int = 100) -> list[dict]:
        """GET /user/repos - needs a token (there's no "my repos" without
        auth). Used for the Brand Kit page's live checkbox list."""
        response = self._get("/user/repos", per_page=per_page, sort="pushed", affiliation="owner,collaborator")
        if response.status_code >= 400:
            raise GithubError(f"GitHub API error {response.status_code}: {response.text}")
        return [{"full_name": r["full_name"], "private": r["private"]} for r in response.json()]

    def get_readme(self, full_name: str) -> str | None:
        response = self._get(f"/repos/{full_name}/readme")
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise GithubError(f"GitHub API error {response.status_code}: {response.text}")
        return self._decode(response.json())

    def get_file(self, full_name: str, path: str) -> str | None:
        """Best-effort fetch of a single file (e.g. CHANGELOG.md) - None on
        404 rather than raising, since most repos won't have every
        candidate path fetch_grounding_text tries."""
        response = self._get(f"/repos/{full_name}/contents/{path}")
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise GithubError(f"GitHub API error {response.status_code}: {response.text}")
        data = response.json()
        if isinstance(data, list):  # path was a directory, not a file
            return None
        return self._decode(data)

    def list_recent_commits(self, full_name: str, limit: int = 15) -> list[str]:
        response = self._get(f"/repos/{full_name}/commits", per_page=limit)
        if response.status_code >= 400:
            raise GithubError(f"GitHub API error {response.status_code}: {response.text}")
        return [c["commit"]["message"].splitlines()[0] for c in response.json()]

    def _decode(self, content_response: dict) -> str | None:
        if content_response.get("encoding") != "base64":
            return content_response.get("content")
        return base64.b64decode(content_response["content"]).decode("utf-8", errors="replace")
