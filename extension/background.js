/**
 * NemApi background service worker: parallel jobs per provider, one poll loop.
 *
 * RULES THAT COST A BUG EACH (v5.4)
 *
 * - The poll loop is a SINGLE loop. The alarm used to reset `pollInFlight`
 *   while a long-poll was still open, which started a second loop: two
 *   `GET /job` in flight, two dispatches, and a job rejected with "provider
 *   busy".
 * - The loop never AWAITS a job: navigating a tab, waiting for a composer or
 *   waiting for an answer inside the loop blocked every other provider (and
 *   was lost when the service worker was suspended).
 * - A job that cannot finish is reported as an error at once. A closed or
 *   navigated tab used to leave the job active for the whole 900 s timeout.
 * - Provider tabs are protected from Chrome's memory saver
 *   (`autoDiscardable: false`) and from freezing (the content script holds a
 *   Web Lock): a discarded or frozen tab runs nothing, and the request only
 *   completed when the user activated the tab.
 */
"use strict";

const PROXY = "http://127.0.0.1:8090";
// Long-poll duration of GET /job (the proxy caps it at 25 s). The extension
// used to poll every 3 s with an immediate answer, adding up to 3 s of
// latency to EVERY request; the proxy now holds the request until a job is
// ready, so a job is picked up as soon as it is queued.
const JOB_LONG_POLL = 20;
// A stopped heartbeat for this long means the tab is frozen, crashed or
// reloaded: the provider slot must be freed (the content script pings every
// 15 s while it works).
const PROGRESS_TIMEOUT_MS = 120000;
// A poll cycle that lasts longer than this is considered dead (a fetch that
// never settled, a service worker suspended mid-await).
const POLL_STUCK_MS = (JOB_LONG_POLL + 15) * 1000;
const PROVIDER_MATCH = [
  { id: "deepseek", re: /chat\.deepseek\.com/i },
  { id: "qwen", re: /chat\.qwen\.ai|qianwen\.com/i },
  { id: "claude", re: /claude\.ai/i },
  { id: "gemini", re: /gemini\.google\.com/i },
  { id: "chatgpt", re: /chatgpt\.com|chat\.openai\.com/i },
  { id: "kimi", re: /kimi\.com|kimi\.ai|moonshot\.cn/i },
  { id: "zai", re: /chat\.z\.ai|\.z\.ai|chatglm\.cn|bigmodel\.cn/i },
];
const PROVIDER_HOME = {
  deepseek: "https://chat.deepseek.com/",
  qwen: "https://chat.qwen.ai/",
  claude: "https://claude.ai/new",
  gemini: "https://gemini.google.com/app",
  chatgpt: "https://chatgpt.com/",
  kimi: "https://www.kimi.ai/",
  zai: "https://chat.z.ai/",
};
const PROVIDER_FILES = {
  deepseek: "providers/deepseek.js",
  qwen: "providers/qwen.js",
  claude: "providers/claude.js",
  gemini: "providers/gemini.js",
  chatgpt: "providers/chatgpt.js",
  kimi: "providers/kimi.js",
  zai: "providers/zai.js",
};

// Chrome 111 is the first version with chrome.scripting world:"MAIN" and the
// alarms API in an MV3 service worker; older Chromium (103…) are supported
// through fallbacks below.
const SCRIPTS_WORLD_MAIN = (() => {
  try {
    return !!(chrome.scripting && chrome.scripting.ExecutionWorld && chrome.scripting.ExecutionWorld.MAIN);
  } catch (_) {
    return false;
  }
})();
const HAS_ALARMS = !!(chrome.alarms && chrome.alarms.create);
const HAS_WEBNAV = !!(chrome.webNavigation && chrome.webNavigation.onCommitted);
// setTimeout-driven fallback loop for the poll when chrome.alarms is missing
let pollLoopStarted = false;
let pollLoopTimer = null;
let isWorker = false;
try {
  isWorker = typeof ServiceWorkerGlobalScope !== "undefined" && self instanceof ServiceWorkerGlobalScope;
} catch (_) {}

