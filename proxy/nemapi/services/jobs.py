"""Async job queue between API clients and the browser extension.

- One FIFO queue per provider; at most one job in flight per provider, while
  different providers run in parallel.
- Clients await an asyncio.Future (no blocked threads, no sleep-polling).
- The extension fetches work with GET /job (optionally long-polling).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional

from ..utils.continuation import continuation_prompt, is_truncated, stitch
from .logs import LogBuffer

PENDING, DISPATCHED, DONE, ERROR, CANCELLED = "pending", "dispatched", "done", "error", "cancelled"
ACTIVE_STATES = (PENDING, DISPATCHED)


def _image_fingerprints(images: Optional[List[Dict[str, str]]]) -> List[str]:
    """Identify the images of a request without keeping the megabytes around.

    The base64 is hashed: the dedup only needs to know whether the two
    requests carry the SAME pictures, and a digest is enough for that.
    """
    out: List[str] = []
    for img in images or []:
        url = (img or {}).get("url") or ""
        if not url:
            continue
        digest = hashlib.sha256(url.encode("utf-8", "replace")).hexdigest()[:32]
        out.append(f"{digest}:{(img or {}).get('name') or ''}")
    return out


@dataclass(eq=False)
class Job:
    question: str
    provider: str
    model: str
    fresh_chat: Optional[bool] = None
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: str = PENDING
    result: str = ""
    error: str = ""
    created_at: float = field(default_factory=time.time)
    dispatched_at: float = 0.0
    waiters: int = 0
    future: asyncio.Future = field(default_factory=lambda: asyncio.get_running_loop().create_future())
    # Optional hooks (used by the incremental-context mode). Both run synchronously:
    # on_dispatch(job) right before the job is handed to the extension (may rewrite
    # question / fresh_chat), on_complete(job) as soon as the job reaches a final state.
    on_dispatch: Optional[Callable[["Job"], None]] = None
    on_complete: Optional[Callable[["Job"], None]] = None
    parsed: Any = None  # post-processed answer, if a hook already computed it
    # set by on_dispatch when the answer is already known (nothing to type)
    local_result: Optional[str] = None
    typed: Optional[bool] = None  # reported by the extension on errors
    # called with the raw answer if the extension answers after a timeout / stop
    on_late_result: Optional[Callable[[str], None]] = None
    key: str = ""  # dedup key (original prompt, even if a hook rewrites `question`)
    # Answer cut off inside a tool call → continued in the same discussion by
    # child jobs; the parent stays active and receives the joined answer.
    parent: Optional["Job"] = None
    child: Optional["Job"] = None
    partial: str = ""
    continuations: int = 0
    alive_at: float = 0.0  # last sign of progress (timeouts count from here)
    allow_continuation: bool = False  # ask for the rest of an answer cut inside a tool call
    # OpenAI image_url parts, uploaded through the provider's file input
    images: List[Dict[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.key = self.key or self.question
        self.alive_at = self.alive_at or self.created_at

    @property
    def short_id(self) -> str:
        return self.id[:8]

    def _finish(self, status: str, result: str = "", error: str = "") -> None:
        self.status, self.result, self.error = status, result, error
        if self.on_complete is not None:
            hook, self.on_complete = self.on_complete, None
            try:
                hook(self)
            except Exception:  # a hook must never break the queue
                logging.getLogger("nemapi").exception("on_complete hook failed")
        if not self.future.done():
            self.future.set_result(status)


class JobTimeout(Exception):
    pass


class ClientGone(Exception):
    pass


class JobManager:
    MAX_JOBS = 250
    KEEP_FINISHED = 150

    def __init__(self, logs: LogBuffer, dedup_window: float = 2.5, max_continuations: int = 4):
        self.logs = logs
        self.max_continuations = max_continuations
        self.dedup_window = dedup_window
        self.jobs: "OrderedDict[str, Job]" = OrderedDict()
        self.queues: Dict[str, Deque[Job]] = {}
        self.current: Dict[str, Optional[Job]] = {}
        self._wakeup = asyncio.Event()
        self._rr = 0  # round-robin cursor across providers

    # ------------------------------------------------------------ creation
    def create(
        self,
        question: str,
        provider: str,
        model: str,
        fresh_chat: Optional[bool] = None,
        continuation: bool = False,
        images: Optional[List[Dict[str, str]]] = None,
    ) -> Job:
        now = time.time()
        image_ids = _image_fingerprints(images)
        for existing in reversed(self.jobs.values()):
            if now - existing.created_at > self.dedup_window:
                break  # jobs are ordered by creation time
            if (
                existing.status in ACTIVE_STATES
                and existing.provider == provider
                and existing.key == question
                # The images are part of the request: the same text with a
                # DIFFERENT image is another question, and reusing the job
                # would answer about the wrong picture.
                and _image_fingerprints(existing.images) == image_ids
            ):
                self.logs.info(f"Dedup: reuse job {existing.short_id} (identical in-flight request)")
                return existing
        job = Job(question=question, provider=provider, model=model, fresh_chat=fresh_chat)
        job.images = list(images or [])
        job.allow_continuation = continuation  # agent integration feature (off in classic mode)
        self.jobs[job.id] = job
        self.queues.setdefault(provider, deque()).append(job)
        self._gc()
        self._wakeup.set()
        return job

    # ------------------------------------------------------------ dispatch
    def take(self) -> Optional[Job]:
        """Next pending job for a provider that has nothing in flight (round-robin)."""
        providers = list(self.queues.keys())
        if not providers:
            return None
        n = len(providers)
        for i in range(n):
            provider = providers[(self._rr + i) % n]
            if self.current.get(provider) is not None:
                continue
            queue = self.queues[provider]
            while queue and queue[0].status != PENDING:
                queue.popleft()  # drop cancelled / timed-out entries
            if not queue:
                continue
            job = queue.popleft()
            job.status = DISPATCHED
            job.dispatched_at = time.time()
            job.alive_at = job.dispatched_at
            if job.parent is not None:
                job.parent.alive_at = job.dispatched_at
            self.current[provider] = job
            if job.on_dispatch is not None:
                try:
                    job.on_dispatch(job)
                except Exception:
                    logging.getLogger("nemapi").exception("on_dispatch hook failed")
            self._rr = (self._rr + i + 1) % n
            if job.local_result is not None:
                # answered without the browser: finish it and look for the next job
                job._finish(DONE, result=job.local_result)
                self._release(job)
                return self.take()
            return job
        return None

    async def take_wait(self, timeout: float, is_disconnected=None, poll: float = 0.25) -> Optional[Job]:
        """Long-poll variant of take(): wait up to `timeout` seconds for a job.

        `is_disconnected` is checked before handing out a job: a poller whose
        client went away (extension reloaded, service worker suspended…) must
        never "steal" a job, otherwise that job would be dispatched to nobody
        and block its provider.

        The wakeup event is CLEARED BEFORE the queue is inspected, never after:
        a job created between `take()` and `clear()` used to be missed, and the
        request then waited for the next poll tick (up to 500 ms) — on every
        request, for nothing.
        """
        deadline = time.monotonic() + timeout
        while True:
            if is_disconnected is not None and await is_disconnected():
                return None
            self._wakeup.clear()
            job = self.take()
            if job:
                return job
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=min(remaining, poll))
            except asyncio.TimeoutError:
                pass

    # ---------------------------------------------------------- completion
    def get(self, job_id: str) -> Optional[Job]:
        return self.jobs.get(job_id)

    def complete(self, job_id: str, action: str, result: str = "", error: str = "", typed: Optional[bool] = None) -> bool:
        job = self.jobs.get(job_id or "")
        if job and job.status not in ACTIVE_STATES and action == "result" and job.on_late_result is not None:
            hook, job.on_late_result = job.on_late_result, None
            try:
                hook(result or "")
            except Exception:
                logging.getLogger("nemapi").exception("on_late_result hook failed")
            return False
        if not job or job.status not in ACTIVE_STATES:
            return False
        job.typed = typed
        if job.parent is not None:
            return self._complete_part(job, action, result, error)
        if (
            action == "result" and self.max_continuations and job.allow_continuation
            and job.child is None and is_truncated(result)
        ):
            # cut off inside a tool call: keep the job active, free the provider
            # slot and ask the model to continue in the same discussion
            self._release(job)
            self._continue_or_finish(job, result or "")
            return True
        if job.child is not None and job.child.status in ACTIVE_STATES:
            self.cancel(job.child, "parent finished")
        if action == "result":
            job._finish(DONE, result=result or "")
        elif action == "cancelled":
            job._finish(CANCELLED, error=error or "cancelled")
        else:
            job._finish(ERROR, error=error or "automation failed")
        self._release(job)
        return True

    # -------------------------------------------------------- continuation
    def _continue_or_finish(self, parent: Job, text: str) -> None:
        if is_truncated(text) and parent.continuations < self.max_continuations:
            parent.partial = text
            parent.continuations += 1
            parent.alive_at = time.time()
            child = Job(
                question=continuation_prompt(text), provider=parent.provider, model=parent.model, fresh_chat=False
            )
            child.key = f"continuation:{child.id}"
            child.parent = parent
            parent.child = child
            self.jobs[child.id] = child
            # first in line: nothing else may be typed between the parts
            self.queues.setdefault(parent.provider, deque()).appendleft(child)
            self._wakeup.set()
            self.logs.warn(
                f"Job {parent.short_id}: answer cut off inside a tool call ({len(text)} chars) → "
                f"continuation {parent.continuations}/{self.max_continuations} in the same discussion"
            )
            return
        if is_truncated(text):
            self.logs.error(
                f"Job {parent.short_id}: tool call still incomplete after {parent.continuations} continuation(s) "
                "→ not executed"
            )
        elif parent.continuations:
            self.logs.info(f"Job {parent.short_id}: answer completed with {parent.continuations} continuation(s)")
        parent.child = None
        parent._finish(DONE, result=text)
        self._release(parent)

    def _complete_part(self, child: Job, action: str, result: str, error: str) -> bool:
        parent = child.parent
        if action == "result":
            child._finish(DONE, result=result or "")
        else:
            child._finish(CANCELLED if action == "cancelled" else ERROR, error=error or "automation failed")
        self._release(child)
        if parent is None or parent.status not in ACTIVE_STATES:
            return True
        parent.child = None
        if action == "result":
            self._continue_or_finish(parent, stitch(parent.partial, result or ""))
        else:
            self.logs.warn(f"Job {parent.short_id}: continuation failed ({error or action}) → partial answer kept")
            parent.continuations = self.max_continuations  # no new attempt
            self._continue_or_finish(parent, parent.partial)
        return True

    def cancel(self, job: Job, reason: str = "cancelled") -> None:
        if job.status not in ACTIVE_STATES:
            return
        if job.child is not None and job.child is not job:
            child, job.child = job.child, None
            self.cancel(child, reason)
        if job.status == PENDING:
            queue = self.queues.get(job.provider)
            if queue:
                try:
                    queue.remove(job)
                except ValueError:
                    pass
        job._finish(CANCELLED, error=reason)
        self._release(job)

    def stop_all(self) -> int:
        stopped = 0
        for job in list(self.jobs.values()):
            if job.status in ACTIVE_STATES:
                job._finish(CANCELLED, error="stopped")
                stopped += 1
        for queue in self.queues.values():
            queue.clear()
        for provider in self.current:
            self.current[provider] = None
        self._wakeup.set()
        return stopped

    def _release(self, job: Job) -> None:
        if self.current.get(job.provider) is job:
            self.current[job.provider] = None
            self._wakeup.set()  # a queued job for this provider may now be dispatched

    def _gc(self) -> None:
        if len(self.jobs) <= self.MAX_JOBS:
            return
        finished = [jid for jid, j in self.jobs.items() if j.status not in ACTIVE_STATES]
        for jid in finished[: max(0, len(finished) - self.KEEP_FINISHED)]:
            self.jobs.pop(jid, None)

    # ------------------------------------------------------------- waiting
    async def wait(self, job: Job, timeout: float, is_disconnected=None, poll: float = 0.5) -> Job:
        """Wait for a job result. Raises JobTimeout or ClientGone.

        If the HTTP client disconnects while the job is still queued (not yet
        sent to the browser), the job is cancelled so it never hits the web UI.
        """
        job.waiters += 1
        deadline = time.monotonic() + timeout
        try:
            while not job.future.done():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # an answer being continued is making progress: extend
                    idle = time.time() - job.alive_at
                    if idle < timeout:
                        deadline = time.monotonic() + (timeout - idle)
                        continue
                    raise JobTimeout()
                await asyncio.wait({job.future}, timeout=min(poll, remaining))
                if job.future.done():
                    break
                if is_disconnected is not None and await is_disconnected():
                    raise ClientGone()
            return job
        except ClientGone:
            if job.waiters <= 1 and job.status == PENDING:
                self.cancel(job, "client disconnected")
                self.logs.warn(f"Job {job.short_id} annulé (client déconnecté avant envoi)")
            raise
        finally:
            job.waiters -= 1

    # -------------------------------------------------------------- reaper
    def reap(self, max_age: float) -> int:
        """Fail jobs older than `max_age` seconds that are still active.

        Normally the waiting request times the job out itself, but when the
        client disconnected after dispatch nobody is waiting any more: without
        this, a job the extension silently dropped would block its provider
        forever.
        """
        now = time.time()
        expired = [
            j for j in self.jobs.values()
            if j.status in ACTIVE_STATES and now - max(j.created_at, j.alive_at) > max_age
        ]
        for job in expired:
            if job.status == PENDING:
                self.cancel(job, "expired in queue")
            else:
                self.complete(job.id, "error", error="timeout waiting for extension")
            self.logs.warn(f"Job {job.short_id} ({job.provider}) expiré après {int(max_age)}s → slot libéré")
        return len(expired)

    async def reaper_loop(self, max_age: float, interval: float = 5.0) -> None:
        while True:
            await asyncio.sleep(interval)
            self.reap(max_age)

    # ------------------------------------------------------------ snapshot
    def has_active(self) -> bool:
        return any(j is not None for j in self.current.values())

    def snapshot(self) -> Dict:
        queues = {}
        for p, q in self.queues.items():
            n = sum(1 for j in q if j.status == PENDING)
            if n:
                queues[p] = n
        return {
            "pending": sum(queues.values()),
            "queues": queues,
            "active": {p: j.short_id for p, j in self.current.items() if j is not None},
        }

    def active_jobs(self) -> List[Job]:
        return [j for j in self.current.values() if j is not None]
