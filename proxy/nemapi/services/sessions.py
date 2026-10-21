"""Incremental context: ONE web discussion per provider, only new content is typed.

NemApi is a personal proxy: one agent talks to one provider tab. In incremental
mode the proxy NEVER opens a new discussion (``freshChat`` is always false):

* first request (no session yet) → the full prompt (system prompt, tools
  contract, history) is typed into the discussion currently open in the tab;
* every following request → the history is compared with what the discussion
  already contains and only the missing messages are typed (duplicates are
  filtered out, see ``utils.history.select_new``). A changed system prompt
  only sends its new lines, new/changed tools only their definition;
* nothing new at all (agent retry of an already answered turn) → nothing is
  typed, the answer already given in the discussion is returned again.

Decisions are taken at DISPATCH time (when the extension picks the job up):
every earlier job of the provider has finished by then, so the session state
is exact. The session state is persisted in ``data/sessions.json`` so a proxy
restart does not re-type the whole history into the discussion.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..utils.history import (
    BACK_TO_MAIN,
    MAIN_START,
    NUDGE,
    SIDE_SYSTEM_TITLE,
    side_request_prompt,
    History,
    format_delta_prompt,
    message_fingerprint,
    select_new,
    system_line_fingerprints,
    tool_fingerprints,
)
from ..utils.jsonfile import read_json, write_json_atomic
from ..utils.continuation import NOTE_NOT_EXECUTED, find_truncated_call
from ..utils.tools_format import clean_assistant_text, parse_tool_calls
from .extension import ExtensionState
from .logs import LogBuffer


@dataclass
class Session:
    provider: str
    tab_id: Optional[int] = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    view: List[str] = field(default_factory=list)  # agent's history after our last answer
    sent: List[str] = field(default_factory=list)  # every message fingerprint in the discussion
    answers: List[str] = field(default_factory=list)  # assistant fingerprints in the discussion
    system_lines: List[str] = field(default_factory=list)  # system-prompt line fingerprints typed
    tools: List[str] = field(default_factory=list)  # tool fingerprints typed
    last_answer: Optional[str] = None  # raw text of the last answer (for retries)
    awaiting: bool = False  # last typed message got no answer (timeout / stop)
    # profile of the MAIN conversation (system-prompt line fps + tool names):
    # requests that do not match it are side requests of the agent (sub-agents…)
    profile_sys: List[str] = field(default_factory=list)
    profile_tools: List[str] = field(default_factory=list)
    lanes: List[Dict[str, Any]] = field(default_factory=list)  # side requests typed in the discussion
    current_lane: str = "main"  # lane of the last message typed into the discussion
    main_started: bool = True  # False while the discussion only holds side requests
    side_turns: int = 0
    turns: int = 0
    delta_turns: int = 0
    cached_turns: int = 0
    chars_saved: int = 0
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    # runtime only (not persisted)
    in_flight: Optional[str] = None
    in_flight_job: Any = None
    in_flight_chain: List[str] = field(default_factory=list)

    PERSISTED = (
        "provider", "tab_id", "id", "view", "sent", "answers", "system_lines", "tools", "last_answer",
        "awaiting", "profile_sys", "profile_tools", "lanes", "current_lane", "main_started", "side_turns", "turns", "delta_turns", "cached_turns", "chars_saved", "created_at", "last_used",
    )

    def to_json(self) -> Dict[str, Any]:
        d = asdict(self)
        return {k: d[k] for k in self.PERSISTED}

    @classmethod
    def from_json(cls, d: Dict[str, Any]) -> "Session":
        sess = cls(**{k: d[k] for k in cls.PERSISTED if k in d})
        for name in ("view", "sent", "answers", "system_lines", "tools", "profile_sys", "profile_tools"):
            value = getattr(sess, name)
            if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
                raise ValueError(f"invalid field {name}")
        lanes = []
        for lane in sess.lanes if isinstance(sess.lanes, list) else []:
            try:
                lanes.append(Lane.from_json(lane).to_json())
            except (TypeError, ValueError, KeyError, AttributeError):
                continue  # a broken side lane is simply forgotten
        sess.lanes = lanes
        if not isinstance(sess.current_lane, str):
            sess.current_lane = "main"
        if not isinstance(sess.provider, str) or (sess.last_answer is not None and not isinstance(sess.last_answer, str)):
            raise ValueError("invalid session")
        for name in ("turns", "delta_turns", "cached_turns", "chars_saved", "side_turns"):
            if not isinstance(getattr(sess, name), int):
                setattr(sess, name, 0)
        for name in ("created_at", "last_used"):
            if not isinstance(getattr(sess, name), (int, float)):
                setattr(sess, name, time.time())
        sess.awaiting = bool(sess.awaiting)
        sess.main_started = bool(sess.main_started)
        return sess


@dataclass
class Lane:
    """A side request stream of the agent typed into the same discussion."""

    id: str
    label: str = ""
    sys: List[str] = field(default_factory=list)  # system-prompt line fps (profile)
    tools: List[str] = field(default_factory=list)  # tool names (profile)
    view: List[str] = field(default_factory=list)
    last_answer: Optional[str] = None
    awaiting: bool = False
    turns: int = 0
    last_used: float = field(default_factory=time.time)

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, d: Dict[str, Any]) -> "Lane":
        lane = cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})
        for name in ("sys", "tools", "view"):
            v = getattr(lane, name)
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                raise ValueError(name)
        if not isinstance(lane.id, str) or (lane.last_answer is not None and not isinstance(lane.last_answer, str)):
            raise ValueError("lane")
        lane.label = str(lane.label or "")
        lane.awaiting = bool(lane.awaiting)
        lane.turns = lane.turns if isinstance(lane.turns, int) else 0
        lane.last_used = lane.last_used if isinstance(lane.last_used, (int, float)) else time.time()
        return lane


MAX_LANES = 12
MATCH = 0.6  # minimal similarity (system prompt AND tools) to belong to a lane


def _similarity(a: List[str], b: List[str], slack: int) -> float:
    """Share of common items; up to `slack` differing items are tolerated
    (dynamic system-prompt lines: date, cwd, git status… / one added tool)."""
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    return min(1.0, len(sa & sb) / max(1, max(len(sa), len(sb)) - slack))


def _sys_similarity(req: List[str], ref: List[str]) -> float:
    return _similarity(req, ref, slack=3)


def _tools_similarity(a: List[str], b: List[str]) -> float:
    return _similarity(a, b, slack=2)


def _lane_label(system_text: str, h: History) -> str:
    for line in (system_text or "").splitlines():
        line = line.strip().lstrip("#").strip()
        if len(line) > 3:
            return line[:70]
    for p in range(len(h.chain) - 1, -1, -1):
        if h.roles[p] == "user":
            from ..utils.tools_format import _extract_text

            text = " ".join(_extract_text(h.message(p).get("content")).split())
            if text:
                return text[:70]
    return "requête annexe"


@dataclass
class Plan:
    """Everything needed to build the prompt when the job is dispatched."""

    provider: str
    history: History
    tools: Optional[List[Any]]
    tool_names: List[str]
    full_prompt: str
    # filled at dispatch
    mode: str = ""  # "full" | "delta" | "cached" | "nudge"
    session: Optional[Session] = None
    typed: List[str] = field(default_factory=list)  # fingerprints typed by this job
    new_system: List[str] = field(default_factory=list)
    new_tools: List[str] = field(default_factory=list)
    lane: Optional[str] = None  # None = main conversation, else side lane id
    force_side: bool = False  # known side query of the agent (typed as a side request)


@dataclass
class Parsed:
    text: str
    tool_calls: List[Dict[str, Any]]


def postprocess(raw: str, names: Optional[List[str]], tools: Optional[List[Any]] = None) -> Parsed:
    text = clean_assistant_text(raw or "")
    cut = find_truncated_call(text)
    if cut is not None:
        # a tool call that never ended (output limit): executing a "repaired"
        # version would write a truncated file and report success — never
        remaining, calls = parse_tool_calls(text[:cut], names or None, tools)
        return Parsed((remaining + "\n\n" + NOTE_NOT_EXECUTED).strip(), calls)
    remaining, calls = parse_tool_calls(text, names or None, tools)
    return Parsed(remaining, calls)


class SessionManager:
    def __init__(self, extension: ExtensionState, logs: LogBuffer, path: Optional[Path] = None):
        self.extension = extension
        self.logs = logs
        self.path = path
        self.active: Dict[str, Session] = {}
        self.counters = {
            "full": 0, "delta": 0, "cached": 0, "nudge": 0, "side": 0, "local": 0,
            "chars_sent": 0, "chars_full": 0,
        }
        # an identical request within this delay is a retry (answer reused),
        # after it, it is a new coding session that starts the same way
        self.retry_window = 300.0
        self._load()

    # --------------------------------------------------------- persistence
    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            data = read_json(self.path)
            items = (data.get("sessions") or {}).items()
        except Exception as exc:  # corrupted file: start empty, never crash
            self.logs.warn(f"Contexte incrémental: sessions.json illisible ({exc}), ignoré")
            return
        for p, d in items:
            try:
                self.active[str(p)] = Session.from_json(d)
            except Exception as exc:
                self.logs.warn(f"Contexte incrémental: session {p} invalide dans sessions.json ({exc}), ignorée")

    def save(self) -> None:
        if not self.path:
            return
        try:
            write_json_atomic(self.path, {"sessions": {p: s.to_json() for p, s in self.active.items()}})
        except OSError as exc:
            self.logs.warn(f"Contexte incrémental: sauvegarde impossible ({exc})")

    # ------------------------------------------------------------ planning
    def plan(self, provider: str, messages, tools, full_prompt: str, tool_names: List[str],
             force_side: bool = False) -> Plan:
        return Plan(
            provider=provider,
            history=History.from_messages(messages),
            tools=tools,
            tool_names=list(tool_names or []),
            full_prompt=full_prompt,
            force_side=force_side,
        )

    # ------------------------------------------------------------ dispatch
    def on_dispatch(self, job, plan: Plan) -> None:
        """Called synchronously by JobManager.take(): rewrites job.question.

        Never opens a new discussion: job.fresh_chat is always False.
        """
        provider = plan.provider
        h = plan.history
        job.fresh_chat = False
        self.counters["chars_full"] += len(plan.full_prompt)
        sess = self.active.get(provider)
        tab = self.extension.tab_for(provider)

        sys_fps = system_line_fingerprints(h.system_text)
        tool_fps = tool_fingerprints(plan.tools)
        req_sys, req_tools = list(sys_fps), sorted(set(plan.tool_names))

        lane: Optional[Lane] = None
        start_main = False
        if sess is None and plan.force_side:
            # the agent's very first request is a side query: the discussion starts
            # with it (framed); the main conversation will be typed in full later
            sess = Session(provider=provider, tab_id=tab, main_started=False)
            self.active[provider] = sess
        if sess is not None and plan.force_side:
            lane = self._side_lane(sess, req_sys, req_tools, h)
        elif sess is not None and not sess.main_started:
            start_main = True
        elif sess is not None:
            if not sess.profile_sys and not sess.profile_tools:
                # session persisted by an older version: adopt this request as the main profile
                sess.profile_sys, sess.profile_tools = req_sys, req_tools
            if self._matches_main(sess, req_sys, req_tools):
                if self._new_conversation(sess, h):
                    # The user started a new coding session (and opened a new discussion
                    # in the browser himself): the agent's history holds no answer at all
                    # and does not continue the previous one → send everything again,
                    # in the discussion that is open (still never a new chat from here).
                    self.logs.info(
                        f"Contexte incrémental ({provider}): nouvelle conversation de l'agent détectée "
                        f"→ prompt complet (prompt système, outils, message) dans la discussion ouverte"
                    )
                    self.active.pop(provider, None)
                    sess = None
            else:
                lane = self._side_lane(sess, req_sys, req_tools, h)

        if sess is None:
            sess = Session(provider=provider, tab_id=tab, profile_sys=req_sys, profile_tools=req_tools)
            self.active[provider] = sess
            job.question = plan.full_prompt
            plan.mode = "full"
            plan.typed = list(h.chain)
            plan.new_system = list(sys_fps)
            plan.new_tools = list(tool_fps)
            self.logs.info(
                f"Contexte incrémental: 1re requête ({provider}) → prompt complet "
                f"{len(plan.full_prompt)}c dans la discussion ouverte"
            )
        elif start_main:
            # first request of the main conversation after side requests only
            sess.main_started = True
            sess.profile_sys, sess.profile_tools = req_sys, req_tools
            job.question = MAIN_START + "\n\n" + plan.full_prompt
            plan.mode = "full"
            plan.typed = list(h.chain)
            plan.new_system = list(sys_fps)
            plan.new_tools = list(tool_fps)
            self.logs.info(
                f"Contexte incrémental ({provider}): début de la conversation principale → prompt complet "
                f"{len(plan.full_prompt)}c (après des requêtes annexes)"
            )
        else:
            if tab is not None and sess.tab_id is not None and tab != sess.tab_id:
                self.logs.warn(
                    f"Contexte incrémental: l'onglet {provider} a changé ; on continue la session "
                    f"(Réinitialiser si la discussion ouverte est différente)"
                )
            sess.tab_id = tab if tab is not None else sess.tab_id
            if lane is None:
                sess.profile_sys, sess.profile_tools = req_sys, req_tools  # follow small changes
            view = lane.view if lane is not None else sess.view
            positions = select_new(h, view, sess.sent, sess.answers)
            new_sys = [fp for fp in sys_fps if fp not in set(sess.system_lines)]
            new_tools = [fp for fp in tool_fps if fp not in set(sess.tools)]
            last_answer = lane.last_answer if lane is not None else sess.last_answer
            awaiting = lane.awaiting if lane is not None else sess.awaiting
            plan.lane = lane.id if lane is not None else None
            if lane is not None:
                self._store_lane(sess, lane)
            where = f" [requête annexe «{lane.label}»]" if lane is not None else ""

            if positions:
                if lane is not None:
                    body = format_delta_prompt(
                        h, positions, plan.tools,
                        new_tools=[tool_fps[fp] for fp in new_tools],
                        new_system_lines=[sys_fps[fp] for fp in new_sys],
                        system_title=SIDE_SYSTEM_TITLE,
                        reminder=False,
                    )
                    job.question = side_request_prompt(body, plan.tool_names, lane.label)
                else:
                    job.question = format_delta_prompt(
                        h, positions, plan.tools,
                        new_tools=[tool_fps[fp] for fp in new_tools],
                        new_system_lines=[sys_fps[fp] for fp in new_sys],
                    )
                    if sess.current_lane != "main":
                        job.question = BACK_TO_MAIN + "\n\n" + job.question
                plan.mode = "delta"
                plan.typed = [h.chain[p] for p in positions]
                plan.new_system, plan.new_tools = new_sys, new_tools
                saved = max(0, len(plan.full_prompt) - len(job.question))
                sess.chars_saved += saved
                self.logs.info(
                    f"Contexte incrémental ({provider}){where}: {len(positions)} nouveau(x) message(s), "
                    f"{len(h.chain) - len(positions)} déjà présent(s) filtré(s) → "
                    f"{len(job.question)}c au lieu de {len(plan.full_prompt)}c"
                    + (f", +{len(new_sys)} ligne(s) système" if new_sys else "")
                    + (f", +{len(new_tools)} outil(s)" if new_tools else "")
                )
            elif last_answer is not None and not awaiting:
                # everything is already in the discussion and answered: do not type anything
                plan.mode = "cached"
                job.local_result = last_answer
                sess.chars_saved += len(plan.full_prompt)
                self.logs.info(f"Contexte incrémental ({provider}){where}: rien de nouveau → réponse déjà donnée renvoyée")
            else:
                # the last typed message never got an answer (timeout / stop): ask again
                job.question = NUDGE
                plan.mode = "nudge"
                self.logs.info(f"Contexte incrémental ({provider}){where}: rien de nouveau, réponse manquante → relance courte")

        self.counters[plan.mode] += 1
        if plan.lane is not None:
            self.counters["side"] += 1
        plan.session = sess
        if plan.mode != "cached":
            self.counters["chars_sent"] += len(job.question)
            sess.in_flight = job.id
            sess.in_flight_job = job
            sess.in_flight_chain = list(h.chain)
        sess.last_used = time.time()

    # --------------------------------------------------------------- lanes
    @staticmethod
    def _matches_main(sess: Session, req_sys: List[str], req_tools: List[str]) -> bool:
        return (
            _sys_similarity(req_sys, sess.profile_sys) >= MATCH
            and _tools_similarity(req_tools, sess.profile_tools) >= MATCH
        )

    def _side_lane(self, sess: Session, req_sys: List[str], req_tools: List[str], h: History) -> Lane:
        """Lane of a request that is not the main conversation (created if needed)."""
        best, best_score = None, 0.0
        for i, d in enumerate(sess.lanes):
            ss = _sys_similarity(req_sys, d.get("sys") or [])
            ts = _tools_similarity(req_tools, d.get("tools") or [])
            if ss >= MATCH and ts >= MATCH and ss + ts > best_score:
                best, best_score = i, ss + ts
        if best is not None:
            lane = Lane.from_json(sess.lanes[best])
            lane.sys, lane.tools = req_sys, req_tools
            return lane
        lane = Lane(id=uuid.uuid4().hex[:6], label=_lane_label(h.system_text, h), sys=req_sys, tools=req_tools)
        self.logs.info(
            f"Contexte incrémental ({sess.provider}): requête annexe de l'agent «{lane.label}» "
            f"(prompt système / outils différents de la conversation principale) → tapée dans la même "
            f"discussion, encadrée, sans toucher à la conversation principale"
        )
        return lane

    @staticmethod
    def _store_lane(sess: Session, lane: Lane) -> None:
        lane.last_used = time.time()
        for i, d in enumerate(sess.lanes):
            if d.get("id") == lane.id:
                sess.lanes[i] = lane.to_json()
                break
        else:
            sess.lanes.append(lane.to_json())
        if len(sess.lanes) > MAX_LANES:
            sess.lanes.sort(key=lambda d: d.get("last_used") or 0)
            del sess.lanes[: len(sess.lanes) - MAX_LANES]

    @staticmethod
    def _get_lane(sess: Session, lane_id: Optional[str]) -> Optional[Lane]:
        if lane_id is None:
            return None
        for d in sess.lanes:
            if d.get("id") == lane_id:
                return Lane.from_json(d)
        return None

    def _new_conversation(self, sess: Session, h: History) -> bool:
        if any(r == "assistant" for r in h.roles):
            return False  # continues a conversation (ours or compacted)
        view, chain = sess.view, h.chain
        if len(chain) >= len(view) and chain[: len(view)] == view:
            return False  # continues the current discussion (e.g. first answer was lost)
        if chain and view[: len(chain)] == chain and time.time() - sess.last_used < self.retry_window:
            return False  # agent retry of the first request
        return True

    # ---------------------------------------------------------- completion
    def on_complete(self, job, plan: Plan) -> None:
        """Called synchronously when the job finishes (success, error, cancel…)."""
        sess = plan.session
        if sess is None:
            return  # never reached the browser: nothing changed in the web UI
        if sess.in_flight == job.id:
            sess.in_flight, sess.in_flight_job, sess.in_flight_chain = None, None, []
        lane = self._lane_for(sess, plan)

        if job.status == "done":
            parsed = postprocess(job.result, plan.tool_names, plan.tools)
            job.parsed = parsed
            if plan.mode != "cached":
                self._record_typed(sess, plan)
                self._record_answer(sess, plan, job.result, parsed, lane)
                sess.current_lane = lane.id if lane is not None else "main"
                if lane is not None:
                    lane.turns += 1
                    sess.side_turns += 1
                else:
                    sess.turns += 1
                    if plan.mode == "delta":
                        sess.delta_turns += 1
            else:
                sess.cached_turns += 1
                view = list(plan.history.chain) + [self._answer_fp(parsed)]
                if lane is not None:
                    lane.view = view
                else:
                    sess.view = view
            if lane is not None:
                self._store_lane(sess, lane)
            self.save()
            return

        if job.typed is not None:  # the extension knows whether the prompt was submitted
            typed_for_sure = job.typed
        else:
            err = (job.error or "").lower()
            typed_for_sure = job.status == "cancelled" or any(w in err for w in ("timeout", "watchdog", "stopped"))
        if typed_for_sure:
            # the prompt was typed but no answer was captured: remember it so it is
            # not typed twice; the next turn asks for the missing answer instead
            self._record_typed(sess, plan)
            sess.current_lane = lane.id if lane is not None else "main"
            if lane is not None:
                lane.view, lane.awaiting = list(plan.history.chain), True
                self._store_lane(sess, lane)
            else:
                sess.view = list(plan.history.chain)  # typed, so never typed again
                sess.awaiting = True
            job.on_late_result = lambda raw, j=job: self.on_late_result(j, plan, raw)
            self.logs.warn(f"Contexte incrémental ({plan.provider}): pas de réponse capturée ({job.status})")
        else:
            # extension error: the text was most likely not sent → it will be typed again next time
            self.logs.warn(f"Contexte incrémental ({plan.provider}): échec ({job.error}) → contenu renvoyé au prochain tour")
        self.save()

    def _lane_for(self, sess: Session, plan: Plan) -> Optional[Lane]:
        if plan.lane is None:
            return None
        lane = self._get_lane(sess, plan.lane)
        if lane is None:  # first turn of a new lane: rebuild it from the plan
            h = plan.history
            lane = Lane(
                id=plan.lane,
                label=_lane_label(h.system_text, h),
                sys=list(system_line_fingerprints(h.system_text)),
                tools=sorted(set(plan.tool_names)),
            )
        return lane

    def on_late_result(self, job, plan: Plan, raw: str) -> None:
        """The extension answered after the proxy gave up (timeout / stop)."""
        sess = plan.session
        if sess is None or self.active.get(plan.provider) is not sess:
            return
        lane = self._lane_for(sess, plan)
        if not (lane.awaiting if lane is not None else sess.awaiting):
            return
        parsed = postprocess(raw, plan.tool_names, plan.tools)
        self._record_answer(sess, plan, raw, parsed, lane)
        if lane is not None:
            lane.turns += 1
            self._store_lane(sess, lane)
        else:
            sess.turns += 1
        self.logs.info(f"Contexte incrémental ({plan.provider}): réponse tardive enregistrée pour une relance")
        self.save()

    @staticmethod
    def _answer_fp(parsed: Parsed) -> str:
        return message_fingerprint(
            {"role": "assistant", "content": parsed.text or None, "tool_calls": parsed.tool_calls or None}
        )

    @staticmethod
    def _record_typed(sess: Session, plan: Plan) -> None:
        if plan.mode == "full":
            # the full prompt also contains earlier assistant messages of the history
            sess.sent.extend(plan.history.chain)
            sess.answers.extend(fp for fp, r in zip(plan.history.chain, plan.history.roles) if r == "assistant")
        elif plan.mode == "delta":
            sess.sent.extend(plan.typed)
        sess.system_lines.extend(fp for fp in plan.new_system if fp not in sess.system_lines)
        sess.tools.extend(fp for fp in plan.new_tools if fp not in sess.tools)

    def _record_answer(self, sess: Session, plan: Plan, raw: str, parsed: Parsed, lane: Optional[Lane] = None) -> None:
        fp = self._answer_fp(parsed)
        sess.sent.append(fp)
        sess.answers.append(fp)
        view = list(plan.history.chain) + [fp]
        if lane is not None:
            lane.view, lane.last_answer, lane.awaiting = view, raw, False
        else:
            sess.view, sess.last_answer, sess.awaiting = view, raw, False
        sess.last_used = time.time()

    def count_local(self) -> None:
        self.counters["local"] += 1

    # ------------------------------------------------------------- retries
    def find_inflight(self, plan: Plan):
        """Job currently answering exactly this history (agent retry while the web UI is busy)."""
        sess = self.active.get(plan.provider)
        if sess and sess.in_flight and sess.in_flight_job is not None and sess.in_flight_chain == plan.history.chain:
            return sess.in_flight_job
        return None

    # -------------------------------------------------------------- admin
    def reset(self, provider: Optional[str] = None) -> int:
        if provider:
            n = 1 if self.active.pop(provider, None) else 0
        else:
            n = len(self.active)
            self.active.clear()
        self.save()
        return n

    def snapshot(self) -> Dict[str, Any]:
        now = time.time()
        c = self.counters
        return {
            "sessions": {
                p: {
                    "id": s.id,
                    "turns": s.turns,
                    "delta_turns": s.delta_turns,
                    "cached_turns": s.cached_turns,
                    "messages": len(s.sent),
                    "side_turns": s.side_turns,
                    "side_lanes": [d.get("label") for d in s.lanes],
                    "chars_saved": s.chars_saved,
                    "awaiting_answer": s.awaiting,
                    "busy": s.in_flight is not None,
                    "idle_seconds": int(now - s.last_used),
                }
                for p, s in self.active.items()
            },
            "requests": {k: c[k] for k in ("full", "delta", "cached", "nudge", "side", "local")},
            "chars_sent": c["chars_sent"],
            "chars_without_incremental": c["chars_full"],
            "chars_saved": max(0, c["chars_full"] - c["chars_sent"]),
        }