/** fetch() with a timeout that also works without AbortSignal.timeout (Chrome 103). */
async function fetchWithTimeout(url, opts, ms) {
  const options = Object.assign({}, opts || {});
  let controller = null;
  if (typeof AbortController === "function") {
    controller = new AbortController();
    options.signal = controller.signal;
  }
  let timer = null;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => {
      if (controller) {
        try {
          controller.abort();
        } catch (_) {}
      }
      reject(new Error("fetch timeout after " + ms + "ms"));
    }, ms);
  });
  try {
    return await Promise.race([fetch(url, options), timeout]);
  } finally {
    clearTimeout(timer);
  }
}

let pollTimer = null;
let targetTabs = {};
/** @type {Record<string, { jobId: string, startedAt: number, lastProgress: number, freshChat: boolean, tabId: number }>} */
let activeJobs = {};
/** Providers currently navigating to a blank chat / waiting for composer */
let settlingProviders = {};
/** Tabs we are navigating ourselves (a navigation event is not a lost job) */
let navigating = new Set();
let autoConfigEnabled = true;
/** A poll cycle is running; guarded by a DEADLINE, never force-reset. */
let polling = false;
let pollStartedAt = 0;
/** Last time each provider tab was protected from the memory saver. */
let protectedTabs = new Set();

function providerFromUrl(url) {
  return (PROVIDER_MATCH.find((p) => p.re.test(url || "")) || {}).id || null;
}
function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
function anyBusy() {
  return Object.keys(activeJobs).length > 0 || Object.keys(settlingProviders).length > 0;
}

async function extensionLog(message, level = "info") {
  try {
    await fetch(PROXY + "/extension/log", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, level }),
    });
  } catch (_) {}
}

async function listAiTabs() {
  const tabs = await chrome.tabs.query({});
  return tabs
    .map((tab) => ({
      id: tab.id,
      windowId: tab.windowId,
      url: tab.url,
      title: tab.title || "",
      provider: providerFromUrl(tab.url),
      active: !!tab.active,
      discarded: !!tab.discarded,
      frozen: tab.frozen === true,
    }))
    .filter((tab) => tab.provider);
}

/**
 * Keep provider tabs out of Chrome's memory saver.
 *
 * A discarded tab is unloaded: its content script is gone, and the job that
 * was running there is lost — the request waited for the proxy timeout. The
 * tab is restored on activation, which is why "clicking the tab" seemed to
 * fix the request.
 */
async function protectTabs(tabs) {
  for (const tab of tabs) {
    if (!Number.isInteger(tab.id) || protectedTabs.has(tab.id)) continue;
    protectedTabs.add(tab.id);
    try {
      await chrome.tabs.update(tab.id, { autoDiscardable: false });
    } catch (_) {}
  }
}

async function reportTabs() {
  try {
    const tabs = await listAiTabs();
    protectTabs(tabs);
    await fetch(PROXY + "/extension/tabs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        tabs,
        busy: anyBusy(),
        settling: Object.keys(settlingProviders).length > 0,
        activeProviders: Object.keys(activeJobs),
        settlingProviders: Object.keys(settlingProviders),
      }),
    });
    return true;
  } catch (_) {
    return false;
  }
}

// proxy job timeout (GET /extension/config): long answers (big files written
// by an agent) can take several minutes — the watchdog follows this value
let jobTimeoutMs = 900000;

async function pullConfig() {
  try {
    const response = await fetch(PROXY + "/extension/config", { cache: "no-store" });
    const config = await response.json();
    targetTabs = config.targetTabs || {};
    if (Number(config.jobTimeoutMs) > 0) jobTimeoutMs = Number(config.jobTimeoutMs);
    autoConfigEnabled = config.autoConfig !== false;
    return true;
  } catch (_) {
    return false;
  }
}

