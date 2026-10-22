"""Unit tests of the async JobManager."""
import asyncio
import time

import pytest

from nemapi.services.jobs import CANCELLED, DISPATCHED, DONE, ERROR, ClientGone, JobManager, JobTimeout
from nemapi.services.logs import LogBuffer

pytestmark = pytest.mark.anyio


async def test_round_robin_and_one_per_provider():
    jm = JobManager(LogBuffer())
    a1 = jm.create("a1", "qwen", "m")
    a2 = jm.create("a2", "qwen", "m")
    b1 = jm.create("b1", "claude", "m")
    assert jm.take() is a1
    assert jm.take() is b1
    assert jm.take() is None  # qwen busy with a1, claude busy with b1
    jm.complete(a1.id, "result", result="ok")
    assert a1.status == DONE and a1.result == "ok"
    assert jm.take() is a2 and a2.status == DISPATCHED


async def test_reaper_frees_abandoned_dispatched_job():
    jm = JobManager(LogBuffer())
    job = jm.create("q", "qwen", "m")
    assert jm.take() is job
    job.created_at -= 100
    job.alive_at -= 100  # last progress (dispatch) too
    assert jm.reap(50) == 1
    assert job.status == ERROR and jm.current["qwen"] is None
    nxt = jm.create("q2", "qwen", "m")
    assert jm.take() is nxt


async def test_wait_timeout_and_client_gone():
    jm = JobManager(LogBuffer())
    job = jm.create("q", "qwen", "m")
    with pytest.raises(JobTimeout):
        await jm.wait(job, 0.2)

    job2 = jm.create("q2", "claude", "m")

    async def gone():
        return True

    with pytest.raises(ClientGone):
        await jm.wait(job2, 5, is_disconnected=gone, poll=0.05)
    assert job2.status == CANCELLED  # was still pending -> cancelled
    assert jm.take() is job  # job2 removed from the queue; job still pending


async def test_wait_returns_as_soon_as_completed():
    jm = JobManager(LogBuffer())
    job = jm.create("q", "qwen", "m")
    jm.take()
    asyncio.get_running_loop().call_later(0.05, jm.complete, job.id, "result", "fast")
    t0 = time.perf_counter()
    await jm.wait(job, 5, poll=1.0)
    assert time.perf_counter() - t0 < 0.3 and job.result == "fast"


async def test_dedup_window():
    jm = JobManager(LogBuffer(), dedup_window=0.2)
    a = jm.create("same", "qwen", "m")
    assert jm.create("same", "qwen", "m") is a
    assert jm.create("same", "claude", "m") is not a
    await asyncio.sleep(0.25)
    assert jm.create("same", "qwen", "m") is not a


async def test_dedup_takes_the_images_into_account():
    """The same text with a DIFFERENT image is another request.

    Reusing the job would answer about the wrong picture — a silent wrong
    answer, the kind that is impossible to debug from the client side.
    """
    jm = JobManager(LogBuffer(), dedup_window=5)
    shot_a = [{"url": "data:image/png;base64,AAAA", "name": "a.png"}]
    shot_b = [{"url": "data:image/png;base64,BBBB", "name": "b.png"}]

    first = jm.create("regarde", "qwen", "m", images=shot_a)
    # same text, same image -> the SDK retried, reuse the job
    assert jm.create("regarde", "qwen", "m", images=shot_a) is first
    # same text, DIFFERENT image -> another question
    assert jm.create("regarde", "qwen", "m", images=shot_b) is not first
    # same text, no image -> another question too
    assert jm.create("regarde", "qwen", "m") is not first


async def test_gc_bounds_memory():
    jm = JobManager(LogBuffer(), dedup_window=0)
    for i in range(400):
        j = jm.create(f"q{i}", "qwen", "m")
        jm.take()
        jm.complete(j.id, "result", "x")
    assert len(jm.jobs) <= JobManager.MAX_JOBS
