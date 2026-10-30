/**
 * NemApi – content script router.
 *
 * Answers are detected HERE, in the provider page: the background only hands
 * out jobs and reports results. Three rules keep a request fast, and each one
 * exists because its absence cost seconds (or minutes) per request.
 *
 * 1. NO RAW TIMERS. Every wait goes through `B.sleep`, which delegates to a
 *    dedicated Worker (`providers/ticker.js`) when the tab is hidden. Chrome
 *    throttles a hidden page's chained timers to ONE wake per SECOND, then to
 *    one per MINUTE after five minutes hidden ("intensive wake-up
 *    throttling"): an idle check asking for 1.5 s of quiet woke up a minute
 *    later, so a whole request took two to five minutes — and it finished
 *    instantly as soon as the user brought the tab back to the front. That is
 *    the random "it only works when I look at the tab" bug.
 *
 * 2. NO EXPENSIVE POLLING. The loops read the CHEAP text of the last answer
 *    (a `textContent` read, `readCheapText`) and rebuild the markdown ONCE,
 *    when the answer is accepted. Polling the markdown rebuild every 400 ms
 *    kept the page's main thread busy for the whole answer, and the cost grew
 *    with the length of the answer — which is what made a provider tab lag
 *    until it was closed and reopened.
 *
 * 3. NOTHING MAY BLOCK THE TAB FOR EVER. A job has a heartbeat and a local
 *    watchdog: a page that never streams releases the tab and fails the job
 *    instead of leaving `__NEMAPI_RUNNING_JOB__` set (every later request was
 *    then rejected with "Tab is already running another job" until a reload).
 *
 * The tab also holds a Web Lock for its whole life (`B.keepTabAwake`): a page
 * that Chrome froze (Energy Saver, hidden and silent for five minutes) runs
 * NOTHING — no timer, no observer, no message handler — and the request only
 * completed when the user clicked the tab.
 */