function schedule(ms) {
  if (pollTimer) clearTimeout(pollTimer);
  pollTimer = setTimeout(doPoll, ms);
}

/**
 * Is the router already running in this tab?
 *
 * Re-injecting nine files before every job re-parsed ~100 KB of JavaScript in
 * the page on every request, and a page that has to compile our scripts again
 * for each of its requests feels slow (that is one of the reasons a provider
 * tab had to be closed and reopened after a while).
 *
 * After an extension reload, the old content scripts are ORPHANED (their
 * `chrome.runtime` is dead): `sendMessage` then fails or nothing answers —
 * which is exactly what tells us to inject again.
 */
async function routerAlive(tabId) {
  try {
    const reply = await chrome.tabs.sendMessage(tabId, { action: "ping" });
    return !!(reply && reply.pong);
  } catch (_) {
    return false;
  }
}

async function injectScripts(tabId, providerId) {
  // MAIN-world bridge first: after an extension reload, tabs that were already
  // open do not receive the manifest content scripts (page_bridge.js
  // included) — without it the answers are rebuilt from the rendered HTML,
  // which loses backslashes, $ and quotes of the tool calls.
  // Chrome < 111 has no executeScript world:"MAIN": page_bridge.js then comes
  // only from the declarative content script (match_origin_as_fallback) and
  // the re-injection is done again when the page commits (see webNavigation).
  if (SCRIPTS_WORLD_MAIN) {
    try {
      await chrome.scripting.executeScript({
        target: { tabId },
        world: "MAIN",
        files: ["providers/page_bridge.js"],
      });
    } catch (error) {
      await extensionLog(`Page bridge injection: ${error.message || error}`, "warn");
    }
  }
  // base.js + the provider of THIS tab + the router. Injecting all seven
  // providers before every job (v5.3) parsed ~200 KB of JavaScript in the tab
  // for nothing: one provider matches the URL, the others only add the cost.
  const files = ["providers/base.js"];
  if (providerId && PROVIDER_FILES[providerId]) files.push(PROVIDER_FILES[providerId]);
  else files.push(...Object.values(PROVIDER_FILES));
  files.push("content.js");
  try {
    await chrome.scripting.executeScript({ target: { tabId }, files });
  } catch (error) {
    await extensionLog(`Script injection: ${error.message || error}`, "warn");
    return false;
  }
  return true;
}

/** Inject only when needed: a live router is left alone. */
async function ensureScripts(tabId, providerId) {
  if (await routerAlive(tabId)) return true;
  return injectScripts(tabId, providerId);
}

async function waitTabComplete(tabId, timeoutMs = 30000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    try {
      const t = await chrome.tabs.get(tabId);
      if (t.status === "complete") return true;
    } catch (_) {
      return false;
    }
    await delay(150);
  }
  return false;
}

/**
 * Wait until the provider page exposes a usable chat input.
 *
 * Critical after URL navigation to /new — agents send the next request too
 * fast otherwise. The scripts are injected ONCE here: the old version
 * re-injected nine files on every 400 ms attempt (up to 60 injections for a
 * single navigation), which is what made the provider tab feel slow.
 */
async function waitForComposerReady(tabId, providerId, timeoutMs = 20000) {
  const t0 = Date.now();
  let injected = false;
  while (Date.now() - t0 < timeoutMs) {
    try {
      const reply = await chrome.tabs.sendMessage(tabId, { action: "ping" });
      if (reply && reply.pong && reply.hasInput) {
        await extensionLog(`Composer ready on tab ${tabId}`);
        return true;
      }
    } catch (_) {
      // content script not ready yet: inject (once) and try again
      if (!injected) {
        injected = true;
        await injectScripts(tabId, providerId);
      }
    }
    await delay(250);
  }
  await extensionLog(`Composer not ready after ${timeoutMs}ms on tab ${tabId}`, "warn");
  return false;
}

