"""GMAIL_CLIENT_ID/GMAIL_CLIENT_SECRET env config vs the client JSON file fallback."""

import json

import pytest

from backend.app.config import get_settings
from backend.app.integrations.gmail import oauth


def _reset(monkeypatch, **env):
    for k in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_CREDENTIALS_PATH"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()


def test_env_wins(monkeypatch, tmp_path):
    f = tmp_path / "c.json"
    f.write_text(json.dumps({"installed": {"client_id": "file"}}))
    _reset(
        monkeypatch, GMAIL_CLIENT_ID="id1", GMAIL_CLIENT_SECRET="s1", GMAIL_CREDENTIALS_PATH=str(f)
    )
    cfg = oauth.gmail_client_config()
    assert cfg["web"]["client_id"] == "id1" and cfg["web"]["client_secret"] == "s1"
    flow = oauth.build_authorization_flow("https://socialize.example/brand-kit/gmail/callback")
    url, _ = flow.authorization_url(prompt="consent")
    assert "client_id=id1" in url and "redirect_uri=https%3A%2F%2Fsocialize.example" in url


def test_file_fallback(monkeypatch, tmp_path):
    f = tmp_path / "c.json"
    f.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "file",
                    "client_secret": "x",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            }
        )
    )
    _reset(monkeypatch, GMAIL_CLIENT_ID="only-id", GMAIL_CREDENTIALS_PATH=str(f))
    assert oauth.gmail_client_config()["installed"]["client_id"] == "file"


def test_not_configured(monkeypatch, tmp_path):
    _reset(monkeypatch, GMAIL_CREDENTIALS_PATH=str(tmp_path / "missing.json"))
    with pytest.raises(oauth.GmailClientNotConfigured):
        oauth.build_authorization_flow("https://x/cb")
