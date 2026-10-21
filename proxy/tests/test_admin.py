"""Admin UI, static files, settings persistence and API keys."""
import json

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