async function navigateToNewChat(provider, tabId) {
  const url = PROVIDER_HOME[provider];
  if (!url || !Number.isInteger(tabId)) return;
  settlingProviders[provider] = true;
  navigating.add(tabId);
  try {
    await extensionLog(`Fresh-chat URL → ${provider}: ${url}`);
    await chrome.tabs.update(tabId, { url, active: false });
    await waitTabComplete(tabId, 25000);
    // The manifest content scripts run on the new page; waitForComposerReady
    // only injects when nothing answers (a page that predates an extension
    // reload), instead of injecting on top of a page that already has them.
    await waitForComposerReady(tabId, provider, 20000);
  } catch (e) {
    await extensionLog(`Fresh-chat navigate failed: ${e.message || e}`, "warn");
  } finally {
    // a navigation of our own is not a lost job: ignore its events briefly
    setTimeout(() => navigating.delete(tabId), 2000);
    delete settlingProviders[provider];
  }
}

async function postResult(jobId, action, value, typed, meta) {
  try {
    await fetch(PROXY + "/job", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        jobId,
        action,
        result: action === "result" ? value : "",
        error: action === "result" ? "" : value,
        // incremental context: was the prompt submitted before the failure?
        typed: typeof typed === "boolean" ? typed : null,
        // diagnostics: how the answer was read (react = original markdown)
        source: (meta && meta.source) || null,
        bridge: meta && typeof meta.bridge === "boolean" ? meta.bridge : null,
        timer: (meta && meta.timer) || null,
        partial: !!(meta && meta.partial),
      }),
    });
  } catch (error) {
    console.error("[NemApi] postResult", error);
  }
}

function clearJob(provider, jobId) {
  const cur = activeJobs[provider];
  if (cur && (!jobId || cur.jobId === jobId)) {
    delete activeJobs[provider];
  }
}

function releaseJobSlot(provider, jobId) {
  clearJob(provider, jobId);
}

/** Fail a job whose tab went away (closed, reloaded, discarded, navigated). */
async function failJob(provider, reason) {
  const job = activeJobs[provider];
  if (!job) return;
  delete activeJobs[provider];
  await extensionLog(`Job ${job.jobId} on ${provider}: ${reason}`, "warn");
  await postResult(job.jobId, "error", reason, true);
}

function jobForTab(tabId) {
  for (const [provider, job] of Object.entries(activeJobs)) {
    if (job.tabId === tabId) return { provider, job };
  }
  return null;
}

/**
 * Watchdog: a job whose tab stopped reporting progress is dead.
 *
 * The content script pings every 15 s while it works. A frozen tab, a crashed
 * renderer or a reloaded page stops pinging: without this the provider slot
 * stayed taken for the whole 900 s job timeout (and the extension rejected
 * every later job for that provider).
 */
async function runWatchdog() {
  const now = Date.now();
  for (const [provider, job] of Object.entries(activeJobs)) {
    const last = job.lastProgress || job.startedAt;
    if (now - last > PROGRESS_TIMEOUT_MS) {
      delete activeJobs[provider];
      await extensionLog(`Watchdog: ${provider} job ${job.jobId} silent for ${Math.round((now - last) / 1000)}s`, "warn");
      await postResult(job.jobId, "error", "the provider tab stopped responding (frozen, reloaded or closed)", true);
    } else if (now - job.startedAt > jobTimeoutMs + 15000) {
      // the proxy has certainly given up: nothing will ever read this result
      delete activeJobs[provider];
      await postResult(job.jobId, "error", "job abandoned locally (watchdog)", true);
      await extensionLog(`Watchdog: released stuck ${provider} job ${job.jobId}`, "warn");
    }
  }
}

