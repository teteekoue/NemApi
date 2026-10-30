/**
 * NemApi v4.0 – content script router
 * Accepts jobs, waits for page readiness is handled by background.
 * One job at a time per tab; finished job ids are tracked briefly to ignore duplicates.
 */
(function () {
  "use strict";
  if (window.__NEMAPI_CS__) return;
  window.__NEMAPI_CS__ = true;
  window.__NEMAPI_RUNNING_JOB__ = null;
  window.__NEMAPI_FINISHED_JOBS__ = window.__NEMAPI_FINISHED_JOBS__ || new Set();

  /**
   * Wait `ms` through base.js.
   *
   * v5.4.1: the waits of this router used a raw `setTimeout`, which Chrome
   * throttles in a hidden tab (1 s, then one wake per MINUTE). `B.sleep` picks
   * a timer that is not throttled (dedicated Worker, else a MessageChannel
   * bounce) — nothing else changes: the loops and the detection are the ones
   * of v5.3.1.
   */
  function sleep(ms) {
    const B = window.NemApiBase || {};
    if (B.sleep) return B.sleep(ms);
    return new Promise((r) => setTimeout(r, ms));
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
    const p = resolveProvider();
    return p ? p.id : null;
  }

  /**
   * Wait until the provider is not generating anymore (previous job leftover).
   * "Generating" = the page says so OR the last answer is still growing: the
   * Stop-button detection is not reliable on every site, and typing (or
   * clicking the send/stop button) while an answer streams would cut it off.
   *
   * The busy flag is read AFTER the sleep (it used to be one iteration stale)
   * and the wait is bounded: a stuck "Stop" indicator must not park the
   * provider for a minute.
   */
  async function waitForIdle(provider, timeoutMs) {
    const t0 = Date.now();
    const B = window.NemApiBase || {};
    const read = () => {
      try {
        if (B.activitySignature) return B.activitySignature(provider);
        return (provider.getLastResponse && provider.getLastResponse()) || "";
      } catch (_) {
        return "";
      }
    };
    let last = read();
    let quietSince = Date.now();
    while (Date.now() - t0 < timeoutMs) {
      await sleep(800);
      let busy = false;
      try { busy = !!(provider.isGenerating && provider.isGenerating()); } catch (_) {}
      const cur = read();
      if (cur !== last) {
        last = cur;
        quietSince = Date.now();
        continue;
      }
      const quiet = Date.now() - quietSince;
      if (!busy && quiet >= 1500) return true;
      // a stuck "generating" indicator must not block the slot for minutes
      if (busy && quiet >= 20000) return true;
    }
    return false; // continue anyway; sendPrompt will handle it
  }

  /** Clear any stale text left in the editor from a previous aborted job.
   *
   * Must go through the native value setter (B.pasteText): assigning
   * `input.value = ""` directly desynchronises React's controlled textarea,
   * after which every later native write is silently ignored — the prompt
   * never appears in the composer and the send button never enables.
   */
  function clearEditor(provider) {
    // Deliberately minimal: the editor is cleared by pasteText() itself, which
    // starts with setVal("") before writing. Touching the controlled textarea
    // here (focus, input events, empty writes) leaves React in a state where
    // the NEXT write is ignored — after an upload the composer re-renders and
    // the prompt never appears. Nothing to do before a job: the paste clears.
    void provider;
  }

  /**
   * Detect "started": a NEW model-response bubble appeared (even with text
   * identical to the previous answer), or the provider flipped to generating.
   */
  async function waitForGenerationStart(provider, previous, prevCount, timeoutMs) {
    const t0 = Date.now();
    const prev = (previous || "").trim();
    const B = window.NemApiBase || {};
    while (Date.now() - t0 < timeoutMs) {
      try {
        if (prevCount && provider.getResponseCount && provider.getResponseCount() > prevCount) return true;
        if (provider.isGenerating && provider.isGenerating()) return true;
        // cheap signal: the full extraction rebuilds the markdown and is far
        // too heavy to run every 400 ms
        let cur = "";
        if (provider.getActivitySignature) {
          cur = String(provider.getActivitySignature() || "");
        } else {
          cur = (provider.getLastResponse && provider.getLastResponse() || "").trim();
        }
        if (cur && cur !== prev) return true;
      } catch (_) {}
      await sleep(400);
    }
    return false;
  }

  function rememberFinished(jobId) {
    const set = window.__NEMAPI_FINISHED_JOBS__;
    set.add(jobId);
    // Cap memory: drop oldest when too many
    if (set.size > 40) {
      const first = set.values().next().value;
      set.delete(first);
    }
  }

  /**
   * Images sent by the API as OpenAI image_url parts → File objects for the
   * page's upload input. Data URLs only: the proxy never lets the extension
   * fetch a remote URL (SSRF, cookies of the provider tab).
   */
  function filesFromImages(images) {
    const B = window.NemApiBase || {};
    const out = [];
    (images || []).forEach((img, i) => {
      try {
        const url = typeof img === "string" ? img : (img && img.url) || "";
        const m = /^data:([^;,]+)?(;base64)?,([\s\S]*)$/.exec(url || "");
        if (!m || !m[2]) {
          throw new Error("image_url must be a base64 data: URL");
        }
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
    if (window.__NEMAPI_RUNNING_JOB__ === jobId) {
      return;
    }

    window.__NEMAPI_STOP__ = false;
    window.__NEMAPI_PREMIUM_MD__ = premiumMd !== false;
    // answer budget = proxy timeout minus a margin to post the result in time
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
    let typed = false; // true once the prompt was submitted to the web UI
    try {
      console.log("[NemApi] Running job via", provider.id, model || "");
      // Ensure input exists before pasting (background should have waited, but belt-and-suspenders)
      if (provider.findInput && !provider.findInput()) {
        await sleep(800);
      }
      window.__NEMAPI_IS_BUSY__ = () => !!(provider.isGenerating && provider.isGenerating());
      // Native-context hardening: if the previous job was left mid-generation
      // (watchdog abort, navigation, retry), the provider may still be busy and
      // the editor may hold unsent text.  Wait for idle, then clear the editor.
      // bounded by the job budget: an idle wait must never eat the whole
      // timeout the proxy granted for this job
      await waitForIdle(provider, Math.min(120000, window.__NEMAPI_JOB_BUDGET_MS__ || 120000));
      // read AFTER the idle wait: a previous answer still streaming would
      // otherwise be taken for the new one once it finishes
      const previous = (provider.getLastResponse && provider.getLastResponse()) || "";
      clearEditor(provider);
      // Snapshot the bubble count BEFORE sending — a new response bubble is the
      // reliable "fresh answer" signal even when the text is identical.
      const prevCount = provider.getResponseCount ? provider.getResponseCount() : 0;
      // Multimodal request: the images go through the page's own upload input
      // before the prompt is typed, exactly like a user attaching a screenshot.
      if (images && images.length) {
        if (typeof provider.attachFiles !== "function") {
          throw new Error(
            `Provider ${provider.id}: this site has no image upload in NemApi yet ` +
            `(the file input was not found on the page)`
          );
        }
        const files = filesFromImages(images);
        await provider.attachFiles(files);
        console.log("[NemApi] attached", files.length, "image(s) on", provider.id);
      }
      await provider.sendPrompt(question);
      typed = true;
      if (window.__NEMAPI_STOP__) throw new Error("Job stopped");
      // If the model never STARTS generating (editor send swallowed, silent
      // rate-limit banner, stale tab), fail fast instead of polling old text
      // until the watchdog kicks in.
      const started = await waitForGenerationStart(provider, previous, prevCount, 25000);
      if (!started) throw new Error("Provider did not start responding (stale tab or rate limit)");
      if (window.__NEMAPI_STOP__) throw new Error("Job stopped");
      const B = window.NemApiBase || {};
      if (B.resetExtractSource) B.resetExtractSource();
      const text = await provider.waitForResponse(previous, prevCount);
      if (window.__NEMAPI_STOP__) throw new Error("Job stopped");
      await chrome.runtime.sendMessage({
        action: "automationResult",
        jobId,
        result: text || "",
        provider: provider.id,
        source: B.getExtractSource ? B.getExtractSource() : null,
        partial: !!window.__NEMAPI_LAST_PARTIAL__,
        bridge: B.bridgeReady ? B.bridgeReady() : null,
      });
    } catch (e) {
      try {
        await chrome.runtime.sendMessage({
          action: "automationError",
          jobId,
          provider: provider.id,
          typed,
          error: e.message || String(e),
        });
      } catch (_) {}
    } finally {
      window.__NEMAPI_RUNNING_JOB__ = null;
      rememberFinished(jobId);
      window.__NEMAPI_STOP__ = false;
    }
  }

  chrome.runtime.onMessage.addListener((msg, _s, sendResponse) => {
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
      const p = resolveProvider();
      let hasInput = false;
      try {
        hasInput = !!(p && p.findInput && p.findInput());
      } catch (_) {}
      sendResponse({
        pong: true,
        provider: p ? p.id : null,
        hasInput,
        running: !!window.__NEMAPI_RUNNING_JOB__,
        url: location.href,
      });
      return true;
    }
    if (msg.action === "probe") {
      const p = resolveProvider();
      sendResponse({
        provider: p ? p.id : null,
        inputFound: !!(p && p.findInput && p.findInput()),
        sample: p && p.getLastResponse ? (p.getLastResponse() || "").slice(0, 200) : "",
      });
      return true;
    }
    return false;
  });

  try {
    chrome.runtime.sendMessage({ action: "contentReady" }).catch(() => {});
  } catch (_) {}
})();