(function () {
  "use strict";
  if (window.__NEMAPI_CS__) return;
  window.__NEMAPI_CS__ = true;
  window.__NEMAPI_RUNNING_JOB__ = null;
  window.__NEMAPI_FINISHED_JOBS__ = window.__NEMAPI_FINISHED_JOBS__ || new Set();

  // How often a running job tells the background it is still alive. The
  // background fails the job when the tab goes silent (frozen, crashed,
  // reloaded): without it the provider slot stayed taken for the whole job
  // timeout and the extension rejected every later job for that provider.
  const HEARTBEAT_MS = 15000;
  // Pace of the "is the page still working?" loops.
  const IDLE_POLL_MS = 400;
  // How long the answer may take to START before the send is declared lost.
  const START_TIMEOUT_MS = 20000;

  function base() {
    return window.NemApiBase || {};
  }

  function sleep(ms) {
    const B = base();
    return B.sleep ? B.sleep(ms) : new Promise((r) => setTimeout(r, ms));
  }

  function resolveProvider() {
    const url = location.href;
    const map = window.NemApiProviders || {};
    for (const key of Object.keys(map)) {
      if (map[key].match && map[key].match(url)) return map[key];
    }
    return null;
  }

  function detectProviderId() {
    const provider = resolveProvider();
    return provider ? provider.id : null;
  }

  /** Cheap "is the page changing?" signature (never a full extraction). */
  function activity(provider) {
    try {
      const B = base();
      if (B.activitySignature) return String(B.activitySignature(provider) || "");
    } catch (_) {}
    try {
      return provider && provider.getResponseCount ? "n" + provider.getResponseCount() : "";
    } catch (_) {
      return "";
    }
  }

  function isGenerating(provider) {
    try {
      return !!(provider && provider.isGenerating && provider.isGenerating());
    } catch (_) {
      return false;
    }
  }

  /**
   * Wait until the provider is not generating anymore (leftover of the
   * previous job). Typing while a previous answer still streams cuts it off,
   * so this cannot be skipped — but it must not cost seconds when the page is
   * simply idle, which is the common case.
   *
   * Fast path: one cheap probe, 250 ms of confirmation. The old code slept
   * 800 ms twice and demanded a 1.5 s quiet window, i.e. 2.4 s on EVERY
   * request, idle page or not.
   */
  async function waitForIdle(provider, timeoutMs) {
    const t0 = Date.now();
    if (!isGenerating(provider)) {
      const first = activity(provider);
      await sleep(250);
      if (!isGenerating(provider) && activity(provider) === first) return true;
    }
    let last = activity(provider);
    let quietSince = Date.now();
    while (Date.now() - t0 < timeoutMs) {
      await sleep(IDLE_POLL_MS);
      const busy = isGenerating(provider);
      const cur = activity(provider);
      if (cur !== last) {
        last = cur;
        quietSince = Date.now();
        continue;
      }
      const quiet = Date.now() - quietSince;
      if (!busy && quiet >= 900) return true;
      // A stuck "Stop" button must not park the provider for minutes: the send
      // itself proves whether the page takes the prompt.
      if (busy && quiet >= 20000) return true;
    }
    return false;
  }

  /**
   * Nothing to clear before a job.
   *
   * The editor is emptied by `B.pasteText` itself, which starts with a native
   * `""` write. Touching the controlled textarea here (focus, input events,
   * empty writes) leaves React in a state where the NEXT write is ignored —
   * after an image upload the composer re-renders and the prompt never
   * appears, so the send control never enables.
   */
  function clearEditor(provider) {
    void provider;
  }

  /**
   * Did the answer START?
   *
   * Three independent signals: the bubble count grew, the provider says it is
   * generating, or the cheap activity signature moved.
   *
   * `prevSig` is the SAME measure captured before the send. Comparing a
   * signature with the markdown of the previous answer (what the code used to
   * do) was always "different", so a send the page had silently swallowed was
   * never detected and the request waited for its whole budget.
   *
   * Failing fast matters: a swallowed send (stale tab, silent rate limit)
   * used to burn the full 900 s timeout.
   */
  async function waitForGenerationStart(provider, prevSig, prevCount, timeoutMs) {
    const t0 = Date.now();
    while (Date.now() - t0 < timeoutMs) {
      try {
        if (prevCount && provider.getResponseCount && provider.getResponseCount() > prevCount) {
          return true;
        }
        if (isGenerating(provider)) return true;
        const cur = activity(provider);
        if (cur && prevSig && cur !== prevSig) return true;
      } catch (_) {}
      await sleep(250);
    }
    return false;
  }

  function rememberFinished(jobId) {
    const set = window.__NEMAPI_FINISHED_JOBS__;
    set.add(jobId);
    if (set.size > 40) {
      // Cap memory: drop the oldest entry
      const first = set.values().next().value;
      set.delete(first);
    }
  }

  /**
   * OpenAI `image_url` parts → `File` objects for the page's upload input.
   *
   * Data URLs only: the extension must never fetch a remote URL with the
   * cookies of the provider tab (SSRF).
   */
  function filesFromImages(images) {
    const B = base();
    const out = [];
    (images || []).forEach((img, i) => {
      try {
        const url = typeof img === "string" ? img : (img && img.url) || "";
        const m = /^data:([^;,]+)?(;base64)?,([\s\S]*)$/.exec(url || "");
        if (!m || !m[2]) throw new Error("image_url must be a base64 data: URL");
        const mime = m[1] || "image/png";
        const ext = (mime.split("/")[1] || "png").replace(/[^a-z0-9]/gi, "") || "png";
        const name = (img && img.name) || `image-${i + 1}.${ext}`;
        out.push(B.fileFromBase64(m[3], name, mime));
      } catch (e) {
        throw new Error(`Image #${i + 1}: ${e.message || e}`);
      }
    });
    return out;
  }

  /** Tell the background, on a timer, that this tab is still working. */
  function startHeartbeat(jobId) {
    let stopped = false;
    const loop = async () => {
      while (!stopped) {
        await sleep(HEARTBEAT_MS);
        if (stopped) return;
        try {
          await chrome.runtime.sendMessage({ action: "jobProgress", jobId: jobId });
        } catch (_) {
          return; // the extension was reloaded: the background knows the tab is gone
        }
      }
    };
    loop();
    return () => {
      stopped = true;
    };
  }

  async function runJob(jobId, question, expectedProvider, model, premiumMd, jobTimeoutMs, images) {
    if (window.__NEMAPI_FINISHED_JOBS__.has(jobId)) {
      console.log("[NemApi] Ignoring already-finished job", jobId.slice(0, 8));
      return;
    }
    if (window.__NEMAPI_RUNNING_JOB__ && window.__NEMAPI_RUNNING_JOB__ !== jobId) {
      chrome.runtime.sendMessage({
        action: "automationError",
        jobId,
        error: "Tab is already running another job",
      });
      return;
    }
    if (window.__NEMAPI_RUNNING_JOB__ === jobId) return;

    window.__NEMAPI_STOP__ = false;
    window.__NEMAPI_PREMIUM_MD__ = premiumMd !== false;
    // answer budget = the proxy timeout minus a margin to post the result
    window.__NEMAPI_JOB_BUDGET_MS__ = Math.max(120000, (Number(jobTimeoutMs) || 900000) - 30000);
    const provider = resolveProvider();
    if (!provider) {
      chrome.runtime.sendMessage({
        action: "automationError",
        jobId,
        error: "No provider matched this page: " + location.hostname,
      });
      return;
    }
    if (expectedProvider && provider.id !== expectedProvider) {
      chrome.runtime.sendMessage({
        action: "automationError",
        jobId,
        error: `Provider mismatch: requested ${expectedProvider}, current page is ${provider.id}`,
      });
      return;
    }

    window.__NEMAPI_RUNNING_JOB__ = jobId;
    const B = base();
    const stopHeartbeat = startHeartbeat(jobId);
    let typed = false; // the prompt reached the web UI
    let settled = false; // no other outcome may be reported
    let reported = false; // the job already has a result or an error

    // Local watchdog: whatever happens in this page, the tab must not stay
    // "busy" for ever. It used to: a loop that never settled kept
    // __NEMAPI_RUNNING_JOB__ set, and every later request was rejected with
    // "Tab is already running another job" until the tab was reloaded.
    const watchdog = (async () => {
      await sleep((window.__NEMAPI_JOB_BUDGET_MS__ || 900000) + 30000);
      if (settled) return;
      settled = true;
      reported = true;
      window.__NEMAPI_RUNNING_JOB__ = null;
      window.__NEMAPI_STOP__ = true;
      try {
        await chrome.runtime.sendMessage({
          action: "automationError",
          jobId: jobId,
          provider: provider.id,
          typed: typed,
          error: "the tab gave up on this job (local watchdog)",
        });
      } catch (_) {}
    })();

    try {
      console.log("[NemApi] Running job via", provider.id, model || "");
      if (provider.findInput && !provider.findInput()) await sleep(300);
      window.__NEMAPI_IS_BUSY__ = () => !!(provider.isGenerating && provider.isGenerating());
      // bounded: a page that keeps "changing" (a spinner, an animation) must
      // not eat the beginning of the job budget
      await waitForIdle(provider, 25000);

      // Read the previous answer AFTER the idle wait: an answer still
      // streaming would otherwise be taken for the new one once it ends.
      const previous = (provider.getLastResponse && provider.getLastResponse()) || "";
      clearEditor(provider);
      // Snapshot BEFORE sending: a new bubble is the reliable "fresh answer"
      // signal even when the new text is identical to the previous one.
      const prevCount = provider.getResponseCount ? provider.getResponseCount() : 0;
      const prevSig = activity(provider);

      if (images && images.length) {
        if (typeof provider.attachFiles !== "function") {
          throw new Error(
            `Provider ${provider.id}: this site has no image upload in NemApi yet ` +
              "(the file input was not found on the page)"
          );
        }
        const files = filesFromImages(images);
        await provider.attachFiles(files);
        console.log("[NemApi] attached", files.length, "image(s) on", provider.id);
      }

      await provider.sendPrompt(question);
      typed = true;
      if (window.__NEMAPI_STOP__) throw new Error("Job stopped");

      // A send the page swallowed (stale tab, rate-limit banner) must fail at
      // once instead of polling the old answer until the watchdog.
      const started = await waitForGenerationStart(provider, prevSig, prevCount, START_TIMEOUT_MS);
      if (!started) throw new Error("Provider did not start responding (stale tab or rate limit)");
      if (window.__NEMAPI_STOP__) throw new Error("Job stopped");

      if (B.resetExtractSource) B.resetExtractSource();
      const text = await provider.waitForResponse(previous, prevCount);
      if (window.__NEMAPI_STOP__) throw new Error("Job stopped");

      settled = true;
      reported = true;
      await chrome.runtime.sendMessage({
        action: "automationResult",
        jobId: jobId,
        result: text || "",
        provider: provider.id,
        source: B.getExtractSource ? B.getExtractSource() : null,
        bridge: B.bridgeReady ? B.bridgeReady() : null,
        timer: B.timerMode ? B.timerMode() : null,
        partial: !!window.__NEMAPI_LAST_PARTIAL__,
      });
    } catch (e) {
      settled = true;
      if (reported) return; // the watchdog already answered for this job
      reported = true;
      try {
        await chrome.runtime.sendMessage({
          action: "automationError",
          jobId: jobId,
          provider: provider.id,
          typed: typed,
          error: (e && e.message) || String(e),
        });
      } catch (_) {}
    } finally {
      settled = true;
      stopHeartbeat();
      window.__NEMAPI_RUNNING_JOB__ = null;
      rememberFinished(jobId);
      window.__NEMAPI_STOP__ = false;
      void watchdog;
    }
  }

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg.action === "runAutomation") {
      runJob(
        msg.jobId,
        msg.question,
        msg.provider,
        msg.model,
        msg.premiumMd !== false,
        msg.jobTimeoutMs,
        msg.images || null
      );
      sendResponse({ ok: true, provider: detectProviderId() });
      return true;
    }
    if (msg.action === "stopAutomation") {
      window.__NEMAPI_STOP__ = true;
      sendResponse({ ok: true });
      return true;
    }
    if (msg.action === "ping") {
      const provider = resolveProvider();
      let hasInput = false;
      try {
        hasInput = !!(provider && provider.findInput && provider.findInput());
      } catch (_) {}
      const B = base();
      sendResponse({
        pong: true,
        provider: provider ? provider.id : null,
        hasInput: hasInput,
        running: !!window.__NEMAPI_RUNNING_JOB__,
        url: location.href,
        timer: B.timerMode ? B.timerMode() : null,
        keepAwake: (B.pageState && B.pageState().keepAwake) || null,
      });
      return true;
    }
    if (msg.action === "probe") {
      const provider = resolveProvider();
      sendResponse({
        provider: provider ? provider.id : null,
        inputFound: !!(provider && provider.findInput && provider.findInput()),
        sample: provider && provider.getLastResponse ? (provider.getLastResponse() || "").slice(0, 200) : "",
        timer: base().timerMode ? base().timerMode() : null,
      });
      return true;
    }
    return false;
  });

  // Hold a Web Lock: Chrome documents it as an opt-out of tab freezing
  // ("Freezing on Energy Saver" ships exactly this exemption). A frozen tab
  // runs nothing at all, and it is unfrozen only by activating it — the
  // symptom is a request that hangs until the user looks at the tab.
  try {
    const B = base();
    if (B.keepTabAwake) B.keepTabAwake();
  } catch (_) {}

  // After a freeze the suspended loops resume where they stopped: the answer
  // is usually already complete, and the first tick of the wait loop takes it.
  document.addEventListener("resume", () => {
    try {
      chrome.runtime.sendMessage({ action: "extensionLog", message: "tab resumed after a freeze", level: "warn" }).catch(() => {});
    } catch (_) {}
  });

  // Report the timer source once per page: "timer" or "message" instead of
  // "worker" means this page refused the Worker (strict CSP) and the waits are
  // back to Chrome's throttled timers — worth a warning in the admin log.
  (async () => {
    await sleep(2000);
    const B = base();
    if (!B.timerMode || !B.pageState) return;
    const state = B.pageState();
    try {
      await chrome.runtime.sendMessage({
        action: "extensionLog",
        level: state.timer === "worker" ? "info" : "warn",
        message: `Tab ready on ${detectProviderId() || "?"}: timers=${state.timer} keepAwake=${state.keepAwake}`,
      });
    } catch (_) {}
  })();

  try {
    chrome.runtime.sendMessage({ action: "contentReady" }).catch(() => {});
  } catch (_) {}
})();