// Listen for messages from content scripts
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.action === "automationResult") {
    const provider = msg.provider;
    if (provider) {
      releaseJobSlot(provider, msg.jobId);
      postResult(msg.jobId, "result", msg.result, undefined, {
        source: msg.source,
        bridge: msg.bridge,
        timer: msg.timer,
        partial: msg.partial,
      });
      extensionLog(`Job ${msg.jobId} completed on ${provider}`);
    }
    sendResponse({ ok: true });
    return true;
  }
  if (msg.action === "automationError") {
    // provider may be missing (page not recognised): find it from the job id
    const provider = msg.provider
      || Object.keys(activeJobs).find((p) => activeJobs[p] && activeJobs[p].jobId === msg.jobId);
    if (provider) releaseJobSlot(provider, msg.jobId);
    if (msg.jobId) {
      postResult(msg.jobId, "error", msg.error, msg.typed);
      extensionLog(`Job ${msg.jobId} failed on ${provider || "?"}: ${msg.error}`, "error");
    }
    sendResponse({ ok: true });
    return true;
  }
  if (msg.action === "jobProgress") {
    const provider = Object.keys(activeJobs).find(
      (p) => activeJobs[p] && activeJobs[p].jobId === msg.jobId
    );
    if (provider) activeJobs[provider].lastProgress = Date.now();
    sendResponse({ ok: true });
    return true;
  }
  if (msg.action === "extensionLog") {
    extensionLog(String(msg.message || ""), msg.level === "warn" ? "warn" : "info");
    sendResponse({ ok: true });
    return true;
  }
  if (msg.action === "contentReady") {
    const tabId = sender.tab ? sender.tab.id : null;
    if (tabId) {
      const provider = providerFromUrl(sender.tab && sender.tab.url);
      if (provider) {
        targetTabs[provider] = tabId;
        extensionLog(`Content ready on ${provider} tab ${tabId}`);
      }
      // Proactively install MAIN-world clipboard hook (CSP-safe)
      installPageClipHook(tabId).catch(() => {});
    }
    sendResponse({ ok: true });
    return true;
  }
  if (msg.action === "installPageClipHook") {
    const tabId = sender.tab ? sender.tab.id : msg.tabId;
    if (tabId) {
      installPageClipHook(tabId)
        .then(() => sendResponse({ ok: true }))
        .catch((e) => sendResponse({ ok: false, error: String(e) }));
      return true;
    }
    sendResponse({ ok: false, error: "no tab" });
    return true;
  }
  return false;
});

/**
 * Inject clipboard intercept into the page MAIN world so writeText from React
 * is visible to the isolated content script via CustomEvent.
 */
async function installPageClipHook(tabId) {
  // Chrome < 111 cannot inject a function into the MAIN world: page_bridge.js
  // installs the same hook from there (ensurePageClipboardHook → this function
  // is skipped, the page world is not reachable anyway).
  if (!SCRIPTS_WORLD_MAIN) return;
  await chrome.scripting.executeScript({
    target: { tabId },
    world: "MAIN",
    func: function () {
      if (window.__NEMAPI_CLIP_HOOK_INSTALLED__) return;
      window.__NEMAPI_CLIP_HOOK_INSTALLED__ = true;
      function emit(text, force) {
        try {
          if (typeof text !== "string") {
            if (text == null) return;
            try {
              text = String(text);
            } catch (_) {
              return;
            }
          }
          if (!text || text === "[object Object]") return;
          // Skip HTML payloads unless forced (plain is preferred)
          if (
            !force &&
            /<\/?(p|div|span|strong|em|br|ul|ol|li|h[1-6])\b/i.test(text) &&
            text.indexOf("<") !== -1
          ) {
            return;
          }
          window.dispatchEvent(new CustomEvent("__nemapi_clip_capture__", { detail: text }));
        } catch (_) {}
      }
      try {
        const clip = navigator.clipboard;
        if (!clip) return;
        if (typeof clip.writeText === "function") {
          const orig = clip.writeText.bind(clip);
          clip.writeText = async function (text) {
            emit(text, true); // writeText is always plain
            return orig(text);
          };
        }
        if (typeof clip.write === "function") {
          const origW = clip.write.bind(clip);
          clip.write = async function (items) {
            try {
              for (const item of items || []) {
                if (!item || !item.types) continue;
                const types = Array.from(item.types || []);
                // Prefer text/plain only for the event payload
                if (types.includes("text/plain")) {
                  const blob = await item.getType("text/plain");
                  emit(await blob.text(), true);
                } else {
                  for (const type of types) {
                    if (String(type).startsWith("text/")) {
                      const blob = await item.getType(type);
                      emit(await blob.text(), type === "text/plain");
                    }
                  }
                }
              }
            } catch (_) {}
            return origW(items);
          };
        }
      } catch (_) {}
      document.addEventListener(
        "copy",
        function (e) {
          try {
            const t = (e.clipboardData && e.clipboardData.getData("text/plain")) || "";
            if (t) emit(t, true);
          } catch (_) {}
        },
        true
      );
    },
  });
}

