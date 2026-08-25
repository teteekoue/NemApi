/**
 * NemApi v3.0 – content script router
 * Accepts jobs, waits for page readiness is handled by background.
 * One job at a time per tab; finished job ids are tracked briefly to ignore duplicates.
 */
(function () {
  "use strict";
  if (window.__NEMAPI_CS__) return;
  window.__NEMAPI_CS__ = true;
  window.__NEMAPI_RUNNING_JOB__ = null;
  window.__NEMAPI_FINISHED_JOBS__ = window.__NEMAPI_FINISHED_JOBS__ || new Set();

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

  function rememberFinished(jobId) {
    const set = window.__NEMAPI_FINISHED_JOBS__;
    set.add(jobId);
    // Cap memory: drop oldest when too many
    if (set.size > 40) {
      const first = set.values().next().value;
      set.delete(first);
    }
  }

  async function runJob(jobId, question, expectedProvider, model, premiumMd) {
    if (window.__NEMAPI_FINISHED_JOBS__.has(jobId)) {
      console.log("[NemApi] Ignoring already-finished job", jobId.slice(0, 8));
      return;
    }
    if (window.__NEMAPI_RUNNING_JOB__ && window.__NEMAPI_RUNNING_JOB__ !== jobId) {
      browser.runtime.sendMessage({
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
    const provider = resolveProvider();
    if (!provider) {
      browser.runtime.sendMessage({
        action: "automationError",
        jobId,
        error: "No provider matched this page: " + location.hostname,
      });
      return;
    }
    if (expectedProvider && provider.id !== expectedProvider) {
      browser.runtime.sendMessage({
        action: "automationError",
        jobId,
        error: `Provider mismatch: requested ${expectedProvider}, current page is ${provider.id}`,
      });
      return;
    }

    window.__NEMAPI_RUNNING_JOB__ = jobId;
    try {
      console.log("[NemApi] Running job via", provider.id, model || "");
      // Ensure input exists before pasting (background should have waited, but belt-and-suspenders)
      if (provider.findInput && !provider.findInput()) {
        await new Promise((r) => setTimeout(r, 800));
      }
      const previous = (provider.getLastResponse && provider.getLastResponse()) || "";
      await provider.sendPrompt(question);
      if (window.__NEMAPI_STOP__) throw new Error("Job stopped");
      const text = await provider.waitForResponse(previous);
      if (window.__NEMAPI_STOP__) throw new Error("Job stopped");
      await browser.runtime.sendMessage({
        action: "automationResult",
        jobId,
        result: text || "",
        provider: provider.id,
      });
    } catch (e) {
      try {
        await browser.runtime.sendMessage({
          action: "automationError",
          jobId,
          error: e.message || String(e),
        });
      } catch (_) {}
    } finally {
      window.__NEMAPI_RUNNING_JOB__ = null;
      rememberFinished(jobId);
      window.__NEMAPI_STOP__ = false;
    }
  }

  browser.runtime.onMessage.addListener((msg, _s, sendResponse) => {
    if (msg.action === "runAutomation") {
      runJob(msg.jobId, msg.question, msg.provider, msg.model, msg.premiumMd !== false);
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
    browser.runtime.sendMessage({ action: "contentReady" }).catch(() => {});
  } catch (_) {}
})();
