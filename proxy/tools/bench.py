#!/usr/bin/env python3
"""Mini benchmark of a running NemApi proxy using a simulated browser extension.

    python tools/bench.py --url http://127.0.0.1:8090 --requests 200 --concurrency 20

WARNING: do not run against a proxy connected to the real extension — the fake
extension below would compete for jobs. Use a dedicated instance:
    NEMAPI_DATA_DIR=/tmp/nemapi-bench python run.py --port 8099
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import time

import httpx

PROVIDERS = ["deepseek", "qwen", "claude", "gemini", "chatgpt", "kimi", "zai"]
EXT_ERRORS: list = []
MODELS = ["deepseek-chat", "qwen-chat", "claude-chat", "gemini-chat", "gpt-chat", "kimi-chat", "glm-chat"]


async def fake_extension(url: str, stop: asyncio.Event, answer: str, long_poll: float):
    tabs = [{"id": 100 + i, "provider": p, "url": "https://x/", "title": p} for i, p in enumerate(PROVIDERS)]
    async with httpx.AsyncClient(base_url=url, timeout=30) as c:
        await c.post("/extension/tabs", json={"tabs": tabs, "busy": False})
        last_report = time.time()
        pending = set()  # keep task refs (un-referenced tasks can be GC'd)
        while not stop.is_set():
            if time.time() - last_report > 1:
                await c.post("/extension/tabs", json={"tabs": tabs, "busy": False})
                last_report = time.time()
            try:
                job = (await c.get("/job", params={"wait": long_poll})).json()
            except (httpx.HTTPError, ValueError) as exc:
                EXT_ERRORS.append(f"poll: {exc!r}")
                continue
            if job.get("action") != "ask":
                if not long_poll:
                    await asyncio.sleep(0.005)
                continue

            async def reply(j=job):
                try:
                    await c.post("/job", json={"jobId": j["jobId"], "action": "result", "result": answer})
                except httpx.HTTPError as exc:
                    EXT_ERRORS.append(f"post: {exc!r}")

            t = asyncio.create_task(reply())
            pending.add(t)
            t.add_done_callback(pending.discard)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8099")
    ap.add_argument("--requests", type=int, default=200)
    ap.add_argument("--concurrency", type=int, default=20)
    ap.add_argument("--answer-size", type=int, default=4000, help="chars in each fake answer")
    ap.add_argument("--stream", action="store_true")
    ap.add_argument("--long-poll", type=float, default=2.0, help="0 = legacy short polling")
    args = ap.parse_args()

    stop = asyncio.Event()
    answer = ("NemApi bench " * (args.answer_size // 13 + 1))[: args.answer_size]
    ext = asyncio.create_task(fake_extension(args.url, stop, answer, args.long_poll))
    await asyncio.sleep(0.5)

    latencies = []
    errors = 0
    sem = asyncio.Semaphore(args.concurrency)
    async with httpx.AsyncClient(base_url=args.url, timeout=60, limits=httpx.Limits(max_connections=200)) as c:
        async def one(i: int):
            nonlocal errors
            async with sem:
                body = {
                    "model": MODELS[i % len(MODELS)],
                    "messages": [{"role": "user", "content": f"question {i}"}],
                    "stream": args.stream,
                }
                t0 = time.perf_counter()
                try:
                    r = await c.post("/v1/chat/completions", json=body)
                    _ = r.text
                    ok = r.status_code == 200
                except httpx.HTTPError:
                    ok = False
                latencies.append(time.perf_counter() - t0)
                if not ok:
                    errors += 1

        t0 = time.perf_counter()
        await asyncio.gather(*(one(i) for i in range(args.requests)))
        total = time.perf_counter() - t0

    stop.set()
    ext.cancel()
    lat = sorted(latencies)
    print(
        f"{args.requests} req, conc={args.concurrency}, stream={args.stream}, answer={args.answer_size}c → "
        f"{total:.2f}s total, {args.requests / total:.1f} req/s, "
        f"p50={statistics.median(lat) * 1000:.0f}ms p95={lat[int(len(lat) * 0.95) - 1] * 1000:.0f}ms, errors={errors}"
    )
    if EXT_ERRORS:
        print(f"fake-extension errors: {len(EXT_ERRORS)} e.g. {EXT_ERRORS[:3]}")


if __name__ == "__main__":
    asyncio.run(main())