/**
 * Run one job on its tab.
 *
 * Never awaited by the poll loop: a fresh-chat navigation or a slow injection
 * used to block every other provider (and lost the job when the service
 * worker was suspended mid-await).
 */
async function runJob(tabId, provider, job) {
  try {
    // The provider slot has to be free. It can be briefly taken by a job whose
    // result is still in flight: wait for it instead of rejecting a job the
    // proxy has already handed out (the client saw a failure).
    const deadline = Date.now() + 30000;
    while (settlingProviders[provider] && Date.now() < deadline) {
      await delay(200);
    }
    if (settlingProviders[provider]) {
      await extensionLog(`Provider ${provider} still settling — job rejected`, "warn");
      delete activeJobs[provider];
      await postResult(job.jobId, "error", `Provider ${provider} is settling (page reload)`);
      return;
    }
    extensionLog(`Starting job ${job.jobId} on ${provider}`);
    await ensureScripts(tabId, provider);
    // Ensure fresh chat if needed
    if (job.freshChat !== false) {
      await navigateToNewChat(provider, tabId);
    }
    await sendJob(tabId, provider, job);
  } catch (e) {
    const message = (e && e.message) || String(e);
    await extensionLog(`Job start failed: ${message}`, "error");
    releaseJobSlot(provider, job.jobId);
    await postResult(job.jobId, "error", message);
  }
}

/**
 * Hand the job to the content script, and recover a tab that lost it.
 *
 * A discarded or crashed tab has no content script: the send fails and the
 * request used to die with "Receiving end does not exist" — the user then had
 * to open the tab and retry. Reloading costs a few seconds but keeps the
 * request alive.
 */
async function sendJob(tabId, provider, job) {
  try {
    await chrome.tabs.sendMessage(tabId, {
      action: "runAutomation",
      jobId: job.jobId,
      question: job.question,
      provider: provider,
      model: job.model,
      premiumMd: job.premiumMd !== false,
      jobTimeoutMs: Number(job.jobTimeoutMs) || jobTimeoutMs,
      // multimodal: OpenAI image_url parts, forwarded untouched
      images: Array.isArray(job.images) ? job.images : null,
    });
    return true;
  } catch (first) {
    await extensionLog(
      `Send to ${provider} tab failed (${(first && first.message) || first}) → reloading the tab`,
      "warn"
    );
    try {
      navigating.add(tabId);
      await chrome.tabs.reload(tabId);
      await waitTabComplete(tabId, 25000);
      await injectScripts(tabId, provider);
      await waitForComposerReady(tabId, provider, 20000);
      await chrome.tabs.sendMessage(tabId, {
        action: "runAutomation",
        jobId: job.jobId,
        question: job.question,
        provider: provider,
        model: job.model,
        premiumMd: job.premiumMd !== false,
        jobTimeoutMs: Number(job.jobTimeoutMs) || jobTimeoutMs,
        images: Array.isArray(job.images) ? job.images : null,
      });
      return true;
    } catch (second) {
      setTimeout(() => navigating.delete(tabId), 1000);
      throw second;
    }
  }
}

