"""Admin UI, static files, settings persistence and API keys."""
import json
import time
from datetime import date, timedelta

import httpx


def test_pages_and_static(server):
    with httpx.Client(base_url=server.url) as c:
        for path in ["/", "/index.html", "/admin.html", "/config", "/chat", "/analytics", "/api-keys-page"]:
            r = c.get(path)
            assert r.status_code == 200, path
            assert r.headers["content-type"].startswith("text/html")
            assert "NemApi" in r.text
        assert c.get("/admin.css").headers["content-type"].startswith("text/css")
        assert c.get("/admin.js").status_code == 200
        assert c.get("/icons/logo-48.png").headers["content-type"] == "image/png"
        assert c.get("/icons/providers/claude.png").status_code == 200
        assert c.get("/extension/icons/logo-32.png").status_code == 200  # legacy path
        r = c.get("/does-not-exist")
        assert r.status_code == 404 and r.json()["error"]["type"] == "not_found"
        assert c.get("/../run.py").status_code == 404


def test_cors_preflight(server):
    r = httpx.options(
        server.url + "/v1/chat/completions",
        headers={
            "Origin": "chrome-extension://abc",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,authorization",
        },
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "*"


def test_status_and_stats_shape(server):
    with httpx.Client(base_url=server.url) as c:
        st = c.get("/status").json()
        assert st["status"] == "waiting_extension" and st["extension"] is False
        assert set(st["providers_ready"]) == {"deepseek", "qwen", "claude", "gemini", "chatgpt", "kimi", "zai"}
        assert st["jobs"] == {"pending": 0, "queues": {}, "active": {}}
        stats = c.get("/stats").json()
        assert stats["total_requests"] == 0 and "pricing" in stats and len(stats["providers"]) == 7
        assert c.get("/providers").json()["providers"][0]["id"] == "deepseek"
        assert c.post("/stats/clear").json()["ok"] is True


def test_the_reference_prices_are_complete_and_cited(server):
    """Every provider has an input, an output and a cache price, a label, a
    remark and a source: the analytics page shows them all."""
    from nemapi.core.providers import PRICING_CHECKED_AT, PRICING_SOURCES, PROVIDERS, TOKEN_PRICING

    with httpx.Client(base_url=server.url) as c:
        stats = c.get("/stats").json()
    pricing = stats["pricing"]
    assert set(pricing) == set(PROVIDERS) == set(TOKEN_PRICING)
    assert stats["pricing_checked_at"] == PRICING_CHECKED_AT
    for pid, entry in pricing.items():
        assert entry["prompt_per_1k"] > 0, pid
        assert entry["completion_per_1k"] > 0, pid
        assert entry["cached_per_1k"] > 0, pid
        assert entry["label"], pid
        assert entry["note"], pid
        assert entry["source"] == PRICING_SOURCES[pid], pid


def test_the_stats_keep_a_daily_history(server):
    """The analytics chart needs one bucket per day, with the empty days
    filled in (a gap must read as "nothing that day")."""
    from nemapi.services.stats import HISTORY_DAYS

    with httpx.Client(base_url=server.url) as c:
        stats = c.get("/stats").json()
    days = stats["daily"]
    assert len(days) == HISTORY_DAYS
    assert stats["history_days"] == HISTORY_DAYS
    assert [d["date"] for d in days] == sorted(d["date"] for d in days)
    assert days[-1]["date"] == date.today().isoformat()
    assert all(d["requests"] == 0 for d in days)


def test_a_request_lands_in_the_daily_bucket(tmp_path):
    """The counters and the daily bucket move together, and the history is
    capped at HISTORY_DAYS days."""
    from nemapi.services.logs import LogBuffer
    from nemapi.services.stats import HISTORY_DAYS, StatsStore

    store = StatsStore(tmp_path / "stats.json", LogBuffer())
    store.record("deepseek", 1000, 200)
    store.record("claude", 500, 100)
    report = store.report(time.time(), "4.1")
    today = report["daily"][-1]
    assert today["requests"] == 2
    assert today["prompt_tokens"] == 1500 and today["completion_tokens"] == 300
    assert report["total_requests"] == 2
    assert report["estimated_cost"] > 0

    # an old day is dropped, the bucket of today is kept
    old = (date.today() - timedelta(days=HISTORY_DAYS + 5)).isoformat()
    store._data["daily"][old] = {"requests": 1, "prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0}
    store.record("qwen", 10, 10)
    days = store.report(time.time(), "4.1")["daily"]
    assert old not in store._data["daily"]
    assert len(days) == HISTORY_DAYS
    assert all(d["requests"] == 0 for d in days[:-1])
    assert days[-1]["requests"] == 3


def test_settings_persist_across_restart(server_factory, tmp_path):
    data = tmp_path / "persist"
    srv = server_factory(data_dir=data)
    with httpx.Client(base_url=srv.url) as c:
        assert c.get("/settings").json()["fresh_chat"] is True
        r = c.post("/settings", json={"fresh_chat": False, "stream_enabled": False, "auto_config": False})
        assert r.json() == {"stream_enabled": False, "auto_config": False, "fresh_chat": False, "premium_md": True}
        # empty body is accepted (no-op)
        assert c.post("/settings").status_code == 200
    srv.stop()
    saved = json.loads((data / "config.json").read_text())
    assert saved["settings"]["fresh_chat"] is False
    srv2 = server_factory(data_dir=data)
    with httpx.Client(base_url=srv2.url) as c:
        assert c.get("/settings").json() == {
            "stream_enabled": False, "auto_config": False, "fresh_chat": False, "premium_md": True,
        }


def test_api_keys_lifecycle(server):
    body = {"model": "qwen-chat", "messages": [{"role": "user", "content": "hi"}]}
    with httpx.Client(base_url=server.url) as c:
        assert c.get("/api-keys/config").json() == {"enabled": False, "require_auth": False, "key_count": 0}
        # no protection: request reaches the extension check (503), not 401
        assert c.post("/v1/chat/completions", json=body).status_code == 503

        r = c.post("/api-keys/enable").json()
        key = r["api_key"]
        assert key.startswith("nemapi-token")
        assert c.post("/v1/chat/completions", json=body).status_code == 401
        assert c.get("/v1/models").status_code == 401
        assert c.post("/v1/chat/completions", json=body, headers={"Authorization": "Bearer wrong"}).status_code == 401
        ok = {"Authorization": f"Bearer {key}"}
        assert c.get("/v1/models", headers=ok).status_code == 200
        assert c.get("/v1/models", headers={"x-api-key": key}).status_code == 200
        assert c.post("/v1/chat/completions", json=body, headers=ok).status_code == 503

        k2 = c.post("/api-keys", json={"name": "cursor"}).json()
        listing = c.get("/api-keys").json()
        assert listing["require_auth"] is True and len(listing["api_keys"]) == 2
        assert all("key" not in k for k in c.get("/config-full").json()["api_keys"]["keys"])

        # disable keeps a backup; enable restores it instead of creating a new key
        assert c.post("/api-keys/disable").json()["ok"] is True
        assert c.get("/api-keys/config").json()["enabled"] is False
        r = c.post("/api-keys/enable").json()
        assert r["message"] == "Protection restaurée" and r["key_count"] == 2
        assert c.get("/v1/models", headers=ok).status_code == 200

        # delete via POST (UI) and DELETE (REST)
        assert c.post(f"/api-keys/{k2['key_id']}").json()["ok"] is True
        assert c.delete(f"/api-keys/{k2['key_id']}").status_code == 404
        assert c.post("/api-keys/restore").status_code == 404


def test_job_result_extraction_diagnostics(server):
    """The extension reports how it read the answer; lossy reads are flagged once."""
    with httpx.Client(base_url=server.url) as c:
        for _ in range(2):
            assert c.post("/job", json={"jobId": "nope", "action": "result", "result": "x",
                                        "source": "html", "bridge": False}).json() == {"ok": True}
        st = c.get("/status").json()
        assert st["last_extraction"]["source"] == "html" and st["last_extraction"]["bridge"] is False
        logs = json.dumps(c.get("/extension/state").json()["logs"])
        assert logs.count("page bridge missing") == 1  # rate-limited
        c.post("/job", json={"jobId": "nope", "action": "result", "result": "x", "source": "react", "bridge": True})
        assert c.get("/status").json()["last_extraction"]["source"] == "react"
