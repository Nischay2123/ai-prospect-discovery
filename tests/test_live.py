"""Opt-in live test: real LLM, search and YouTube APIs. RUN_LIVE_TESTS=1 pytest -m live -s"""

import os

os.environ.setdefault("DB_PATH", "data/live-test.db")  # before app.config is imported anywhere

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

pytestmark = [pytest.mark.live,
              pytest.mark.skipif(os.getenv("RUN_LIVE_TESTS") != "1", reason="set RUN_LIVE_TESTS=1 with real keys")]


def test_live_discovery_returns_real_prospects():
    from app.main import app

    with TestClient(app) as client:
        response = client.post("/prospect-discovery?debug=true", json={"query": "Startup founders in London UK"})
    assert response.status_code == 200, response.text
    body = response.json()
    print({p["platform"]: (p["status"], p.get("found"), p.get("error")) for p in body["platforms"]})
    assert body["success"] is True
    assert any(p["status"] == "ok" for p in body["platforms"])
    for prospect in body["prospects"]:
        assert prospect["name"] and prospect["social_link"]