/** Take a job from the proxy and hand it to its tab. */
async function acceptJob(jobData) {
  const provider = jobData.provider;
  const tabId = targetTabs[provider];
  if (!tabId) {
    extensionLog(`No tab for provider ${provider}`, "warn");
    await postResult(jobData.jobId, "error", `No tab configured for ${provider}`);
    return;
  }
  if (activeJobs[provider]) {
    // The job was already taken from the proxy queue: dropping it silently
    // leaves the client waiting for the whole job timeout (900 s). Report
    // the failure instead so the slot is released and the caller gets an
    // error right away.
    extensionLog(`Provider ${provider} busy — job rejected`, "warn");
    await postResult(jobData.jobId, "error", `Provider ${provider} is still busy`);
    return;
  }
  activeJobs[provider] = {
    jobId: jobData.jobId,
    startedAt: Date.now(),
    lastProgress: Date.now(),
    freshChat: jobData.freshChat !== false,
    tabId: tabId,
  };
  // Deliberately not awaited: the poll loop must stay available for the other
  // providers (and for the answer of this one).
  runJob(tabId, provider, jobData);
}

/**
 * Tab report + config refresh + tab protection, at their own pace.
 *
 * Never awaited by the poll loop: a `chrome.tabs.query` (and the two HTTP
 * calls) between two long-polls delayed the dispatch of a queued job.
 */
let housekeepingAt = 0;
let housekeepingAgain = false;
async function housekeeping() {
  if (housekeepingAgain) return; // one run at a time
  housekeepingAgain = true;
  try {
    await reportTabs();
    const now = Date.now();
    if (now - housekeepingAt > 3000) {
      housekeepingAt = now;
      await pullConfig();
    }
  } finally {
    housekeepingAgain = false;
  }
}

// Main polling loop - FETCH JOBS FROM /job
async function doPoll() {
  if (polling) {
    if (Date.now() - pollStartedAt < POLL_STUCK_MS) return; // normal: a cycle is open
    // The previous cycle never came back (a fetch that hung, or a service
    // worker suspended in the middle of it). Take over instead of resetting
    // the flag — resetting it started a SECOND loop in parallel.
    await extensionLog("Poll cycle stuck → new cycle", "warn");
  }
  polling = true;
  pollStartedAt = Date.now();
  let delayMs = 0;
  try {
    // Housekeeping is not on the critical path of a request: it used to be
    // awaited BEFORE the long-poll of every cycle, adding two round trips and
    // a `chrome.tabs.query` between a job being ready and being dispatched.
    housekeeping().catch(() => {});
    await runWatchdog();
    try {
      // long-poll: the proxy holds the request until a job is queued
      const response = await fetchWithTimeout(
        PROXY + "/job?wait=" + JOB_LONG_POLL,
        { cache: "no-store" },
        (JOB_LONG_POLL + 10) * 1000
      );
      const jobData = await response.json();
      if (jobData && jobData.action === "ask") {
        await acceptJob(jobData);
      }
    } catch (e) {
      // No pending jobs, proxy not ready or fetch timed out: back off a little
      // so a dead proxy does not spin the service worker at full speed.
      delayMs = 1000;
    }
  } catch (e) {
    await extensionLog(`Poll error: ${e.message || e}`, "error");
    delayMs = 1000;
  } finally {
    polling = false;
    // The long-poll already waited for a job (or timed out): poll again at
    // once. The 1 s pause of v5.3 was pure latency on the next request.
    schedule(delayMs);
  }
}

