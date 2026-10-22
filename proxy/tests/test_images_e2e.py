"""Multimodal request through a simulated extension: images reach /job."""
import asyncio

import httpx
import pytest

from conftest import FakeExtension

pytestmark = pytest.mark.anyio

PNG = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def multimodal_body():
    return {
        "model": "deepseek-chat",
        "stream": False,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What is on this screenshot?"},
                    {"type": "image_url", "image_url": {"url": PNG}, "filename": "shot.png"},
                ],
            }
        ],
    }


async def test_images_are_forwarded_to_the_extension(server):
    async with FakeExtension(server.url) as ext:
        async with httpx.AsyncClient(base_url=server.url, timeout=30) as c:
            r = await c.post("/v1/chat/completions", json=multimodal_body())
        assert r.status_code == 200, r.text
        assert r.json()["choices"][0]["message"]["content"].startswith("ECHO:")

        await asyncio.sleep(0.1)
        jobs = [j for j in ext.received if j["provider"] == "deepseek"]
        assert jobs, "the extension never received the job"
        job = jobs[-1]
        assert job["images"] == [{"url": PNG, "name": "shot.png"}]
        # the text part is still typed as a normal prompt
        assert "What is on this screenshot?" in job["question"]


async def test_text_only_request_has_no_images(server):
    async with FakeExtension(server.url) as ext:
        async with httpx.AsyncClient(base_url=server.url, timeout=30) as c:
            r = await c.post(
                "/v1/chat/completions",
                json={"model": "deepseek-chat", "stream": False,
                      "messages": [{"role": "user", "content": "hello"}]},
            )
        assert r.status_code == 200, r.text
        await asyncio.sleep(0.1)
        job = [j for j in ext.received if j["provider"] == "deepseek"][-1]
        assert job.get("images") is None


async def test_remote_image_url_is_rejected(server):
    body = multimodal_body()
    body["messages"][0]["content"][1]["image_url"]["url"] = "https://example.com/shot.png"
    async with FakeExtension(server.url):
        async with httpx.AsyncClient(base_url=server.url, timeout=30) as c:
            r = await c.post("/v1/chat/completions", json=body)
    assert r.status_code == 400
    assert r.json()["error"]["type"] == "invalid_image"
