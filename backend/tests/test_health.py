from fastapi.testclient import TestClient

from backend.app.main import app


def test_healthz():
    client = TestClient(app)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_niche_requires_auth():
    client = TestClient(app)
    resp = client.get("/niche", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