chrome.action.onClicked.addListener(async () => {
  await chrome.tabs.create({ url: "http://127.0.0.1:8090/admin.html" });
});

/**
 * Poll-loop fallback for browsers without chrome.alarms in the service worker
 * (Chrome < 111: "alarms" is not available there, so the MV3 worker would be
 * suspended after ~30s and never poll again). Re-arms itself through
 * setTimeout; `isWorker` avoids a second loop in a document context.
 */
function ensurePollLoop() {
  if (HAS_ALARMS || pollLoopStarted || !isWorker) return;
  pollLoopStarted = true;
  extensionLog("chrome.alarms unavailable → poll loop driven by setTimeout (compatibility mode)");
  const tick = () => {
    // A fetch that never settled must not stop the loop for ever: the
    // deadline in doPoll() decides, the flag is not reset here.
    doPoll();
    pollLoopTimer = setTimeout(tick, 30000);
  };
  pollLoopTimer = setTimeout(tick, 30000);
}

// Start polling — on install/update too: an extension reloaded while the
// browser stayed open used to poll nothing until the next browser event.
try {
  chrome.runtime.onInstalled.addListener(() => doPoll());
  chrome.runtime.onStartup.addListener(() => doPoll());
} catch (_) {}

doPoll();

// MV3 keep-alive: Chrome suspends an idle service worker after ~30 s, which
// kills the poll's setTimeout. A periodic alarm wakes the worker up and
// restarts doPoll if the loop died (a safety net: the pending long-poll fetch
// is what really keeps it alive).
if (HAS_ALARMS) {
  // 0.5 min is the floor Chrome accepts on recent versions (older ones clamp
  // it to 1 min): this alarm only restarts a poll loop that died with the
  // service worker, so its period is not on the critical path any more.
  chrome.alarms.create("nemapi-poll", { periodInMinutes: 0.5 });
  chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm.name === "nemapi-poll") doPoll();
  });
}

// page_bridge.js (MAIN world) cannot be re-injected with executeScript before
// Chrome 111: the declarative content script covers the page load, and a
// navigation of an AI tab (fresh chat, reload, extension reload) also
// re-injects the isolated-world router, which asks the bridge for its state.
if (HAS_WEBNAV) {
  chrome.webNavigation.onCommitted.addListener((details) => {
    if (details.frameId !== 0) return;
    const provider = providerFromUrl(details.url);
    if (!provider) return;
    if (!SCRIPTS_WORLD_MAIN) injectScripts(details.tabId, provider).catch(() => {});
    // A page that commits while a job is running has thrown the content
    // script (and the answer) away: the job would wait for its timeout.
    if (navigating.has(details.tabId)) return;
    const running = jobForTab(details.tabId);
    if (running) failJob(running.provider, "the provider tab navigated away during the job");
  });
}

// Everything else (alarms, webNavigation, executeScript) may be missing on
// old Chromium builds: the extension must still load and work.
try {
  ensurePollLoop();
} catch (_) {}

// Clean up on tab close: the job must fail NOW, not after the 900 s timeout.
chrome.tabs.onRemoved.addListener((tabId) => {
  navigating.delete(tabId);
  protectedTabs.delete(tabId);
  const running = jobForTab(tabId);
  if (running) {
    failJob(running.provider, `the ${running.provider} tab was closed during the job`);
  }
});

// A tab that reloads or is discarded loses its content script: same outcome.
if (chrome.tabs.onUpdated) {
  chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
    if (!changeInfo) return;
    if (changeInfo.discarded) {
      protectedTabs.delete(tabId);
      const running = jobForTab(tabId);
      if (running) failJob(running.provider, `the ${running.provider} tab was discarded during the job`);
      return;
    }
    if (changeInfo.status === "loading" && !navigating.has(tabId)) {
      const running = jobForTab(tabId);
      if (running) failJob(running.provider, `the ${running.provider} tab reloaded during the job`);
    }
  });
}
