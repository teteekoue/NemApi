/**
 * NemApi – shared DOM helpers for provider adapters
 */
(function (global) {
  "use strict";

  /* ------------------------------------------------------------------ */
  /* Timers — the hidden-tab problem (v5.5.0, minimal version)            */
  /* ------------------------------------------------------------------ */

  /**
   * Timers that keep their pace in a hidden (background) tab.
   *
   * Chrome throttles a hidden page's chained timers: one wake per second, and
   * — after five minutes hidden, with a timer chain of 5+ (every poll loop is
   * such a chain) — ONE wake per MINUTE ("intensive wake-up throttling",
   * Chrome 88+). A wait that asks for 1.5 s of quiet then woke up a minute
   * later: a whole request took 2 to 5 minutes and finished INSTANTLY as soon
   * as the tab came back to the foreground. That is the random "it only works
   * when I look at the tab" bug.
   *
   * This ONLY changes the timer source: the wait loops, the detection and the
   * extraction are untouched (v5.3.1 behaviour).
   *
   *  1. a dedicated Worker (`providers/ticker.js`): its timers are not
   *     throttled. It is pinged before being trusted, and written off after
   *     two missed wake-ups;
   *  2. a MessageChannel bounce: the `setTimeout` is armed from a MESSAGE
   *     task (nesting level 1), so the chain never reaches 5 and the
   *     "once per minute" tier never applies (1 s granularity at worst);
   *  3. a plain `setTimeout`, for a page without the extension (tests) and
   *     for a visible tab (the cheapest, most precise timer).
   */
  const TimerHub = (function () {
    const GUARD_MS = 2500;
    const PROBE_MS = 1200;
    const worker = { state: "unknown", ref: null, seq: 1, waits: new Map(), guards: new Map(), misses: 0, gen: 0 };
    const bounce = { port: null, keep: null, seq: 1, waits: new Map(), queue: [] };

    function extensionAvailable() {
      try {
        return (
          typeof chrome !== "undefined" &&
          !!chrome.runtime &&
          typeof chrome.runtime.getURL === "function"
        );
      } catch (_) {
        return false;
      }
    }

    function releaseWorkerWaits() {
      const pending = Array.from(worker.waits.values());
      worker.waits.clear();
      for (const id of Array.from(worker.guards.keys())) dropGuard(id);
      for (const entry of pending) {
        try {
          entry.resolve();
        } catch (_) {}
      }
    }

    function dropGuard(id) {
      const handle = worker.guards.get(id);
      if (handle === undefined) return;
      worker.guards.delete(id);
      try {
        clearTimeout(handle);
      } catch (_) {}
    }

    function retireWorker() {
      worker.gen += 1;
      try {
        worker.ref && worker.ref.terminate();
      } catch (_) {}
      worker.ref = null;
      worker.state = "unavailable";
      releaseWorkerWaits();
    }

    /** Spawn the worker and CONFIRM it answers (`ping`) before trusting it. */
    function startWorker() {
      if (worker.state !== "unknown") return;
      worker.state = "unavailable";
      if (!extensionAvailable() || typeof Worker !== "function") return;
      let ref = null;
      try {
        ref = new Worker(chrome.runtime.getURL("providers/ticker.js"));
      } catch (_) {
        return; // CSP of the page (`worker-src`), or the resource is missing
      }
      const gen = worker.gen;
      try {
        ref.onmessage = function (event) {
          const data = (event && event.data) || {};
          const id = data.id;
          if (data.type === "pong") {
            if (id === "probe" && gen === worker.gen) {
              worker.state = "ready";
              worker.ref = ref;
            }
            return;
          }
          const entry = id != null ? worker.waits.get(id) : null;
          if (!entry) return;
          worker.waits.delete(id);
          dropGuard(id);
          worker.misses = 0;
          try {
            entry.resolve();
          } catch (_) {}
        };
        ref.onerror = function () {
          retireWorker();
        };
      } catch (_) {
        return;
      }
      try {
        ref.postMessage({ type: "ping", id: "probe" });
      } catch (_) {
        return;
      }
      // No pong: the page refuses the worker. Keep it alive (a late `pong`
      // revives it) but stop trusting it.
      setTimeout(function () {
        if (gen === worker.gen && worker.state !== "ready") worker.state = "unavailable";
      }, PROBE_MS);
    }

    function arriveOnTime(id) {
      return function () {
        const entry = worker.waits.get(id);
        if (!entry) return; // the worker answered: nothing to repair
        worker.waits.delete(id);
        worker.misses += 1;
        if (worker.misses >= 2) {
          // A worker that answers nothing is worse than no worker: the guard
          // would add GUARD_MS to every single wait.
          retireWorker();
        }
        try {
          entry.resolve();
        } catch (_) {}
      };
    }

    function workerSleep(ms) {
      const id = worker.seq++;
      return new Promise((resolve) => {
        worker.waits.set(id, { resolve: resolve });
        try {
          worker.ref.postMessage({ type: "sleep", id: id, ms: ms });
        } catch (_) {
          worker.waits.delete(id);
          return resolve();
        }
        worker.guards.set(
          id,
          setTimeout(function () {
            worker.guards.delete(id);
            arriveOnTime(id)();
          }, ms + GUARD_MS)
        );
      });
    }

    function startBounce() {
      if (bounce.port) return;
      try {
        const channel = new MessageChannel();
        channel.port1.onmessage = function () {
          const id = bounce.queue.shift();
          if (id === undefined) return;
          const entry = bounce.waits.get(id);
          if (!entry) return;
          bounce.waits.delete(id);
          // Armed from a MESSAGE task, not from a timer task: nesting level 1.
          setTimeout(function () {
            try {
              entry.resolve();
            } catch (_) {}
          }, entry.ms);
        };
        // The endpoint must stay reachable: a port with no reference left is
        // collected and stops delivering.
        bounce.port = channel.port2;
        bounce.keep = channel.port1;
      } catch (_) {
        bounce.port = null;
        bounce.keep = null;
      }
    }

    function bounceSleep(ms) {
      const id = bounce.seq++;
      return new Promise((resolve) => {
        bounce.waits.set(id, { ms: ms, resolve: resolve });
        bounce.queue.push(id);
        try {
          bounce.port.postMessage(id);
        } catch (_) {
          bounce.waits.delete(id);
          resolve();
        }
      });
    }

    /**
     * Sleep `ms`.
     *
     * A VISIBLE tab keeps the plain timer: it is not throttled, it is the most
     * precise and it costs nothing. Only a hidden tab is worth a round trip
     * through the Worker (or the bounce).
     */
    function sleep(ms) {
      const delay = Math.max(0, Number(ms) || 0);
      let hidden = false;
      try {
        hidden = !!document.hidden;
      } catch (_) {}
      if (!delay || !hidden) return new Promise((r) => setTimeout(r, delay));
      // A hidden tab may be frozen after five minutes: opt out before waiting.
      keepTabAwake();
      startWorker();
      if (worker.state === "ready" && worker.ref) return workerSleep(delay);
      startBounce();
      if (bounce.port) return bounceSleep(delay);
      return new Promise((r) => setTimeout(r, delay));
    }

    return {
      sleep: sleep,
      /** "worker" | "message" | "timer" — what a hidden tab actually uses. */
      mode: function () {
        if (worker.state === "ready" && worker.ref) return "worker";
        if (bounce.port) return "message";
        return "timer";
      },
    };
  })();

  function sleep(ms) {
    return TimerHub.sleep(ms);
  }

  /**
   * Keep this tab out of Chrome's freezer.
   *
   * "Freezing on Energy Saver" (desktop, Chrome 133+) suspends the task
   * execution of a tab that has been hidden and silent for five minutes:
   * timers, event handlers and promise jobs all stop, and the tab only runs
   * again when the user activates it — a request in flight then simply waits
   * forever. The documented opt-out is to HOLD A WEB LOCK.
   *
   * Called from `sleep()` only when the tab is hidden, so a visible tab pays
   * nothing.
   */
  function keepTabAwake() {
    try {
      if (!navigator || !navigator.locks || typeof navigator.locks.request !== "function") {
        return "unavailable";
      }
      if (global.__NEMAPI_KEEP_AWAKE__) return global.__NEMAPI_KEEP_AWAKE__;
      global.__NEMAPI_KEEP_AWAKE__ = "requested";
      navigator.locks
        .request("nemapi:keep-page-alive", { mode: "shared" }, function () {
          global.__NEMAPI_KEEP_AWAKE__ = "held";
          // Never resolves: the lock is held for the life of the tab.
          return new Promise(function () {});
        })
        .catch(function () {
          global.__NEMAPI_KEEP_AWAKE__ = "failed";
        });
      return "requested";
    } catch (_) {
      return "failed";
    }
  }

  function waitFor(selector, timeout = 12000) {
    return new Promise((resolve, reject) => {
      const t0 = Date.now();
      const tick = () => {
        const el = document.querySelector(selector);
        if (el) return resolve(el);
        if (Date.now() - t0 > timeout) return reject(new Error("Timeout: " + selector));
        setTimeout(tick, 120);
      };
      tick();
    });
  }

  function queryFirst(selectors) {
    for (const s of selectors) {
      try {
        const el = document.querySelector(s);
        if (el) return el;
      } catch (_) {}
    }
    return null;
  }

  function queryAll(selectors, root = document) {
    const out = [];
    for (const s of selectors) {
      try {
        out.push(...root.querySelectorAll(s));
      } catch (_) {}
    }
    return out;
  }

  function isVisible(el) {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    return !!(style && style.display !== "none" && style.visibility !== "hidden" && style.opacity !== "0");
  }

  function textOf(el) {
    return (el && (el.innerText || el.textContent || "") || "").trim();
  }

  function findByText(selectors, patterns, root = document) {
    const els = queryAll(selectors, root);
    for (const el of els) {
      const label = `${el.getAttribute("aria-label") || ""} ${el.title || ""} ${textOf(el)}`.toLowerCase();
      if (patterns.some((re) => re.test(label)) && isVisible(el)) return el;
    }
    return null;
  }

  function findComposerRoot(el) {
    // The search starts ONE LEVEL ABOVE the field: a composer field is often
    // mistaken for the composer root (its class contains "input" — e.g.
    // "message-input-textarea", "chat-input-editor"), and the send control
    // is a SIBLING of the field, not a descendant. Anchoring on the field
    // itself therefore never finds the send button.
    const anchor = el || document.activeElement || document.body;
    let node = anchor.parentElement || anchor;
    for (let i = 0; i < 5 && node; i += 1) {
      if (node.matches && node.matches("form, main, [role='main'], [class*='composer'], [class*='input'], [class*='prompt'], [class*='chat']")) {
        return node;
      }
      node = node.parentElement;
    }
    return document.body;
  }

  /**
   * Last-resort composer discovery.
   *
   * Selectors age: a site renames one class and the composer becomes
   * invisible to every list of candidates — the prompt is then "written" into
   * nothing, or the send control is never found, and the caller loops. This
   * finds the editable surface the page is actually using: the biggest visible
   * textarea / contenteditable, preferring the one an input event would reach
   * (bottom of the viewport, near a button).
   *
   * It only runs when the specific selectors found nothing.
   */
  function discoverEditable() {
    const nodes = [];
    const push = (root) => {
      try {
        root
          .querySelectorAll("textarea, [contenteditable='true'], [role='textbox']")
          .forEach((el) => nodes.push(el));
      } catch (_) {}
    };
    push(document);
    for (const root of openShadowRoots()) push(root);
    const vh = (window.innerHeight || 800);
    let best = null;
    let bestScore = -1;
    for (const el of nodes) {
      if (!isVisible(el)) continue;
      if (el.disabled || el.readOnly) continue;
      if (el.getAttribute && el.getAttribute("contenteditable") === "false") continue;
      const r = el.getBoundingClientRect ? el.getBoundingClientRect() : null;
      if (!r || r.width < 80 || r.height < 10) continue;
      // prefer: large, low on the screen, editable text
      const score =
        Math.min(r.width, 1200) / 10 +
        Math.min(r.height, 300) / 5 +
        (r.top > vh * 0.4 ? 60 : 0) +
        (el.tagName === "TEXTAREA" ? 20 : 0);
      if (score > bestScore) {
        bestScore = score;
        best = el;
      }
    }
    return best;
  }

  /**
   * Last-resort send control, anchored on the composer we wrote into.
   *
   * A send control is a small clickable element right of / below the composer.
   * Everything that is obviously something else (stop, attach, upload, mic,
   * model picker, toolkit) is refused by its label.
   */
  function discoverSend(near) {
    const root = near ? findComposerRoot(near) : document.body;
    const scope = root && root.querySelectorAll ? root : document;
    const base = near && near.getBoundingClientRect ? near.getBoundingClientRect() : null;
    const BAD = /stop|cancel|attach|upload|file|mic|voice|audio|toolkit|model|setting|menu|close|delete|clear|copy|retry|regenerat|scroll|download|share|like|dislike|feedback|speak|search|web|deep|think|reason/i;
    const GOOD = /send|submit|发送|送信|enviar|envoyer/i;
    let best = null;
    let bestScore = -1;
    const nodes = [];
    try {
      scope.querySelectorAll("button, [role='button'], [type='submit']").forEach((el) => nodes.push(el));
    } catch (_) {}
    for (const el of nodes) {
      if (!isVisible(el)) continue;
      const label = `${el.getAttribute("aria-label") || ""} ${el.title || ""} ${el.id || ""} ${
        typeof el.className === "string" ? el.className : ""
      } ${el.getAttribute("data-testid") || ""}`;
      if (BAD.test(label)) continue;
      const r = el.getBoundingClientRect ? el.getBoundingClientRect() : null;
      if (!r || r.width < 12 || r.height < 12) continue;
      let score = 0;
      if (GOOD.test(label)) score += 100;
      if (base) {
        // right of the composer, on the composer's line
        const dx = r.left - base.right;
        const dy = Math.abs(r.top + r.height / 2 - (base.top + base.height / 2));
        if (dx > -base.width * 0.6 && dx < 320) score += 20;
        if (dy < Math.max(80, base.height)) score += 25;
      }
      // a send control is small (an arrow) and often round
      if (r.width <= 64 && r.height <= 64) score += 10;
      if (/^button$/i.test(el.tagName)) score += 5;
      if (score > bestScore) {
        bestScore = score;
        best = el;
      }
    }
    return bestScore >= 20 ? best : null;
  }

  /**
   * Value React currently holds for a controlled field (the composer textarea).
   *
   * The DOM value is NOT enough: React keeps its own state and rewrites the
   * DOM on the next render. A write React ignored therefore leaves a textarea
   * that looks filled for a few hundred milliseconds and is then silently
   * emptied. The page bridge (MAIN world) can read the controlled value.
   * Returns null when it cannot be read (bridge absent, uncontrolled field).
   */
  function readReactValue(el) {
    if (!el || !el.setAttribute || typeof document === "undefined") return null;
    try {
      el.removeAttribute("data-nemapi-val");
      el.removeAttribute("data-nemapi-val-ok");
      el.setAttribute("data-nemapi-val-req", "1");
      document.dispatchEvent(new CustomEvent("nemapi:read-value"));
      const ok = el.getAttribute("data-nemapi-val-ok") === "1";
      const raw = el.getAttribute("data-nemapi-val");
      el.removeAttribute("data-nemapi-val-req");
      el.removeAttribute("data-nemapi-val");
      el.removeAttribute("data-nemapi-val-ok");
      return ok ? (raw == null ? "" : String(raw)) : null;
    } catch (_) {
      return null;
    }
  }

  /** Is the observed text the text we wanted? (tolerant to a trimmed copy) */
  function textMatches(observed, want) {
    const a = String(observed || "").trim();
    const b = String(want || "").trim();
    if (!a || !b) return false;
    return a === b || a.includes(b) || b.includes(a);
  }

  /**
   * Is the composer holding EXACTLY our text?
   *
   * The tolerant `textMatches` is wrong for the composer: a leftover draft
   * ("continue" restored from localStorage, a previous prompt) plus our text
   * still "contains" our text, but the message that would be sent is the
   * concatenation of the two. The composer check must be exact.
   */
  /**
   * Compare a composer's content with what we wrote, TOLERANTLY for long text.
   *
   * A 100 to 150 KB prompt cannot be compared character for character with the
   * DOM: a rich editor turns newlines into <br> / paragraphs, inserts
   * non-breaking spaces, and may normalise trailing blanks. `textEquals` then
   * never succeeds, and the caller — which believes the text was wiped —
   * clears the field and types it again: the user sees the prompt appear and
   * disappear in a loop, and the message is never sent.
   *
   * The comparison therefore runs on a normalised form (line endings, NBSP,
   * runs of spaces, empty lines) and, above 4 000 characters, ALSO accepts the
   * case where the editor kept our text minus a normalisation difference —
   * but never a truncation, which is reported separately by
   * `composerReport` so the caller can say what the site did.
   */
  function normalizeComposer(value) {
    return String(value ?? "")
      .replace(/\r\n?/g, "\n")
      .replace(/[\u00a0\u2007\u202f]/g, " ")
      .replace(/[ \t]+/g, " ")
      .replace(/ *\n */g, "\n")
      .replace(/\n{3,}/g, "\n\n")
      .trim();
  }

  /** Every character kept, all layout dropped: what "the text is there" means. */
  function compactComposer(value) {
    return String(value ?? "").replace(/\s+/g, "");
  }

  function composerMatches(observed, want) {
    if (textEquals(observed, want)) return true;
    if (!String(want ?? "").trim()) return false;
    const a = normalizeComposer(observed);
    const b = normalizeComposer(want);
    if (a === b) return true;
    // Below this size the exact comparison is the right one (a leftover draft
    // must never be accepted); above it, no editor round-trips whitespace
    // faithfully and the check must be about the CONTENT.
    if (String(want ?? "").length <= 4000) return false;
    return compactComposer(a) === compactComposer(b);
  }

  /** Why a write failed, in numbers — logged and shown to the caller. */
  function composerReport(observed, want) {
    const a = String(observed ?? "");
    const b = String(want ?? "");
    let i = 0;
    while (i < a.length && i < b.length && a[i] === b[i]) i += 1;
    return {
      dom: a.length,
      want: b.length,
      firstDiff: i,
      truncated: a.length > 0 && a.length < b.length * 0.98,
      matches: composerMatches(a, b),
    };
  }

  function textEquals(observed, want) {
    const a = String(observed || "").trim();
    const b = String(want || "").trim();
    if (!a || !b) return false;
    return a === b;
  }

  /**
   * Empty a rich-text editor.
   *
   * A rich editor INSERTS at the caret: without clearing it first, the
   * prompt is appended to whatever was already there. `selectAll` + `delete`
   * is the usual way, but it does not work everywhere (Kimi's /agent page
   * selects but does not delete), so the DOM is the fallback — the editor
   * reconciles its state from the DOM.
   */
  function clearContentEditable(el) {
    if (!el) return false;
    try {
      el.focus();
      document.execCommand("selectAll", false, null);
      document.execCommand("delete", false, null);
      if (!String(el.innerText || el.textContent || "").trim()) return true;
    } catch (_) {}
    try {
      el.textContent = "";
      notifyInput(el);
      return true;
    } catch (_) {}
    return false;
  }

  /**
   * React-friendly textarea value set (works on 1st and Nth message in same chat).
   *
   * Two strategies, most reliable first:
   *  1. `document.execCommand("insertText")` — the browser performs a REAL text
   *     insertion and emits a trusted `beforeinput`/`input` pair. React's
   *     synthetic onChange sees it exactly like a user's keystroke.
   *  2. native value setter + InputEvent — the classic React workaround. It
   *     works, but React ignores the event when the field is disabled, or when
   *     the page re-renders before the event is dispatched.
   */
  function setTextareaValue(el, text) {
    const value = String(text ?? "");
    if (!el) return "";
    try {
      el.focus();
    } catch (_) {}

    // 1) Quill editor (Gemini): the app reads Quill's MODEL, so the instance
    //    itself must be written. A DOM-only write is synced back by Quill's
    //    MutationObserver, but it loses the caret and the app's state.
    if (isQuillEditor(el)) {
      setQuillText(el, value);
      return editorText(el);
    }

    // 2) rich-text editor (contenteditable): the current DeepSeek composer is
    //    a Lexical editor, and ONLY a real text insertion is understood by it.
    //    Assigning textContent is not enough: the editor rebuilds its state
    //    from the DOM and drops the text at the next render.
    if (isContentEditable(el)) {
      // a rich editor INSERTS at the caret: clear it first, or the prompt is
      // appended to a leftover draft
      clearContentEditable(el);
      placeCaretEnd(el);
      if (insertTextNative(el, value)) return editorText(el);
      try {
        el.textContent = value;
      } catch (_) {}
      notifyInput(el);
      return editorText(el);
    }

    // 3) textarea / input. A native insertion is the most compatible form:
    //    it bypasses React's value tracker (a plain `el.value = x` updates
    //    the tracker too, and React then sees "no change" and drops it).
    //
    //    But NOT for a very large prompt: an agent sends 90 to 120 KB on its
    //    first turn, and `execCommand("insertText")` feeds it to the editor's
    //    input handlers, which re-render the controlled field on every
    //    insertion — the write then takes minutes (the "first request takes
    //    3 to 5 minutes" report). Above this size the field is written once,
    //    through the native setter below, then a single `input` event.
    const big = value.length > 8000;
    if (!el.disabled && !el.readOnly && !big) {
      clearNativeValue(el);
      placeCaretEnd(el);
      if (insertTextNative(el, value) && editorText(el).trim() === value.trim()) {
        notifyInput(el);
        return editorText(el);
      }
    }

    // 4) native setter fallback (the classic React workaround)
    const proto =
      el.tagName === "INPUT" ? window.HTMLInputElement.prototype : window.HTMLTextAreaElement.prototype;
    const desc = Object.getOwnPropertyDescriptor(proto, "value");
    const setVal = (v) => {
      if (desc && desc.set) desc.set.call(el, v);
      else el.value = v;
    };
    if (!big) {
      // make React see a change (it compares with its own tracker)
      setVal("");
      el.dispatchEvent(new Event("input", { bubbles: true }));
    }
    setVal(value);
    try {
      el.dispatchEvent(
        new InputEvent("beforeinput", {
          bubbles: true,
          cancelable: true,
          inputType: "insertFromPaste",
          data: value,
        })
      );
    } catch (_) {}
    try {
      el.dispatchEvent(
        new InputEvent("input", { bubbles: true, inputType: "insertFromPaste", data: value })
      );
    } catch (_) {
      el.dispatchEvent(new Event("input", { bubbles: true }));
    }
    el.dispatchEvent(new Event("change", { bubbles: true }));
    // Some React builds only listen to keyup/keydown to enable Send
    try {
      el.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, key: "a", code: "KeyA" }));
      el.dispatchEvent(new KeyboardEvent("keyup", { bubbles: true, key: "a", code: "KeyA" }));
    } catch (_) {}
    try {
      el.style.height = "auto";
      el.style.height = el.scrollHeight + "px";
    } catch (_) {}
    // Final assert-friendly assignment
    if (editorText(el) !== value) {
      setVal(value);
      el.dispatchEvent(new Event("input", { bubbles: true }));
    }
    return editorText(el);
  }

  /** Is this element a rich-text editor (contenteditable)? */
  function isContentEditable(el) {
    if (!el || !el.getAttribute) return false;
    const a = el.getAttribute("contenteditable");
    if (a === "true" || a === "") return true;
    return el.isContentEditable === true;
  }

  /** Is this element (or the editor it belongs to) driven by Quill? */
  function isQuillEditor(el) {
    if (!el) return false;
    try {
      if (el.classList && el.classList.contains("ql-editor")) return true;
      if (el.classList && el.classList.contains("ql-container")) return true;
      if (findQuill(el)) return true;
      if (el.querySelector && el.querySelector(".ql-editor")) return true;
    } catch (_) {}
    return false;
  }

  /** The text really held by a composer (value for a field, content for an editor). */
  function editorText(el) {
    if (!el) return "";
    if (isQuillEditor(el)) {
      const quill = findQuill(el);
      if (quill) {
        try {
          // the MODEL is what the app knows about
          return String(quill.getText() || "");
        } catch (_) {}
      }
      // the instance lives in the page realm: the bridge reports the model
      const bridged = el.getAttribute && el.getAttribute("data-nemapi-quill-text");
      if (bridged !== null && bridged !== undefined) return String(bridged);
    }
    if (isContentEditable(el)) {
      return String(el.innerText != null ? el.innerText : el.textContent || "");
    }
    const v = el.value;
    return v == null ? String(el.textContent || "") : String(v);
  }

  /**
   * Perform a REAL text insertion in the focused element.
   *
   * `document.execCommand("insertText")` is the browser's own insertion: it
   * emits the beforeinput/input pair an editor (Lexical, ProseMirror, React)
   * listens to, exactly like a keystroke or a paste. It is the only method
   * that works on a rich-text editor.
   */
  function insertTextNative(el, value) {
    if (!el || typeof document.execCommand !== "function") return false;
    const text = String(value ?? "");
    if (!text) return true;
    try {
      el.focus();
      return !!document.execCommand("insertText", false, text);
    } catch (_) {
      return false;
    }
  }

  /** Put the caret at the end of the field (so the text is appended, not inserted at 0). */
  function placeCaretEnd(el) {
    try {
      if (el.setSelectionRange && typeof el.value === "string") {
        el.focus();
        const n = el.value.length;
        el.setSelectionRange(n, n);
        return;
      }
      if (isContentEditable(el)) {
        el.focus();
        const range = document.createRange();
        range.selectNodeContents(el);
        range.collapse(false);
        const sel = window.getSelection();
        if (sel) {
          sel.removeAllRanges();
          sel.addRange(range);
        }
      }
    } catch (_) {}
  }

  /** Empty a textarea/input through the PROTOTYPE setter (bypasses React's tracker). */
  function clearNativeValue(el) {
    try {
      const proto =
        el.tagName === "INPUT" ? window.HTMLInputElement.prototype : window.HTMLTextAreaElement.prototype;
      const desc = Object.getOwnPropertyDescriptor(proto, "value");
      if (desc && desc.set) desc.set.call(el, "");
      else el.value = "";
    } catch (_) {}
  }

  /** Dispatch the event pair an editor listens to. */
  function notifyInput(el) {
    try {
      el.dispatchEvent(new Event("input", { bubbles: true }));
    } catch (_) {}
    try {
      el.dispatchEvent(new Event("change", { bubbles: true }));
    } catch (_) {}
  }

  /**
   * The Quill instance that owns a Quill editor, if any.
   *
   * Quill v1 attaches itself to its container: `container.__quill = this`
   * (core/quill.js, constructor) and exposes `Quill.find(node)`. The
   * container is the `.ql-container` element, the editable root is the
   * `.ql-editor` inside it.
   */
  function findQuill(el) {
    if (!el) return null;
    try {
      if (el.__quill) return el.__quill;
      const container =
        (el.closest && el.closest(".ql-container")) ||
        (el.parentElement && el.parentElement.classList &&
          el.parentElement.classList.contains("ql-container")
          ? el.parentElement
          : null);
      if (container && container.__quill) return container.__quill;
      // walk up a few levels: the editor may be nested in a custom element
      let node = el;
      for (let i = 0; i < 4 && node; i += 1) {
        if (node.__quill) return node.__quill;
        node = node.parentElement;
      }
    } catch (_) {}
    return null;
  }

  /**
   * Ask the page bridge to write into a Quill editor.
   *
   * The Quill instance only exists in the PAGE realm (`container.__quill`
   * is an expando, and expandos do not cross the two JS realms of a Chrome
   * extension). The isolated world therefore cannot reach it: the write
   * goes through the bridge, which lives in the page world.
   *
   * Returns true when the bridge reports a successful write.
   */
  function bridgeSetQuill(el, text) {
    if (!el || !el.setAttribute) return false;
    try {
      el.setAttribute("data-nemapi-quill-target", "1");
      el.removeAttribute("data-nemapi-quill-ok");
      el.removeAttribute("data-nemapi-quill-text");
      document.dispatchEvent(
        new CustomEvent("nemapi:set-quill", { detail: { text: String(text ?? "") } })
      );
      const ok = el.getAttribute("data-nemapi-quill-ok") === "1";
      el.removeAttribute("data-nemapi-quill-target");
      el.removeAttribute("data-nemapi-quill-ok");
      return ok;
    } catch (_) {
      return false;
    }
  }

  /**
   * Write into a Quill editor.
   *
   * The official path is the instance itself: `quill.setText(text, 'user')`
   * updates Quill's MODEL (the app reads the model, not the DOM) and emits
   * the text-change event the app listens to. Mutating the DOM instead
   * (`innerHTML`) only works because Quill syncs it back through a
   * MutationObserver — it loses the caret and the app's state.
   */
  function setQuillText(el, text) {
    const value = String(text ?? "");
    const quill = findQuill(el);
    if (quill) {
      // "user" is the source of a real paste: an app that only reacts to
      // user-initiated changes ignores an "api" write. The fallback covers
      // the opposite case — a read-only editor refuses a "user" write
      // (Quill's `strict` mode) but accepts an "api" one.
      try {
        quill.setText(value, "user");
        if (String(quill.getText() || "").trim() === value.trim()) return true;
      } catch (_) {}
      try {
        quill.setText(value, "api");
        if (String(quill.getText() || "").trim() === value.trim()) return true;
      } catch (_) {}
    }
    // The instance usually lives in the page realm → ask the bridge.
    if (bridgeSetQuill(el, value)) return true;
    // No instance reachable: fall back to the DOM (Quill's observer syncs it)
    const editor =
      el.classList && el.classList.contains("ql-editor")
        ? el
        : el.querySelector(".ql-editor") || el;
    const escaped = value
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .split("\n")
      .map((l) => `<p>${l || "<br>"}</p>`)
      .join("");
    editor.innerHTML = escaped;
    notifyInput(editor);
    return false;
  }

  /**
   * Write `text` into a controlled field and make sure it STAYS there.
   *
   * A write can be silently dropped when the page re-renders (React restores
   * the value of its state, which is still the old one) — the classic case
   * being a text typed while an image upload finishes. The DOM value is
   * therefore not enough: the value React holds is read through the page
   * bridge and the text is written again for as long as it is not confirmed.
   *
   * `findEl` is called again on every attempt: React may have replaced the
   * node entirely (a remount discards the text that was written on it).
   */
  /**
   * Wait until the composer stops growing (a long insert is committed in
   * several frames by Lexical/ProseMirror). Bounded: 12 s, or two identical
   * readings 250 ms apart.
   */
  async function waitForComposerToSettle(el, { timeout = 12000, step = 250 } = {}) {
    const t0 = Date.now();
    let last = -1;
    let same = 0;
    while (Date.now() - t0 < timeout) {
      let n = 0;
      try {
        n = editorText(el).length;
      } catch (_) {}
      if (n > 0 && n === last) {
        same += 1;
        if (same >= 2) return n;
      } else {
        same = 0;
      }
      last = n;
      await sleep(step);
    }
    return last;
  }

  async function writeAndConfirm(findEl, text, { attempts = 5, holdMs = 320, settleMs = 600, log } = {}) {
    const want = String(text ?? "").trim();
    if (!want) return null;
    for (let attempt = 1; attempt <= attempts; attempt += 1) {
      let el = null;
      try {
        el = findEl();
      } catch (_) {}
      if (!el) {
        await sleep(250);
        continue;
      }
      if (el.disabled || el.readOnly) {
        // not editable yet (upload still running): wait for the field
        await sleep(300);
        continue;
      }
      setTextareaValue(el, text);
      await sleep(holdMs);
      // A very long prompt is not committed in 320 ms: the rich editor keeps
      // rendering it, and reading it now returns a PREFIX. Judging that
      // prefix as "the app wiped the text" made the caller clear the composer
      // and type the whole 150 KB again — the loop the user saw. Wait until
      // the editor stops growing before comparing.
      if (want.length > 4000) await waitForComposerToSettle(el);
      // The app may re-render right after (the end of an upload): a text
      // written too early is wiped when it restores its own state. The check
      // is therefore made TWICE, with a settle window in between.
      // long prompts: a character-for-character comparison with the DOM is
      // impossible (see composerMatches) — that mismatch is what made a
      // 150 KB prompt loop between "typed" and "cleared"
      const match = want.length > 4000 ? composerMatches : textEquals;
      const dom = editorText(el);
      const react = readReactValue(el);
      const ok = match(dom, want) && (react === null || match(react, want));
      if (ok) {
        await sleep(settleMs);
        const dom2 = editorText(el);
        const react2 = readReactValue(el);
        if (match(dom2, want) && (react2 === null || match(react2, want))) {
          return el;
        }
        if (log) log(`write attempt ${attempt}: wiped by a re-render, retrying`);
        continue;
      }
      if (log) {
        const r = composerReport(dom, want);
        log(
          `write attempt ${attempt}: dom=${r.dom}c want=${r.want}c ` +
            `first diff at ${r.firstDiff}${r.truncated ? " (the site truncated the prompt)" : ""} ` +
            `react=${react === null ? "n/a" : react.length + "c"}`
        );
      }
      await sleep(200);
    }
    return null;
  }

  /** ProseMirror / contenteditable — clear then insert (Nth message safe). */
  function setContentEditable(el, text) {
    const value = String(text ?? "");
    try {
      el.focus();
      el.click();
    } catch (_) {}
    try {
      document.execCommand("selectAll", false, null);
      document.execCommand("delete", false, null);
    } catch (_) {
      el.textContent = "";
    }
    let ok = false;
    try {
      ok = document.execCommand("insertText", false, value);
    } catch (_) {}
    if (!ok || !(el.textContent || "").includes(value.slice(0, Math.min(20, value.length)))) {
      el.textContent = value;
    }
    try {
      el.dispatchEvent(
        new InputEvent("beforeinput", {
          bubbles: true,
          cancelable: true,
          inputType: "insertFromPaste",
          data: value,
        })
      );
    } catch (_) {}
    el.dispatchEvent(new Event("input", { bubbles: true }));
    try {
      el.dispatchEvent(
        new InputEvent("input", { bubbles: true, inputType: "insertFromPaste", data: value })
      );
    } catch (_) {}
  }

  /** Quill (Gemini) */
  function setQuill(container, text) {
    const editor =
      container.classList && container.classList.contains("ql-editor")
        ? container
        : container.querySelector(".ql-editor") || container;
    editor.focus();
    const escaped = text
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .split("\n")
      .map((l) => `<p>${l || "<br>"}</p>`)
      .join("");
    editor.innerHTML = escaped;
    editor.dispatchEvent(new InputEvent("beforeinput", { bubbles: true, cancelable: true, inputType: "insertFromPaste", data: text }));
    editor.dispatchEvent(new Event("input", { bubbles: true }));
  }

  function clickEl(el) {
    if (!el) return false;
    try {
      el.click();
      return true;
    } catch (_) {
      return false;
    }
  }

  function pressEnter(el) {
    const opts = { bubbles: true, cancelable: true, key: "Enter", code: "Enter", keyCode: 13, which: 13 };
    el.dispatchEvent(new KeyboardEvent("keydown", opts));
    el.dispatchEvent(new KeyboardEvent("keypress", opts));
    el.dispatchEvent(new KeyboardEvent("keyup", opts));
  }

  function setNativeValue(el, text) {
    if (!el) return;
    if ("value" in el) {
      const proto = Object.getPrototypeOf(el);
      const desc = Object.getOwnPropertyDescriptor(proto, "value") || Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value") || Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value");
      if (desc && desc.set) desc.set.call(el, text);
      else el.value = text;
      return;
    }
    el.textContent = text;
  }

  /** Insert the complete prompt in one DOM update (never simulated typing). */
  function pasteText(el, text) {
    if (!el) throw new Error("No input element");
    if ("value" in el) return setTextareaValue(el, String(text ?? ""));
    if (el.classList && (el.classList.contains("ql-editor") || el.classList.contains("ql-container")) || el.querySelector?.(".ql-editor")) {
      return setQuill(el, String(text ?? ""));
    }
    return setContentEditable(el, String(text ?? ""));
  }

  /**
   * Wait until text stabilizes (streaming finished).
   * getText() returns current assistant text; done when unchanged for stableMs.
   * Virtual-list safe: ignores previous answer and truncated prefixes of it.
   */
  /**
   * Is this answer visibly unfinished? (a tool call / code block opened and
   * never closed). Used to wait much longer before accepting it: a long file
   * written by an agent can pause for several seconds mid-stream.
   */
  function looksIncomplete(text) {
    const t = String(text || "");
    const lastOpen = Math.max(t.lastIndexOf("<tool>"), t.lastIndexOf("<tool "), t.lastIndexOf("<tool_call>"));
    if (lastOpen >= 0 && !/<\/tool(?:_call)?\s*>/i.test(t.slice(lastOpen))) return true;
    // DSML parameter opened, never closed
    const dsmlOpen = t.search(/<[｜|]{1,2}\s*DSML\s*[｜|]{1,2}\s*parameter[^>]*>(?![\s\S]*<\s*\/\s*[｜|]{1,2}\s*DSML\s*[｜|]{1,2}\s*parameter)/i);
    if (dsmlOpen >= 0) return true;
    // odd number of fence lines → a code block is still open
    const fences = t.match(/^[ \t]*(`{3,}|~{3,})/gm) || [];
    return fences.length % 2 === 1;
  }

  /** Overall budget of the current job (set by content.js from the proxy timeout). */
  function jobBudgetMs(fallback) {
    const b = Number(global.__NEMAPI_JOB_BUDGET_MS__ || 0);
    return Math.max(fallback || 0, b > 0 ? b : 0);
  }

  function providerBusy() {
    try {
      const f = global.__NEMAPI_IS_BUSY__;
      return typeof f === "function" ? !!f() : false;
    } catch (_) {
      return false;
    }
  }

  /**
   * Wait for the answer to stop changing.
   *
   * The STABILITY of the text is the ground truth, not the provider's
   * "generating" flag: a page that still shows a Stop button (or a stray
   * spinner) after the answer is finished used to add the whole `busyStuckMs`
   * window to EVERY request — the answer was there, the proxy just never
   * returned it. The busy flag is now only a bounded grace period.
   *  - an unfinished-looking answer (open tool call / code block) must stay
   *    unchanged for `incompleteStableMs` before it is accepted;
   *  - a complete-looking answer is accepted after `stableMs` of stability,
   *    or after `busyGraceMs` if the page still claims to be generating;
   *  - no fixed 3-minute cap any more: the budget is the proxy job timeout,
   *    and an answer that keeps growing is never cut.
   * The result is flagged in global.__NEMAPI_LAST_PARTIAL__ when returned on
   * timeout.
   */
  async function waitUntilStable(getText, { timeout = 180000, stableMs = 1800, pollMs = 400, previous = "", incompleteStableMs = 25000, busyGraceMs = 8000, getFinal = null } = {}) {
    // The polling getter is CHEAP (a raw text read); the expensive markdown
    // conversion runs ONCE, when the answer is accepted. Rebuilding the
    // markdown every 400 ms was the main cost of the detection, and it
    // grows with the length of the answer.
    const finish = (t) => {
      if (typeof getFinal === "function") {
        try {
          return getFinal();
        } catch (_) {}
      }
      return t;
    };
    const t0 = Date.now();
    const budget = jobBudgetMs(timeout);
    const prev = (previous || "").trim();
    const prevFp = prev ? prev.length + ":" + prev.slice(0, 48) + ":" + prev.slice(-48) : "";
    let last = "";
    let lastFp = "";
    let stableSince = Date.now();
    global.__NEMAPI_LAST_PARTIAL__ = false;
    while (Date.now() - t0 < budget) {
      if (global.__NEMAPI_STOP__) throw new Error("Stopped");
      let cur = "";
      try {
        cur = (getText() || "").trim();
      } catch (_) {}
      // Ignore the last answer that was already present before this job, and
      // any partial render of it. Virtualized lists (DeepSeek/Qwen) re-render
      // old messages as a truncated prefix of the full text.
      if (cur && prev) {
        if (cur === prev) cur = "";
        else if (cur.length < prev.length && prev.startsWith(cur)) cur = "";
        else if (prev.length < cur.length && cur.startsWith(prev) && cur.length - prev.length < 8) {
          // still essentially the old message with trivial growth
          cur = "";
        } else {
          const fp = cur.length + ":" + cur.slice(0, 48) + ":" + cur.slice(-48);
          if (fp === prevFp) cur = "";
        }
      }
      const fp = cur ? cur.length + ":" + cur.slice(0, 48) + ":" + cur.slice(-48) : "";
      if (cur && fp === lastFp && cur.length > 0) {
        const still = Date.now() - stableSince;
        const busy = providerBusy();
        if (looksIncomplete(cur) && busy) {
          // the model is still writing: an unfinished answer is expected
          if (still >= Math.max(stableMs, incompleteStableMs)) return finish(cur);
        } else if (busy) {
          // the page still claims to be generating although the text stopped
          // moving: bounded grace period (a pause while thinking), then take
          // the answer instead of waiting for a whole minute
          if (still >= busyGraceMs) return finish(cur);
        } else if (still >= stableMs) {
          // the page is idle and the text stopped moving: the answer is done.
          // An "unfinished looking" answer is NOT a reason to wait longer —
          // the proxy detects a call that was cut off and continues it.
          return finish(cur);
        }
      } else {
        last = cur;
        lastFp = fp;
        stableSince = Date.now();
      }
      await sleep(pollMs);
    }
    if (last) {
      global.__NEMAPI_LAST_PARTIAL__ = true;
      return finish(last);
    }
    throw new Error("Timeout waiting for response");
  }

  function lastOf(arr) {
    return arr && arr.length ? arr[arr.length - 1] : null;
  }

  function textFingerprint(text) {
    const t = (text || "").trim();
    if (!t) return "";
    return t.length + ":" + t.slice(0, 64) + ":" + t.slice(-64);
  }

  /**
   * Cheap activity signature of the page: does it still change?
   *
   * getLastResponse() rebuilds the whole markdown (clone + innerText +
   * regexes) — far too heavy to call every 800 ms while waiting for the page
   * to be idle. Providers may expose their own getActivitySignature(); the
   * number of answer blocks is the generic fallback.
   */
  function activitySignature(provider) {
    try {
      if (provider.getActivitySignature) return String(provider.getActivitySignature());
    } catch (_) {}
    try {
      if (provider.getResponseCount) return "n" + provider.getResponseCount();
    } catch (_) {}
    return "";
  }

  /**
   * Wait for a NEW assistant message (new DOM node OR new fingerprint on the
   * last node — critical for virtualized lists that reuse elements).
   * getMessageEls() returns assistant message elements in DOM order;
   * readText(el) extracts the assistant answer text from a message element.
   */
  async function waitForNewResponse(getMessageEls, readText, { timeout = 180000, stableMs = 2000, pollMs = 400, previous = "", newElTimeout = 25000, readCheap = null } = {}) {
    // The polling read is CHEAP (raw text); the markdown conversion is done
    // once, at the end. Rebuilding the markdown on every poll was the main
    // cost of the detection — and it grows with the length of the answer.
    const cheap = readCheap || readText;
    const oldEls = getMessageEls() || [];
    const oldEl = lastOf(oldEls);
    const oldCount = oldEls.length;
    const oldFp = textFingerprint(previous || (oldEl ? readText(oldEl) : ""));
    const phase1End = Date.now() + newElTimeout;
    let newEl = null;
    let sawNewContent = false;

    while (Date.now() < phase1End) {
      if (global.__NEMAPI_STOP__) throw new Error("Stopped");
      const els = getMessageEls() || [];
      const last = lastOf(els);
      if (last && last !== oldEl) {
        newEl = last;
        break;
      }
      // Virtual list: same node, but message count grew or fingerprint changed
      if (last && els.length > oldCount) {
        newEl = last;
        break;
      }
      if (last) {
        let cur = "";
        try {
          cur = (cheap(last) || "").trim();
        } catch (_) {}
        const fp = textFingerprint(cur);
        // A virtualized list re-renders the PREVIOUS answer as a truncated
        // prefix of it: that is not a new answer. Anything else IS new, even
        // a short one ("OK.", "Done.") — the old heuristic ("the new answer
        // must be half as long as the previous one") missed those, delaying
        // every short answer by the whole newElTimeout.
        const isOldPrefix = !!previous && (previous === cur || previous.startsWith(cur));
        if (fp && fp !== oldFp && !isOldPrefix) {
          sawNewContent = true;
          newEl = last;
          break;
        }
      }
      await sleep(pollMs);
    }

    const readLast = () => {
      const last = lastOf(getMessageEls());
      return last ? readText(last) : "";
    };
    // No new bubble detected → keep filtering against `previous` so we don't
    // return the old answer while streaming/idle.
    if (!newEl && !sawNewContent) {
      return waitUntilStable(
        () => {
          const last = lastOf(getMessageEls());
          return last ? cheap(last) : "";
        },
        {
          timeout,
          stableMs,
          pollMs,
          previous,
          getFinal: readLast,
        }
      );
    }
    // A NEW message element (or fingerprint) was observed. Do NOT filter by
    // `previous` anymore — consecutive identical answers are valid and must
    // be delivered (same fix as Gemini bubble-count gate).
    const target = newEl;
    return waitUntilStable(() => (target ? cheap(target) : readLast()), {
      timeout,
      stableMs,
      pollMs,
      previous: "",
      getFinal: () => (target ? readText(target) : readLast()),
    });
  }

  function isDisabled(el) {
    if (!el) return true;
    if (el.getAttribute("aria-disabled") === "true") return true;
    if (el.disabled) return true;
    // A "disabled" class is often transient (React re-renders it while an
    // upload finishes or the editor state settles). Only treat it as final
    // when there is no other sign that the button is usable.
    const cls = (el.className && String(el.className)) || "";
    if (/\bdisabled\b/i.test(cls) && !/(?:primary|submit|circle|send)/i.test(cls)) return true;
    return false;
  }

  /**
   * Wait until a send button becomes enabled after pasting text.
   * findSendFn() must return the current candidate button or null.
   *
   * `extraCheck` lets a provider add its own rule: Kimi's send control is a
   * `<div>` (no `disabled` attribute) that gets a `disabled` CLASS instead.
   */
  async function waitForEnabledSend(findSendFn, timeoutMs = 3500, extraCheck = null) {
    const t0 = Date.now();
    while (Date.now() - t0 < timeoutMs) {
      const btn = findSendFn();
      if (
        btn &&
        !isDisabled(btn) &&
        isVisible(btn) &&
        (!extraCheck || !extraCheck(btn))
      ) {
        return btn;
      }
      await sleep(80);
    }
    return findSendFn();
  }

  /**
   * The editable surfaces of a composer, best candidate first.
   *
   * A provider may use several widgets (a rich-text editor, a textarea, a
   * hidden mirror). Writing into the wrong one is SILENT — the text goes
   * nowhere and the send control stays disabled — so the caller tries them
   * in turn, best first.
   *
   * The order of `selectors` IS the order of preference: put the specific
   * ones first, the generic ones (a bare "textarea") last.
   */
  function inputCandidatesFrom(selectors) {
    const out = [];
    const seen = new Set();
    const collect = (els) => {
      for (const el of els) {
        if (seen.has(el)) continue;
        if (!isVisible(el)) continue;
        if (el.getAttribute && el.getAttribute("contenteditable") === "false") continue;
        seen.add(el);
        out.push(el);
      }
    };
    for (const sel of selectors || []) {
      let els = [];
      try {
        els = Array.from(document.querySelectorAll(sel));
      } catch (_) {
        continue;
      }
      collect(els);
    }
    if (!out.length) {
      // The composer may live inside an OPEN shadow root (a custom element's
      // content is invisible to document.querySelectorAll). The scan only
      // runs when the direct lookup found nothing, so it never costs
      // anything on the happy path.
      const roots = openShadowRoots();
      for (const sel of selectors || []) {
        for (const root of roots) {
          let els = [];
          try {
            els = Array.from(root.querySelectorAll(sel));
          } catch (_) {
            continue;
          }
          collect(els);
        }
      }
    }
    return out;
  }

  /** The open shadow roots of the page (bounded depth). */
  function openShadowRoots() {
    const roots = [];
    const walk = (scope, depth) => {
      if (depth > 3) return;
      let hosts = [];
      try {
        hosts = Array.from(scope.querySelectorAll("*"));
      } catch (_) {
        return;
      }
      for (const host of hosts) {
        const root = host.shadowRoot;
        if (!root) continue;
        roots.push(root);
        walk(root, depth + 1);
      }
    };
    walk(document, 0);
    return roots;
  }

  /**
   * Wait until a surface is editable.
   *
   * A composer is disabled while the app is busy (an upload in progress, a
   * previous answer still streaming). Writing into a disabled field is
   * ignored by the app, so the write is postponed rather than lost.
   */
  async function waitEditable(el, timeoutMs = 5000) {
    const t0 = Date.now();
    while (Date.now() - t0 < timeoutMs) {
      if (!el) return false;
      if (el.isConnected === false) return false;
      if (!el.disabled && !el.readOnly) {
        if (!el.getAttribute || el.getAttribute("contenteditable") !== "false") return true;
      }
      await sleep(200);
    }
    return false;
  }

  /**
   * Write a prompt into the composer and PROVE the app took it.
   *
   * The DOM value is not proof: a rich-text editor (Lexical, ProseMirror,
   * Quill) keeps the text in its own state, and a field the app ignored is
   * emptied at the next render — the text LOOKS typed and disappears.
   *
   * What the app exposes is the SEND CONTROL: it is enabled only when the
   * composer really holds the text. "The send button became usable" is
   * therefore the success signal, and the prompt is rewritten until it does.
   * This is also what makes the write survive the re-render that ends an
   * image upload.
   *
   * `inputCandidates()` returns the editable surfaces to try, best first:
   * writing into the wrong one is SILENT (the text goes nowhere and the
   * send control stays disabled), so every candidate is tried in turn.
   *
   * Returns {input, btn} on success, null when no candidate ever took the
   * text.
   */
  async function sendWithProof({
    inputCandidates,
    findSend,
    text,
    log,
    attempts = 6,
    sendTimeout = 6000,
    settleMs = 600,
    sendDisabledCheck = null,
  } = {}) {
    const want = String(text ?? "").trim();
    const cands =
      (typeof inputCandidates === "function" ? inputCandidates() : inputCandidates) || [];
    if (!cands.length) {
      if (log) log("no editable composer found at all");
      return null;
    }
    // A 100 KB prompt needs the tolerant comparison, a short one the exact
    // check (a leftover draft must never be sent).
    const same = want.length > 4000 ? composerMatches : textMatches;
    const holds = (el) => {
      try {
        return !!want && same(editorText(el), want);
      } catch (_) {
        return false;
      }
    };
    for (const el of cands.slice(0, 3)) {
      // a disabled surface (an upload in progress) is postponed, not dropped
      if (!(await waitEditable(el, 6000))) continue;

      // Write ONCE. The old loop rewrote the whole prompt on every attempt,
      // which is what the user saw as "the message is pasted again and again
      // and never sent": the send control was never found, and each retry
      // wiped and retyped the composer.
      if (!holds(el)) {
        const written = await writeAndConfirm(() => el, text, { attempts: 1, log });
        if (!written) {
          if (log) log("this surface did not take the text → trying the next one");
          continue; // the next attempt clears the surface itself
        }
      }

      let lastBtn = null;
      for (let attempt = 1; attempt <= 3; attempt += 1) {
        const btn = await waitForEnabledSend(
          () => (findSend ? findSend(el) : null),
          attempt === 1 ? sendTimeout : 2500,
          sendDisabledCheck
        );
        lastBtn = btn;
        const usable =
          btn &&
          !isDisabled(btn) &&
          isVisible(btn) &&
          (!sendDisabledCheck || !sendDisabledCheck(btn));
        if (usable) {
          await sleep(settleMs);
          if (holds(el)) return { input: el, btn };
          if (log) log("the text was wiped while the send control was awaited");
          break; // the app cleared it: write it again, once
        }
        // No usable control: several apps (Kimi) accept Enter, and the button
        // only appears once the page has re-rendered — press Enter and see.
        if (log) {
          log(
            `no usable send control (attempt ${attempt}, found=${btn ? "yes" : "no"}) ` +
              "→ trying Enter"
          );
        }
        pressEnter(el);
        await sleep(settleMs);
        if (!holds(el)) return { input: el, btn: null }; // the app took it
      }
      if (log) {
        log(
          "the send control stayed unusable — clearing this surface and trying the next one " +
            `(found=${lastBtn ? lastBtn.tagName + "." + (lastBtn.className || "") : "none"})`
        );
      }
      try {
        if (isContentEditable(el)) el.textContent = "";
        else clearNativeValue(el);
        notifyInput(el);
      } catch (_) {}
    }
    return null;
  }

  /**
   * Click the send control and check that the message was accepted.
   *
   * "Accepted" = the composer no longer holds the text (the app cleared it
   * when it sent the message). Falls back to Enter, which some apps accept
   * instead of the button.
   *
   * `sentCheck` lets a provider add its own confirmation: on Gemini the send
   * button morphs into a "Stop response" button the instant the message
   * leaves, which is a much more reliable signal than the composer text
   * clearing (that one can lag).
   */
  async function clickSend({ input, btn, findSend, text, log, sentCheck = null } = {}) {
    const want = String(text ?? "").trim();
    const accepted = () => !want || !textMatches(editorText(input), want);
    if (btn) {
      try {
        btn.focus();
      } catch (_) {}
      if (clickEl(btn)) {
        await sleep(400);
        if (accepted()) return true;
        if (sentCheck) {
          for (let i = 0; i < 12; i++) {
            if (sentCheck()) return true;
            await sleep(200);
          }
        }
      }
    }
    // Enter submits on several apps
    pressEnter(input);
    await sleep(300);
    if (accepted()) return true;
    const again = findSend ? findSend(input) : null;
    if (again && !isDisabled(again)) {
      clickEl(again);
      await sleep(300);
    }
    const ok = accepted();
    if (!ok && log) log("the message was not accepted (the composer still holds the text)");
    return ok;
  }

  /* ------------------------------------------------------------------ */
  /* Premium markdown extraction (no physical clicks)                    */
  /* ------------------------------------------------------------------ */

  function getReactFiber(el) {
    if (!el) return null;
    const key = Object.keys(el).find(
      (k) => k.startsWith("__reactFiber$") || k.startsWith("__reactInternalInstance$")
    );
    return key ? el[key] : null;
  }

  /** Navigate fiber path like "$0.return.return.return" (DeepSeek exporter). */
  function navigateFiberPath(el, pathStr) {
    let f = getReactFiber(el);
    if (!f || !pathStr) return null;
    const steps = String(pathStr).replace(/^\$0\.?/, "").split(".").filter(Boolean);
    for (const step of steps) {
      if (!f) return null;
      f = f[step];
    }
    return f || null;
  }

  /**
   * Read original markdown from React state — same source the Copy button uses.
   * DeepSeek: memoizedProps.markdown on .ds-markdown fiber.
   * No physical/synthetic click required.
   */
  /**
   * Content scripts live in an ISOLATED world: React fiber expandos set by the
   * page are not visible here. Ask the MAIN-world bridge (page_bridge.js),
   * synchronously, through a DOM event + attributes on the element.
   */
  /**
   * Diagnostics: how was the last answer read?  "react" (original markdown,
   * exact) > "clipboard" > "html" / "dom" (rebuilt from the rendered page:
   * backslash escapes, $…$ and auto-linked words may be altered).
   * Stored on window: base.js is re-injected before every job.
   */
  const SOURCE_RANK = { react: 4, clipboard: 3, html: 2, dom: 1 };
  function noteExtractSource(src) {
    try {
      const cur = global.__NEMAPI_EXTRACT_SOURCE__;
      if (!cur || (SOURCE_RANK[src] || 0) > (SOURCE_RANK[cur] || 0)) global.__NEMAPI_EXTRACT_SOURCE__ = src;
    } catch (_) {}
  }
  function resetExtractSource() {
    try {
      global.__NEMAPI_EXTRACT_SOURCE__ = null;
    } catch (_) {}
  }
  function getExtractSource() {
    try {
      return global.__NEMAPI_EXTRACT_SOURCE__ || null;
    } catch (_) {
      return null;
    }
  }
  /** Is the MAIN-world page bridge (page_bridge.js) installed in this tab? */
  function bridgeReady() {
    try {
      return document.documentElement.getAttribute("data-nemapi-bridge") === "1";
    } catch (_) {
      return false;
    }
  }

  function readMarkdownViaPage(el, propNames) {
    if (!el || !el.setAttribute || typeof document === "undefined") return "";
    try {
      el.removeAttribute("data-nemapi-md");
      el.setAttribute("data-nemapi-md-req", (propNames || []).join(","));
      document.dispatchEvent(new CustomEvent("nemapi:read-md"));
      const md = el.getAttribute("data-nemapi-md");
      el.removeAttribute("data-nemapi-md");
      el.removeAttribute("data-nemapi-md-req");
      return md || "";
    } catch (_) {
      return "";
    }
  }

  /**
   * KaTeX / MathJax render `$…$` as math: the source text disappears from the
   * DOM text (fatal for shell commands like `"$c" --version` inside a tool
   * call). Put the original TeX source back, with its `$` delimiters.
   */
  function restoreMath(root) {
    if (!root || !root.querySelectorAll) return root;
    const texOf = (el) => {
      const a = el.querySelector('annotation[encoding="application/x-tex"], annotation[encoding*="tex" i]');
      return a ? a.textContent : null;
    };
    const swap = (el, text) => {
      const doc = el.ownerDocument || document;
      if (el.parentNode) el.parentNode.replaceChild(doc.createTextNode(text), el);
    };
    try {
      root.querySelectorAll(".katex-display").forEach((d) => {
        const t = texOf(d);
        if (t != null) swap(d, "$$" + t + "$$");
      });
      root.querySelectorAll(".katex").forEach((k) => {
        const t = texOf(k);
        if (t != null) swap(k, "$" + t + "$");
      });
      root.querySelectorAll("mjx-container").forEach((m) => {
        const t = texOf(m);
        if (t != null) swap(m, (m.getAttribute("display") === "true" ? "$$" : "$") + t + (m.getAttribute("display") === "true" ? "$$" : "$"));
      });
    } catch (_) {}
    return root;
  }

  function extractReactMarkdown(el, propNames) {
    if (!el) return "";
    if (!getReactFiber(el)) {
      // isolated world (normal case for an extension): go through the page bridge
      const viaPage = readMarkdownViaPage(el, propNames);
      if (viaPage && viaPage.trim()) noteExtractSource("react");
      return viaPage ? viaPage.trim() : "";
    }
    const names = propNames || [
      "markdown", "content", "text", "rawContent", "source",
      "message", "value", "answer", "response", "children",
    ];

    // Proven DeepSeek paths (Chat Exporter)
    const knownPaths = [
      "$0.return.return.return",
      "$0.return.return",
      "$0.return",
      "$0.child.return",
      "$0.child.child.return",
    ];
    for (const path of knownPaths) {
      try {
        const fiber = navigateFiberPath(el, path);
        const props = fiber && (fiber.memoizedProps || fiber.pendingProps);
        if (props && typeof props.markdown === "string" && props.markdown.trim().length > 5) {
          return props.markdown.trim();
        }
      } catch (_) {}
    }

    const startFiber = getReactFiber(el);
    if (!startFiber) return "";
    const seen = new Set();
    const queue = [startFiber];
    let best = "";
    let bestScore = -1;
    let depth = 0;
    while (queue.length && depth < 120) {
      const f = queue.shift();
      depth += 1;
      if (!f || seen.has(f)) continue;
      seen.add(f);
      const props = f.memoizedProps || f.pendingProps;
      if (props && typeof props === "object") {
        for (const name of names) {
          if (!Object.prototype.hasOwnProperty.call(props, name)) continue;
          let v = props[name];
          if (v && typeof v === "object" && typeof v.markdown === "string") v = v.markdown;
          if (typeof v !== "string") continue;
          const s = v.trim();
          if (s.length < 3) continue;
          let score = s.length;
          if (name === "markdown") score += 100000;
          if (s.includes("```")) score += 8000;
          if (s.includes("\n")) score += 400;
          if (/^#{1,6}\s/m.test(s)) score += 500;
          if (score > bestScore) {
            bestScore = score;
            best = s;
          }
        }
      }
      if (f.child) queue.push(f.child);
      if (f.sibling) queue.push(f.sibling);
      if (f.return && depth < 60) queue.push(f.return);
    }
    return best;
  }

  /**
   * Convert a rendered markdown HTML subtree back to reasonably faithful MD.
   * Handles pre/code, headings, lists, tables, links, emphasis.
   */
  /**
   * Remove UI line-number gutters that leak into code text, e.g.:
   *   "1| def foo():"  or  "  12  const x = 1"
   */
  function stripLeadingLineNumbers(text) {
    const src = String(text || "");
    const lines = src.split("\n");
    if (lines.length < 3) return src;
    // a tool call is never shown with a gutter — and its JSON must stay intact
    if (/<tool[\s>:]|<function=|DSML/i.test(src)) return src;
    const numRe = /^\s*(\d{1,5})(?:\s*[|：:.\)]\s|\s{2,}\S|\s*$)/;
    const nonEmpty = lines.filter((l) => l.trim());
    let numbered = 0;
    let consecutive = 0;
    let prev = null;
    for (const line of nonEmpty) {
      const m = line.match(numRe);
      if (!m) continue;
      numbered += 1;
      const n = Number(m[1]);
      if (prev !== null && n === prev + 1) consecutive += 1;
      prev = n;
    }
    // Real gutters number (almost) every line, consecutively.
    if (numbered < 3 || numbered < nonEmpty.length * 0.8 || consecutive < (numbered - 1) * 0.8) {
      return src;
    }
    return lines
      .map((line) =>
        line
          .replace(/^\s*\d+\s*[|：:.\)]\s?/, "")
          .replace(/^\s*\d{1,5}\s{2,}/, "")
      )
      .join("\n");
  }

  /** Code fence long enough for a body that itself contains ``` runs. */
  function fenceFor(body) {
    let longest = 0;
    String(body || "").replace(/`{3,}/g, (m) => {
      longest = Math.max(longest, m.length);
      return m;
    });
    return "`".repeat(Math.max(3, longest + 1));
  }

  /**
   * A markdown renderer turns bare "m.to" / "a.sh" into links; writing them
   * back as [m.to](https://m.to/) would corrupt code. Keep only the label
   * when the link is just the label itself.
   */
  function linkToMarkdown(label, href) {
    if (!href) return label;
    const bare = (x) => String(x || "").trim().replace(/^(?:https?:\/\/|mailto:)/i, "").replace(/\/+$/, "").toLowerCase();
    if (!label || bare(label) === bare(href)) return label || href;
    return "[" + label + "](" + href + ")";
  }

  /** Strip line numbers from every fenced code block in a markdown string. */
  function cleanMarkdownCodeFences(md) {
    return String(md || "").replace(/```([^\n`]*)\n([\s\S]*?)```/g, (full, lang, body) => {
      return "```" + lang + "\n" + stripLeadingLineNumbers(body).replace(/\n$/, "") + "\n```";
    });
  }

  function htmlToMarkdown(root) {
    if (!root) return "";
    noteExtractSource("html");
    const clone = root.cloneNode(true);
    restoreMath(clone);
    const kill = [
      "button",
      "[role='button']",
      "[class*='copy']",
      "[class*='download']",
      "[class*='toolbar']",
      "[class*='action-bar']",
      "[class*='icon-button']",
      "[class*='line-number']",
      "[class*='linenumber']",
      "[class*='line-num']",
      ".linenumber",
      ".line-numbers",
      "[data-line-number]",
      "svg",
      "style",
      "script",
      "noscript",
    ];
    for (const s of kill) {
      try {
        clone.querySelectorAll(s).forEach((n) => n.remove());
      } catch (_) {}
    }

    function walk(node) {
      if (!node) return "";
      if (node.nodeType === 3) return node.textContent || "";
      if (node.nodeType !== 1) return "";
      const tag = node.tagName.toLowerCase();
      if (tag === "pre") {
        // Remove gutter / line-number nodes before reading text
        node.querySelectorAll(
          "[class*='line-number'],[class*='linenumber'],[class*='line-num'],.line-numbers,[data-line-number],.code-block-gutter,.cm-gutter,.linenos"
        ).forEach((n) => n.remove());
        const code = node.querySelector("code");
        let lang = "";
        if (code) {
          const cls = code.className || "";
          const m = cls.match(/language-([a-z0-9_+-]+)/i) || cls.match(/lang-([a-z0-9_+-]+)/i);
          if (m) lang = m[1];
        }
        // Prefer textContent (preserves spaces). Avoid innerText (can collapse).
        let body = (code ? code.textContent : node.textContent) || "";
        body = body.replace(/\u00a0/g, " "); // nbsp → space
        body = stripLeadingLineNumbers(body);
        const fence = fenceFor(body);
        return "\n\n" + fence + lang + "\n" + body.replace(/\n$/, "") + "\n" + fence + "\n\n";
      }
      if (tag === "code" && (!node.parentElement || node.parentElement.tagName.toLowerCase() !== "pre")) {
        return "`" + (node.textContent || "").replace(/`/g, "\\`") + "`";
      }
      if (/^h[1-6]$/.test(tag)) {
        const level = Number(tag[1]);
        return "\n\n" + "#".repeat(level) + " " + Array.from(node.childNodes).map(walk).join("").trim() + "\n\n";
      }
      if (tag === "strong" || tag === "b") {
        return "**" + Array.from(node.childNodes).map(walk).join("") + "**";
      }
      if (tag === "em" || tag === "i") {
        return "*" + Array.from(node.childNodes).map(walk).join("") + "*";
      }
      if (tag === "a") {
        const href = node.getAttribute("href") || "";
        const label = Array.from(node.childNodes).map(walk).join("") || href;
        return linkToMarkdown(label, href);
      }
      if (tag === "li") {
        return "\n- " + Array.from(node.childNodes).map(walk).join("").trim();
      }
      if (tag === "ul" || tag === "ol") {
        return "\n" + Array.from(node.childNodes).map(walk).join("") + "\n";
      }
      if (tag === "br") return "\n";
      if (tag === "p" || tag === "div") {
        // Detect code-line containers (Qwen/Claude often use div.line without <pre>)
        const cls = (node.className && String(node.className)) || "";
        const isCodeLine =
          /\b(line|code-line|hljs|cm-line|token-line)\b/i.test(cls) ||
          node.getAttribute("data-line") != null;
        if (isCodeLine) {
          // Preserve exact indentation — never trim leading spaces
          const lineText = node.textContent || "";
          return lineText.replace(/\r/g, "") + "\n";
        }
        // Whole code-block wrapper without a pre
        // (a wrapper that holds a <pre> is walked normally: its header — language
        // label, copy button — must not be glued to the code)
        if (/(?:^|\s)(?:[\w]+-)*(code-block|codeblock|highlight|hljs|prism|cm-content|monaco)(?=\s|$)/i.test(cls) && !node.querySelector("pre")) {
          node.querySelectorAll(
            "[class*='line-number'],[class*='linenumber'],[class*='line-num'],.line-numbers,[data-line-number],button,svg"
          ).forEach((n) => n.remove());
          let body = node.textContent || "";
          body = stripLeadingLineNumbers(body);
          // If children already walked would double — use textContent once
          const fence = fenceFor(body);
          return "\n\n" + fence + "\n" + body.replace(/\n$/, "") + "\n" + fence + "\n\n";
        }
        const inner = Array.from(node.childNodes).map(walk).join("");
        if (!inner || !inner.replace(/\s/g, "")) return "";
        // Preserve internal indentation: only strip blank edges, not per-line indent
        return "\n\n" + inner.replace(/^\n+|\n+$/g, "") + "\n\n";
      }
      if (tag === "blockquote") {
        const inner = Array.from(node.childNodes).map(walk).join("").trim();
        return "\n\n" + inner.split("\n").map((l) => "> " + l).join("\n") + "\n\n";
      }
      if (tag === "table") {
        // crude table → markdown
        const rows = Array.from(node.querySelectorAll("tr"));
        if (!rows.length) return "";
        const cells = rows.map((tr) =>
          Array.from(tr.querySelectorAll("th,td")).map((c) => (c.textContent || "").trim().replace(/\|/g, "\\|"))
        );
        const header = cells[0];
        const sep = header.map(() => "---");
        const lines = [
          "| " + header.join(" | ") + " |",
          "| " + sep.join(" | ") + " |",
          ...cells.slice(1).map((r) => "| " + r.join(" | ") + " |"),
        ];
        return "\n\n" + lines.join("\n") + "\n\n";
      }
      return Array.from(node.childNodes).map(walk).join("");
    }

    let md = walk(clone);
    md = md.replace(/[ \t]+\n/g, "\n");
    md = md.replace(/\n{3,}/g, "\n\n");
    return md.trim();
  }

  /**
   * Install a MAIN-world clipboard hook so page JS (Qwen/Claude/…) writeText calls
   * are visible to the isolated content-script world.
   * Content-script patches of navigator.clipboard do NOT affect the page realm —
   * that was why clipboard intercept silently failed while the real OS clipboard
   * still received the text.
   */
  function ensurePageClipboardHook() {
    if (global.__NEMAPI_PAGE_CLIP_READY__) return;
    global.__NEMAPI_PAGE_CLIP_READY__ = true;
    global.__NEMAPI_LAST_CLIP__ = "";

    // Content-script side: receive captures from the page world
    window.addEventListener(
      "__nemapi_clip_capture__",
      (e) => {
        try {
          const t = e && e.detail != null ? String(e.detail) : "";
          if (t && t !== "[object Object]" && t.length >= (global.__NEMAPI_LAST_CLIP__ || "").length) {
            global.__NEMAPI_LAST_CLIP__ = t;
          }
        } catch (_) {}
      },
      true
    );

    // CSP blocks inline <script> injection on ChatGPT/Kimi/etc.
    // Only use chrome.scripting world:MAIN via the background service worker.
    // Chrome < 111 cannot inject there: page_bridge.js (content script, MAIN)
    // exposes the same hook on the page, which this world can only call by
    // asking it through a DOM event.
    try {
      if (window.__nemapiInstallClipHook) {
        window.__nemapiInstallClipHook();
      } else {
        document.dispatchEvent(new CustomEvent("nemapi:install-clip-hook"));
      }
    } catch (_) {}
    try {
      chrome.runtime.sendMessage({ action: "installPageClipHook" }).catch(() => {});
    } catch (_) {}
  }

  /**
   * Try synthetic click on a Copy button while intercepting the page's clipboard writes.
   * Returns markdown string or "".
   *
   * Strategy (in order):
   *  1) MAIN-world hook (page navigator.clipboard.writeText) via CustomEvent
   *  2) Isolated-world writeText patch (works only if page shares the same object — rare)
   *  3) document "copy" event
   *  4) navigator.clipboard.readText() after the click (needs clipboardRead permission)
   */
  /**
   * True if the button is a *code-block* copy control (not the full-message one).
   */
  function isCodeBlockCopyButton(el) {
    if (!el) return false;
    if (
      el.closest(
        "pre, code, .md-code-block, [class*='md-code'], [class*='code-block'], " +
          "[class*='CodeBlock'], [class*='codeblock'], [class*='hljs'], " +
          "[class*='syntax'], [data-code-block], .ds-code-block"
      )
    ) {
      return true;
    }
    const label = (
      (el.getAttribute("aria-label") || "") +
      " " +
      (el.title || "") +
      " " +
      (el.innerText || el.textContent || "")
    ).toLowerCase();
    if (/copy\s*code|copy\s*snippet|复制代码|复制片段|copy\s*block/i.test(label)) {
      return true;
    }
    const parent = el.parentElement;
    if (parent) {
      const siblings = parent.querySelectorAll(
        "button, [role='button'], .ds-icon-button, [class*='icon-button']"
      );
      if (siblings.length <= 2 && el.closest("pre, [class*='code']")) return true;
    }
    return false;
  }

  /**
   * True if the element lives inside a *user* / human prompt bubble.
   * Critical: never click a copy button that belongs to the user message.
   */
  function isUserMessageContext(el) {
    if (!el) return false;
    if (
      el.closest(
        // Generic
        '[data-role="user"], [data-message-author-role="user"], [data-message-author-role="human"], ' +
          '[data-testid*="user-message" i], [data-testid*="user_message" i], ' +
          // ChatGPT
          '[data-message-author-role="user"], ' +
          // Gemini
          "user-query, .user-query, .query-text, [class*='user-query'], [class*='query-content'], " +
          // Claude
          '[data-testid="user-message"], .font-user-message, ' +
          // DeepSeek / Qwen / Kimi / Z.ai class heuristics
          "[class*='user-message'], [class*='human-message'], [class*='UserMessage'], " +
          "[class*='user_message'], [class*='prompt-message'], [class*='question-message'], " +
          // Role attributes
          '[role="user"], [data-author="user"], [data-author="human"]'
      )
    ) {
      return true;
    }
    // Aria / data labels on ancestors
    let n = el;
    for (let i = 0; i < 8 && n; i++) {
      const role = (
        (n.getAttribute && (n.getAttribute("data-role") || n.getAttribute("data-message-author-role") || "")) +
        " " +
        (n.getAttribute && (n.getAttribute("aria-label") || "")) +
        " " +
        (n.className && String(n.className)) ||
        ""
      ).toLowerCase();
      if (/\b(user|human|prompt|query|question)\b/.test(role) && !/\b(assistant|model|bot|ai|response|answer)\b/.test(role)) {
        // Strong user signal without assistant signal
        if (/\b(user-message|human-message|user-query|data-role.?=.?user|author-role.?=.?user)\b/.test(role)) {
          return true;
        }
      }
      n = n.parentElement;
    }
    return false;
  }

  /**
   * True if the element lives inside an *assistant* / model response bubble.
   */
  function isAssistantMessageContext(el) {
    if (!el) return false;
    if (
      el.closest(
        '[data-role="assistant"], [data-message-author-role="assistant"], [data-message-author-role="model"], ' +
          '[data-testid*="assistant" i], [data-testid*="model-response" i], ' +
          "model-response, .model-response, .model-response-text, message-content.model-response-text, " +
          '[data-testid="assistant-message"], .font-claude-message, ' +
          "[class*='assistant-message'], [class*='bot-message'], [class*='model-message'], " +
          "[class*='ai-message'], [class*='response-message'], .ds-message, " +
          "[class*='AgentMessage'], [class*='AssistantMessage']"
      )
    ) {
      return true;
    }
    return false;
  }

  /**
   * Score a candidate copy button: higher = more likely the *assistant message* copy.
   * Strongly rejects user-message and code-block buttons.
   * Prefers toolbars with regenerate / like / dislike / share.
   */
  function scoreMessageCopyButton(el) {
    if (!el || !isVisible(el) || isCodeBlockCopyButton(el)) return -1;
    // Hard reject: belongs to a user/human prompt bubble
    if (isUserMessageContext(el) && !isAssistantMessageContext(el)) return -1;

    let score = 10;
    const label = (
      (el.getAttribute("aria-label") || "") +
      " " +
      (el.title || "") +
      " " +
      (el.innerText || el.textContent || "") +
      " " +
      (el.getAttribute("data-testid") || "")
    ).toLowerCase();

    // Explicit assistant/response copy labels
    if (
      /copy\s*(response|message|answer|full|prompt)?|copier\s*(la\s*)?(réponse|message)?|复制(全部|消息|回答|回复|内容)?/i.test(
        label
      )
    ) {
      score += 40;
    }
    if (/copy-response|copy-turn|action-bar-copy|copy_response/i.test(label)) score += 60;
    if (/copy|copier|复制|clipboard/.test(label)) score += 5;

    // Reject labels that clearly mean "copy prompt / user input"
    if (/copy\s*(prompt|query|question|input)|复制(提问|问题|输入)/i.test(label)) return -1;

    if (isAssistantMessageContext(el)) score += 35;
    if (isUserMessageContext(el)) score -= 80;

    // Sibling toolbar: regenerate / like / dislike / share = assistant action bar
    let group = el.parentElement;
    for (let i = 0; i < 5 && group; i++) {
      const groupText = (group.innerText || group.textContent || "").toLowerCase();
      const groupAria = (
        (group.getAttribute && (group.getAttribute("aria-label") || "")) +
        " " +
        (group.className && String(group.className)) ||
        ""
      ).toLowerCase();
      const btns = group.querySelectorAll(
        "button, [role='button'], .ds-icon-button, [class*='icon-button']"
      );
      if (btns.length >= 3) score += 15;
      if (btns.length >= 4) score += 10;
      if (
        /regenerat|retry|重新生成|like|dislike|thumb|share|分享|点赞|点踩|good\s*response|bad\s*response|report|修改/i.test(
          groupText + " " + groupAria
        )
      ) {
        score += 40;
      }
      if (/message\s*actions|action-bar|response-actions|footer-actions/i.test(groupAria)) {
        score += 25;
      }
      group = group.parentElement;
    }
    return score;
  }

  /**
   * Collect copy-button candidates under `root`, excluding user-message /
   * code-block controls. Returns sorted [{el, score}] (best first).
   */
  function collectAssistantCopyCandidates(root, buttonSelectors) {
    const sels = buttonSelectors || [
      'button[data-testid="action-bar-copy"]',
      'button[aria-label*="Copy response" i]',
      'button[aria-label*="Copy message" i]',
      'button[aria-label*="Copy" i]',
      'button[aria-label*="Copier" i]',
      'button[aria-label*="复制"]',
      'button[title*="Copy" i]',
    ];
    if (!root) root = document.body;
    const candidates = [];
    const seen = new Set();
    for (const s of sels) {
      try {
        const nodes = root.querySelectorAll(s);
        for (const n of nodes) {
          if (seen.has(n)) continue;
          seen.add(n);
          const label = (
            (n.getAttribute("aria-label") || "") +
            " " +
            (n.title || "") +
            " " +
            (n.innerText || n.textContent || "")
          ).toLowerCase();
          if (s === "button" && !/copy|copier|复制|clipboard/.test(label)) continue;
          if (!isVisible(n)) continue;
          if (isCodeBlockCopyButton(n)) continue;
          // Hard skip pure user-message copies
          if (isUserMessageContext(n) && !isAssistantMessageContext(n)) continue;
          const sc = scoreMessageCopyButton(n);
          if (sc >= 0) candidates.push({ el: n, score: sc });
        }
      } catch (_) {}
    }
    if (!candidates.length) {
      try {
        const all = root.querySelectorAll("button, [role='button']");
        for (const n of all) {
          if (seen.has(n)) continue;
          const label = (
            (n.getAttribute("aria-label") || "") +
            " " +
            (n.title || "") +
            " " +
            (n.innerText || n.textContent || "")
          ).toLowerCase();
          if (!/copy|copier|复制|clipboard/.test(label)) continue;
          if (!isVisible(n) || isCodeBlockCopyButton(n)) continue;
          if (isUserMessageContext(n) && !isAssistantMessageContext(n)) continue;
          const sc = scoreMessageCopyButton(n);
          if (sc >= 0) candidates.push({ el: n, score: sc });
        }
      } catch (_) {}
    }
    candidates.sort((a, b) => b.score - a.score);
    return candidates;
  }

  /**
   * Heuristic: does `clip` look like the *user prompt* rather than the assistant answer?
   * Used as a safety net after clipboard intercept.
   */
  function looksLikeUserPrompt(clip, domAssistantText) {
    if (!clip || typeof clip !== "string") return false;
    const c = clip.trim();
    if (c.length < 2) return true;
    // Matches known last user-query bubbles on the page
    try {
      const userNodes = document.querySelectorAll(
        'user-query, [data-message-author-role="user"], [data-role="user"], ' +
          '[data-testid="user-message"], .user-query, [class*="user-message"], [class*="human-message"]'
      );
      for (const n of userNodes) {
        const ut = (n.innerText || n.textContent || "").trim();
        if (ut.length > 5 && (c === ut || (c.length <= ut.length + 5 && ut.startsWith(c.slice(0, Math.min(80, c.length)))))) {
          return true;
        }
        // clipboard is a short prefix of user text
        if (ut.length > 20 && c.length > 10 && ut.startsWith(c.slice(0, Math.min(c.length, 120)))) {
          return true;
        }
      }
    } catch (_) {}
    // If we have a solid DOM assistant extract, and clipboard is wildly different & shorter → suspicious
    if (domAssistantText && domAssistantText.trim().length > 40) {
      const d = domAssistantText.trim();
      if (c.length < d.length * 0.25 && !d.includes(c.slice(0, Math.min(40, c.length)))) {
        // clipboard much shorter and not contained in DOM answer → likely wrong
        return true;
      }
    }
    return false;
  }

  async function tryClipboardFromCopyButton(root, buttonSelectors, opts) {
    opts = opts || {};
    const domHint = opts.domAssistantText || "";
    ensurePageClipboardHook();
    // Give MAIN-world inject a moment on first use (message is async)
    if (!global.__NEMAPI_CLIP_HOOK_WAITED__) {
      global.__NEMAPI_CLIP_HOOK_WAITED__ = true;
      await sleep(120);
    }

    if (!root) root = document.body;
    const candidates = collectAssistantCopyCandidates(root, buttonSelectors);
    // Require a minimum score so we never click a user-message / junk button
    const btn =
      candidates.length && candidates[0].score >= 20 ? candidates[0].el : null;
    if (!btn) return "";

    // Snapshot before click so we can detect a fresh write
    const before = global.__NEMAPI_LAST_CLIP__ || "";
    global.__NEMAPI_LAST_CLIP__ = "";

    // Prefer plain/markdown over HTML. ChatGPT often writes both text/plain AND
    // text/html; HTML is longer so a naive "longest wins" picks the wrong one.
    let capturedPlain = "";
    let capturedHtml = "";
    const looksHtml = (s) =>
      /<\/?(p|div|span|strong|em|br|ul|ol|li|h[1-6]|a|code|pre|table)\b/i.test(s || "");

    const capture = (text, mimeHint) => {
      if (typeof text !== "string" || !text || text === "[object Object]") return;
      const isHtml =
        mimeHint === "text/html" ||
        (mimeHint !== "text/plain" && looksHtml(text) && text.includes("<"));
      if (isHtml) {
        if (text.length > capturedHtml.length) capturedHtml = text;
      } else {
        if (text.length > capturedPlain.length) capturedPlain = text;
      }
    };

    // Isolated-world patch (harmless if page uses its own clipboard object)
    const clip = navigator.clipboard;
    const origWrite = clip && clip.writeText ? clip.writeText.bind(clip) : null;
    const origWriteFull = clip && clip.write ? clip.write.bind(clip) : null;
    if (clip) {
      try {
        clip.writeText = async (text) => {
          capture(text, "text/plain");
          if (origWrite) return origWrite(text);
        };
      } catch (_) {}
      try {
        clip.write = async (items) => {
          try {
            for (const item of items || []) {
              if (item && item.types) {
                // Prefer text/plain first when both are present
                const types = Array.from(item.types || []);
                const ordered = [
                  ...types.filter((t) => t === "text/plain"),
                  ...types.filter((t) => t !== "text/plain" && String(t).startsWith("text/")),
                ];
                for (const type of ordered) {
                  const blob = await item.getType(type);
                  capture(await blob.text(), String(type));
                }
              }
            }
          } catch (_) {}
          if (origWriteFull) return origWriteFull(items);
        };
      } catch (_) {}
    }

    const onCopy = (e) => {
      try {
        const plain =
          (e.clipboardData && e.clipboardData.getData("text/plain")) ||
          (window.clipboardData && window.clipboardData.getData("Text")) ||
          "";
        if (plain) capture(plain, "text/plain");
        const html = e.clipboardData && e.clipboardData.getData("text/html");
        if (html) capture(html, "text/html");
      } catch (_) {}
    };
    document.addEventListener("copy", onCopy, true);

    try {
      btn.focus && btn.focus();
      for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
        try {
          btn.dispatchEvent(
            new PointerEvent(type, {
              bubbles: true,
              cancelable: true,
              view: window,
              pointerType: "mouse",
            })
          );
        } catch (_) {
          btn.dispatchEvent(
            new MouseEvent(type.replace("pointer", "mouse"), {
              bubbles: true,
              cancelable: true,
              view: window,
            })
          );
        }
      }
      try {
        btn.click();
      } catch (_) {}

      // Page handlers are often async (React onClick → writeText). Poll briefly.
      for (let i = 0; i < 5; i++) {
        await sleep(i < 2 ? 40 : 70);
        if (global.__NEMAPI_LAST_CLIP__ && global.__NEMAPI_LAST_CLIP__ !== before) {
          capture(global.__NEMAPI_LAST_CLIP__);
        }
        if (capturedPlain && capturedPlain.length > 10) break;
      }
    } finally {
      document.removeEventListener("copy", onCopy, true);
      if (clip && origWrite) {
        try {
          clip.writeText = origWrite;
        } catch (_) {}
      }
      if (clip && origWriteFull) {
        try {
          clip.write = origWriteFull;
        } catch (_) {}
      }
    }

    if (!capturedPlain && global.__NEMAPI_LAST_CLIP__ && global.__NEMAPI_LAST_CLIP__ !== before) {
      capture(global.__NEMAPI_LAST_CLIP__);
    }

    // Last resort: read the system clipboard (requires clipboardRead permission)
    if ((!capturedPlain || capturedPlain.length < 10) && clip && typeof clip.readText === "function") {
      try {
        const read = await clip.readText();
        if (read && read.length > 10 && read !== "[object Object]" && read !== before) {
          capture(read, "text/plain");
        }
      } catch (_) {}
    }

    let result = capturedPlain;
    // If we only got HTML, convert tags to markdown-ish plain text
    if ((!result || result.length < 10) && capturedHtml) {
      result = htmlStringToMarkdown(capturedHtml);
    }
    // Safety: never return raw HTML blobs
    if (result && looksHtml(result) && result.includes("<")) {
      result = htmlStringToMarkdown(result);
    }

    if (result && result !== "[object Object]" && !/^\[object\s+\w+\]$/i.test(result.trim())) {
      result = result.trim();
      // Final safety: reject if clipboard looks like the user prompt
      if (looksLikeUserPrompt(result, domHint)) {
        return "";
      }
      return result;
    }
    return "";
  }

  /** Convert an HTML string (from clipboard text/html) into approximate markdown. */
  function htmlStringToMarkdown(html) {
    if (!html || typeof html !== "string") return "";
    try {
      const doc = new DOMParser().parseFromString(html, "text/html");
      const body = doc.body || doc.documentElement;
      if (!body) return stripTagsFallback(html);
      return htmlToMarkdown(body) || stripTagsFallback(html);
    } catch (_) {
      return stripTagsFallback(html);
    }
  }

  function stripTagsFallback(html) {
    return String(html || "")
      .replace(/<br\s*\/?>/gi, "\n")
      .replace(/<\/p>/gi, "\n\n")
      .replace(/<\/div>/gi, "\n")
      .replace(/<\/h[1-6]>/gi, "\n\n")
      .replace(/<\/li>/gi, "\n")
      .replace(/<li[^>]*>/gi, "- ")
      .replace(/<(strong|b)[^>]*>/gi, "**")
      .replace(/<\/(strong|b)>/gi, "**")
      .replace(/<(em|i)[^>]*>/gi, "*")
      .replace(/<\/(em|i)>/gi, "*")
      .replace(/<[^>]+>/g, "")
      .replace(/&nbsp;/g, " ")
      .replace(/&amp;/g, "&")
      .replace(/&lt;/g, "<")
      .replace(/&gt;/g, ">")
      .replace(/&quot;/g, '"')
      .replace(/\n{3,}/g, "\n\n")
      .trim();
  }

  /* ------------------------------------------------------------------ */
  /* File / image attachments (multimodal requests)                      */
  /* ------------------------------------------------------------------ */

  /** Decode base64 (data URL or bare) into bytes, binary-safe. */
  function base64ToBytes(b64) {
    const clean = String(b64 || "").replace(/\s+/g, "").replace(/^data:[^,]*,/, "");
    const bin = atob(clean);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i += 1) out[i] = bin.charCodeAt(i);
    return out;
  }

  /** Data URL or bare base64 → File the page can consume. */
  function fileFromBase64(b64, name, mime) {
    return new File([base64ToBytes(b64)], name || "image.png", {
      type: mime || "application/octet-stream",
    });
  }

  /**
   * Put files on the page's <input type=file> and fire what React listens to.
   * Proven on chat.deepseek.com: the thumbnail appears without any click on
   * the attach button (the input is hidden but live in the DOM).
   */
  async function attachFiles(input, files) {
    if (!input) throw new Error("attachFiles: no file input");
    const list = Array.from(files || []).filter(Boolean);
    if (!list.length) return 0;
    const dt = new DataTransfer();
    for (const f of list) dt.items.add(f);
    input.files = dt.files;
    try {
      input.dispatchEvent(new Event("input", { bubbles: true }));
    } catch (_) {}
    try {
      input.dispatchEvent(new Event("change", { bubbles: true }));
    } catch (_) {}
    await sleep(120);
    return input.files ? input.files.length : list.length;
  }

  /**
   * Can this file input carry an image?
   *
   * A page can hold SEVERAL file inputs and the wrong one is silently
   * ignored: ChatGPT's composer form carries four (camera / image+video /
   * images only / any file) and ChatGLM's avatar menu has one of its own.
   * Two rules narrow the choice:
   *
   *  - a `[capture]` input is the CAMERA, never the gallery;
   *  - a disabled input is not listening.
   *
   * Both are invisible in the DOM dump: the app renders the camera input
   * first, so a bare "input[type=file]" lands on it and the upload is lost
   * without a word.
   */
  function fileInputUsable(el) {
    if (!el) return false;
    try {
      if (el.disabled) return false;
      if (el.hasAttribute("capture")) return false;
      return true;
    } catch (_) {
      return false;
    }
  }

  /**
   * The page's file input, if it is mounted.
   *
   * A file input is almost always HIDDEN (display:none) — the app shows a
   * button instead — so visibility is not a criterion here. The provider's
   * own selectors come first, a bare "input[type=file]" is the last resort.
   * Open shadow roots are scanned too, but only when the direct lookup found
   * nothing.
   */
  function findFileInput(selectors) {
    const sels = [...(selectors || []), "input[type='file']:not([capture])"];
    for (const sel of sels) {
      try {
        const el = document.querySelector(sel);
        if (el && fileInputUsable(el)) return el;
      } catch (_) {}
    }
    for (const sel of sels) {
      for (const root of openShadowRoots()) {
        try {
          const el = root.querySelector(sel);
          if (el && fileInputUsable(el)) return el;
        } catch (_) {}
      }
    }
    return null;
  }

  /** The first VISIBLE element matching one of the selectors (a button). */
  function firstVisible(selectors) {
    for (const sel of selectors || []) {
      let els = [];
      try {
        els = Array.from(document.querySelectorAll(sel));
      } catch (_) {
        continue;
      }
      for (const el of els) {
        if (el && isVisible(el)) return el;
      }
    }
    return null;
  }

  /**
   * Attach files to the provider's composer.
   *
   * Three transports, tried in this order — the sites do not agree on one:
   *
   *   1. the page's own `<input type=file>`: the BROWSER fills it (a
   *      DataTransfer built in JS) and the app is notified with the `input`
   *      + `change` events. Proven on chat.deepseek.com, and the input is
   *      present in the DOM of ChatGPT / Gemini (`input.hidden-file-input`).
   *   2. a synthetic PASTE event carrying the file, for the sites whose
   *      upload entry point is a hidden input created only when the native
   *      file chooser opens (Qwen, Claude, Kimi — the native chooser cannot
   *      be driven from a content script).
   *   3. a synthetic DROP event, for the sites with a drop zone (Gemini).
   *
   * In every case the base64 is never typed as text.
   *
   * A transport only counts as SUCCESS when the page shows it took the file.
   * Setting `input.files` from JS is not a proof: an app that ignores the
   * dispatched events (or that re-renders its composer and drops the input)
   * leaves the file nowhere, and the request would then send a prompt whose
   * image is silently missing. When no transport can prove it, the job fails
   * loudly instead.
   *
   * `openAttach` is a MENU OPENER (a button that opens a DOM menu), never
   * the item that opens the native file chooser: a native dialog blocks the
   * page until the user closes it.
   */
  async function attachFilesToPage(files, {
    inputs = [],
    openAttach = [],
    pasteTarget = null,
    dropTarget = null,
    name = "provider",
  } = {}) {
    const list = Array.from(files || []).filter(Boolean);
    if (!list.length) return 0;
    const names = list.map((f) => f.name).filter(Boolean);

    // 1. the page's own file input
    let input = findFileInput(inputs);
    if (!input && openAttach.length) {
      const btn = firstVisible(openAttach);
      if (btn) {
        try {
          btn.click();
        } catch (_) {}
        const t0 = Date.now();
        while (Date.now() - t0 < 5000) {
          await sleep(150);
          input = findFileInput(inputs);
          if (input) break;
        }
      }
    }
    if (input) {
      const inputBaseline = previewCount();
      const accepted = await attachFiles(input, list);
      // the app renders its preview asynchronously, and the final re-render
      // of an upload WIPES a prompt typed too early (the text vanishes, the
      // image stays). Waiting for the upload to finish is what saves it.
      const state = await waitUploadDone(list, { inputs, timeout: 25000 });
      if (state === "dropped") {
        // the composer was re-rendered: the app may have taken the file (the
        // preview says so) or refused it. The preview is then the only
        // witness left.
        const ok = await waitForAttachments({ names, min: 1, timeout: 10000, baseline: inputBaseline });
        if (!ok) {
          throw new Error(
            `${name}: the page took the file input but never showed the image — ` +
            `the upload was refused (the site may have changed, or it does not accept images)`
          );
        }
      }
      // The upload must be OVER before the prompt is typed: an app that
      // re-renders its composer at the end of the transfer WIPES the text
      // (the image stays, the text disappears) and the send control stays
      // disabled — the request then hangs until the watchdog.
      const settled = await waitUploadFinished(list, {
        inputs,
        baseline: inputBaseline,
        timeout: 30000,
      });
      if (!settled) {
        throw new Error(
          `${name}: the image was never taken by the page (no preview, no spinner) — ` +
          `the upload was refused (the site may have changed, or it does not accept images)`
        );
      }
      return accepted;
    }

    // 2. a synthetic paste (the app reads event.clipboardData.files)
    const baseline = previewCount();
    if (pasteTarget) {
      const target = typeof pasteTarget === "function" ? pasteTarget() : firstVisible(pasteTarget);
      if (target && pasteFiles(list, target)) {
        if (await waitPreview(baseline, { timeout: 15000 })) {
          await waitUploadFinished(list, { scope: target, baseline, timeout: 30000 });
          return list.length;
        }
      }
    }

    // 3. a synthetic drop (the site has a drop zone)
    if (dropTarget) {
      const target = typeof dropTarget === "function" ? dropTarget() : firstVisible(dropTarget);
      if (target && dropFiles(list, target)) {
        if (await waitPreview(baseline, { timeout: 15000 })) {
          await waitUploadFinished(list, { scope: target, baseline, timeout: 30000 });
          return list.length;
        }
      }
    }

    throw new Error(
      `${name}: the page did not take the image — no file input, and the paste/drop ` +
      `transports were refused (the site may have changed, or it does not accept images)`
    );
  }

  /** The number of attachment previews currently on the page. */
  function previewCount() {
    try {
      return document.querySelectorAll("img[src^='blob:'], img[src^='data:image']").length;
    } catch (_) {
      return 0;
    }
  }

  /**
   * Wait for a NEW preview: the app took the file and shows it.
   *
   * The preview is the only proof available for the paste / drop transports:
   * there is no input to inspect, and the site does not always show a
   * spinner.
   */
  async function waitPreview(baseline, { timeout = 15000, pollMs = 300 } = {}) {
    const t0 = Date.now();
    while (Date.now() - t0 < timeout) {
      if (global.__NEMAPI_STOP__) throw new Error("Stopped");
      if (previewCount() > baseline) {
        await sleep(600);
        return true;
      }
      await sleep(pollMs);
    }
    return false;
  }

  /**
   * A synthetic PASTE carrying the files.
   *
   * The app that supports it reads `event.clipboardData.files` — the same
   * route a real paste takes, without touching the user's clipboard.
   */
  function pasteFiles(files, target) {
    try {
      const dt = new DataTransfer();
      for (const f of files) dt.items.add(f);
      try {
        target.focus();
      } catch (_) {}
      const ev = new ClipboardEvent("paste", {
        clipboardData: dt,
        bubbles: true,
        cancelable: true,
      });
      target.dispatchEvent(ev);
      return true;
    } catch (_) {
      return false;
    }
  }

  /**
   * A synthetic DROP carrying the files (the site has a drop zone).
   */
  function dropFiles(files, target) {
    try {
      const dt = new DataTransfer();
      for (const f of files) dt.items.add(f);
      for (const type of ["dragenter", "dragover", "drop"]) {
        target.dispatchEvent(
          new DragEvent(type, { bubbles: true, cancelable: true, dataTransfer: dt })
        );
      }
      return true;
    } catch (_) {
      return false;
    }
  }

  /**
   * The area around the file input: the closest ancestor that also holds the
   * composer (the editable surface). Used to look for the upload spinner
   * without scanning the whole page.
   */
  function composerScopeOf(input) {
    let node = input;
    for (let i = 0; i < 6 && node; i += 1) {
      try {
        if (node.querySelector && node.querySelector("textarea, [contenteditable='true']")) return node;
      } catch (_) {}
      node = node.parentElement;
    }
    return document.body;
  }

  /** A visible spinner inside the attachment area: the upload is running. */
  function uploadSpinner(input, scope) {
    const area = scope || composerScopeOf(input);
    try {
      const els = area.querySelectorAll(
        "[class*='spinner'], [class*='loading'], [class*='progress'], [role='progressbar']"
      );
      for (const el of els) {
        if (isVisible(el)) return el;
      }
    } catch (_) {}
    return null;
  }

  /**
   * Wait until the upload is over.
   *
   * The input is looked up again on every tick: an app that re-renders its
   * composer replaces the input, and the old node would look "empty" for
   * ever.
   *
   * Bounded, and never waits for the DOM to be quiet: a page that updates a
   * counter never becomes quiet, and waiting for it cost 90 s for nothing.
   *
   * Returns:
   *  - "done"    : the input still holds the files and no spinner is left —
   *                the page took them.
   *  - "dropped" : the input is empty or gone: the composer was re-rendered.
   *                The file may well have been taken (the app replaces the
   *                input once it owns the file) — the PREVIEW is then the
   *                only witness left.
   *  - "timeout" : the files are still there but a spinner never stopped.
   */
  async function waitUploadDone(files, { inputs = [], timeout = 25000, pollMs = 250 } = {}) {
    const wanted = (files || []).length;
    const t0 = Date.now();
    while (Date.now() - t0 < timeout) {
      if (global.__NEMAPI_STOP__) throw new Error("Stopped");
      const input = findFileInput(inputs);
      let kept = 0;
      try {
        kept = input && input.files ? input.files.length : 0;
      } catch (_) {}
      if (kept >= wanted && !uploadSpinner(input)) return "done";
      if (!kept) return "dropped";
      await sleep(pollMs);
    }
    return "timeout";
  }

  /**
   * Wait until the page visibly accepted the attachment: a blob: preview
   * appears (DeepSeek, Qwen). Providers without a preview return false after
   * the timeout so the caller can decide.
   *
   * `baseline` = number of previews before the upload. A NEW preview is proof
   * that the page took the file, even when the thumbnail carries no usable
   * alt attribute (many builds set none).
   */
  async function waitForAttachments({ timeout = 30000, names = [], min = 1, baseline = null } = {}) {
    const t0 = Date.now();
    const wanted = (names || []).filter(Boolean);
    while (Date.now() - t0 < timeout) {
      if (global.__NEMAPI_STOP__) throw new Error("Stopped");
      let seen = 0;
      let total = 0;
      try {
        const imgs = document.querySelectorAll('img[src^="blob:"], img[src^="data:image"]');
        total = imgs.length;
        for (const img of imgs) {
          const alt = img.getAttribute("alt") || "";
          if (!wanted.length || wanted.some((n) => alt === n || alt.indexOf(n) >= 0)) seen += 1;
        }
      } catch (_) {}
      const grew = baseline !== null && total > baseline;
      if (seen >= min || grew) {
        await sleep(600);
        return true;
      }
      await sleep(300);
    }
    return false;
  }

  /**
   * Signature of the attachment area: it changes while an upload runs
   * (preview appears, spinner, preview replaced by the CDN url).
   *
   * `scope` restricts the spinner search to the composer: a page-wide
   * `[class*='loading']` match finds unrelated elements (skeleton, lazy
   * image) and keeps the area "busy" for ever.
   */
  function attachmentSignature(scope) {
    let s = "";
    try {
      const imgs = document.querySelectorAll("img[src^='blob:'], img[src^='data:image']");
      s = "img" + imgs.length;
      for (const i of imgs) s += ":" + (i.getAttribute("src") || "").slice(-20);
    } catch (_) {}
    let spin = 0;
    try {
      const area = scope || document;
      const els = area.querySelectorAll(
        "[class*='animated-size-item'] [class*='loading'], " +
          "[class*='animated-size-item'] .ds-loading, " +
          "[class*='upload'] [class*='spinner'], [class*='spinner']"
      );
      for (const el of els) if (isVisible(el)) spin += 1;
    } catch (_) {}
    return s + "|spin" + spin;
  }

  /**
   * Wait until the upload is OVER.
   *
   * An app that re-renders its composer when the transfer ENDS wipes the
   * prompt typed during the upload — the image stays, the text disappears —
   * and the send control stays disabled: the request then hangs until the
   * watchdog. The prompt must therefore WAIT for the upload.
   *
   * Two questions, two witnesses:
   *  - did the app TAKE the file? A preview appeared in the composer area
   *    (an image, or the file name on the attachment chip), or the app
   *    emptied the file input (it consumed the file);
   *  - is the transfer OVER? No spinner left, and the attachment area no
   *    longer changing.
   *
   * Bounded, and it never waits for a QUIET DOM: a page that updates a
   * counter never becomes quiet, and waiting for it cost 90 s for nothing.
   * A site that shows no preview and no spinner is given `quietMs` and then
   * let go — the write/retry loop remains the safety net.
   *
   * Returns true when the app took the file (a preview, the file name, or
   * an emptied input is the proof) — the wait ends as soon as the area
   * settles, or when the patience runs out on a site that never stops
   * moving. It returns false only when NOTHING proves the app took the
   * file: the caller must then fail loudly rather than send a prompt whose
   * image is missing.
   */
  async function waitUploadFinished(files, {
    inputs = [],
    scope = null,
    probe = null,
    timeout = 30000,
    quietMs = 8000,
    stableMs = 700,
    pollMs = 250,
    baseline = null,
  } = {}) {
    const names = (files || []).map((f) => f.name).filter(Boolean);
    const base = baseline === null ? previewCount() : baseline;
    const t0 = Date.now();
    let last = null;
    let stableSince = Date.now();
    let taken = false;
    while (Date.now() - t0 < timeout) {
      if (global.__NEMAPI_STOP__) throw new Error("Stopped");
      const input = findFileInput(inputs);
      const spinner = uploadSpinner(input, scope);
      const sig = attachmentSignature(scope);
      if (sig !== last) {
        last = sig;
        stableSince = Date.now();
      }
      // did the app take the file?
      if (!taken) {
        if (previewCount() > base) taken = true;
        else if (names.length && nameVisible(names, scope)) taken = true;
        else if (probe) {
          try {
            if (probe()) taken = true;
          } catch (_) {}
        } else if (input) {
          let kept = 0;
          try {
            kept = input.files ? input.files.length : 0;
          } catch (_) {}
          // the app consumed the file: it emptied the input
          if (!kept) taken = true;
        }
      }
      const settled = !spinner && Date.now() - stableSince >= stableMs;
      if (settled) {
        if (taken) return true;
        // nothing observable on this site: stop waiting, the retry loop
        // will tell us if the composer really took the text
        if (Date.now() - t0 >= quietMs) return false;
      }
      await sleep(pollMs);
    }
    // Out of patience. The app DID take the file (a preview is proof) but
    // something in the composer never stops moving: a permanent spinner
    // would otherwise turn a working site into a failure. The write/retry
    // loop remains the safety net.
    return taken;
  }

  /** Is the file name visible in the attachment area? (the chip of a chip) */
  function nameVisible(names, scope) {
    if (!names || !names.length) return false;
    const area = scope || document.body;
    let text = "";
    try {
      text = area.textContent || "";
    } catch (_) {
      return false;
    }
    for (const n of names) {
      if (n && text.indexOf(n) >= 0) return true;
    }
    return false;
  }

  /**
   * Wait until the upload is over: the attachment area must stop changing and
   * no spinner may be left.
   *
   * The preview appears at once but the transfer to the provider takes
   * seconds, and the page re-renders when it ends. A prompt typed in between
   * is wiped by React (the image stays, the text vanishes) — so the text is
   * only written once the composer has settled.
   *
   * @deprecated use waitUploadFinished(): it also PROVES that the app took
   * the file instead of assuming a quiet area.
   */
  async function waitForUploadSettled({ timeout = 20000, stableMs = 1800, pollMs = 300 } = {}) {
    const t0 = Date.now();
    let last = null;
    let stableSince = Date.now();
    while (Date.now() - t0 < timeout) {
      if (global.__NEMAPI_STOP__) throw new Error("Stopped");
      const cur = attachmentSignature();
      if (cur !== last) {
        last = cur;
        stableSince = Date.now();
      } else if (Date.now() - stableSince >= stableMs && cur.indexOf("|spin0") >= 0) {
        return true; // nothing moves any more and no spinner is left
      }
      await sleep(pollMs);
    }
    return false;
  }

  /**
   * Cascade: React fiber → clipboard copy → htmlToMarkdown → plain text.
   * @param {Element} messageEl
   * @param {{ markdownRoots?: string[], copyRoot?: Element, preferPremium?: boolean }} opts
   */
  async function extractAssistantMarkdown(messageEl, opts) {
    opts = opts || {};
    const preferPremium = opts.preferPremium !== false;
    if (!messageEl) return "";

    if (preferPremium) {
      // 1) React source markdown (DeepSeek stores props.markdown on ds-markdown)
      const mdRoots = [];
      if (opts.markdownRoots) {
        for (const s of opts.markdownRoots) {
          try {
            mdRoots.push(...messageEl.querySelectorAll(s));
          } catch (_) {}
        }
      }
      if (!mdRoots.length) mdRoots.push(messageEl);
      for (const node of mdRoots) {
        const fromReact = extractReactMarkdown(node);
        if (fromReact && fromReact.length > 20) return fromReact;
      }

      // 2) Clipboard via synthetic Copy click (works when site doesn't require isTrusted)
      const copyRoot = opts.copyRoot || messageEl.parentElement || messageEl;
      const fromClip = await tryClipboardFromCopyButton(copyRoot, opts.copySelectors);
      if (fromClip && fromClip.length > 10) return fromClip;
    }

    // 3) Structured HTML → Markdown
    const htmlRoots = opts.markdownRoots
      ? opts.markdownRoots.flatMap((s) => {
          try {
            return Array.from(messageEl.querySelectorAll(s));
          } catch (_) {
            return [];
          }
        })
      : [messageEl];
    for (const node of htmlRoots.length ? htmlRoots : [messageEl]) {
      const md = htmlToMarkdown(node);
      if (md) return md;
    }

    // 4) Plain text last resort
    return (messageEl.innerText || messageEl.textContent || "").trim();
  }

  function premiumEnabled() {
    return !!(global.__NEMAPI_PREMIUM_MD__);
  }

  // Install page clipboard hook as early as possible (content-script load)
  try {
    if (typeof document !== "undefined") {
      if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", () => ensurePageClipboardHook(), { once: true });
      } else {
        ensurePageClipboardHook();
      }
    }
  } catch (_) {}

  global.NemApiBase = {
    sleep,
    timerMode: TimerHub.mode,
    keepTabAwake,
    waitFor,
    queryFirst,
    queryAll,
    isVisible,
    isDisabled,
    textOf,
    findByText,
    findComposerRoot,
    setTextareaValue,
    setContentEditable,
    setQuill,
    setNativeValue,
    pasteText,
    readReactValue,
    textMatches,
    writeAndConfirm,
    isContentEditable,
    editorText,
    isQuillEditor,
    findQuill,
    setQuillText,
    bridgeSetQuill,
    insertTextNative,
    placeCaretEnd,
    clearNativeValue,
    notifyInput,
    clickEl,
    sendWithProof,
    clickSend,
    inputCandidatesFrom,
    waitEditable,
    pressEnter,
    waitUntilStable,
    waitForNewResponse,
    waitForEnabledSend,
    textFingerprint,
    activitySignature,
    getReactFiber,
    navigateFiberPath,
    extractReactMarkdown,
    readMarkdownViaPage,
    restoreMath,
    noteExtractSource,
    resetExtractSource,
    looksIncomplete,
    jobBudgetMs,
    getExtractSource,
    bridgeReady,
    fenceFor,
    linkToMarkdown,
    htmlToMarkdown,
    stripLeadingLineNumbers,
    cleanMarkdownCodeFences,
    tryClipboardFromCopyButton,
    ensurePageClipboardHook,
    extractAssistantMarkdown,
    base64ToBytes,
    fileFromBase64,
    attachFiles,
    attachFilesToPage,
    findFileInput,
    firstVisible,
    waitUploadDone,
    waitUploadFinished,
    waitForAttachments,
    attachmentSignature,
    waitForUploadSettled,
    premiumEnabled,
    isCodeBlockCopyButton,
    isUserMessageContext,
    isAssistantMessageContext,
    scoreMessageCopyButton,
    composerMatches,
    composerReport,
    discoverEditable,
    discoverSend,
    normalizeComposer,
    compactComposer,
  };
})(typeof window !== "undefined" ? window : self);
