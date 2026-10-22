/** NemApi v4.0 Chromium background: parallel jobs per provider + wait for page ready after fresh-chat. */
"use strict";

const PROXY = "http://127.0.0.1:8090";
// Long-poll duration of GET /job (the proxy caps it at 25 s). The extension
// used to poll every 3 s with an immediate answer, adding up to 3 s of
// latency to EVERY request; the proxy now holds the request until a job is
// ready, so a job is picked up as soon as it is queued.
const JOB_LONG_POLL = 20;
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
/** @type {Record<string, { jobId: string, startedAt: number, freshChat: boolean, tabId: number }>} */
let activeJobs = {};
/** Providers currently navigating to a blank chat / waiting for composer */
let settlingProviders = {};
let autoConfigEnabled = true;
/** Prevent overlapping poll cycles */
let pollInFlight = false;

function providerFromUrl(url) {
  return (PROVIDER_MATCH.find((p) => p.re.test(url || "")) || {}).id || null;
}
function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
function anyBusy() {
  return Object.keys(activeJobs).length > 0 || Object.keys(settlingProviders).length > 0;
}
function isProviderFree(provider) {
  return !activeJobs[provider] && !settlingProviders[provider];
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
    }))
    .filter((tab) => tab.provider);
}

async function reportTabs() {
  try {
    await fetch(PROXY + "/extension/tabs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        tabs: await listAiTabs(),
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

async function injectScripts(tabId) {
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
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      files: [
        "providers/base.js",
        "providers/deepseek.js",
        "providers/qwen.js",
        "providers/claude.js",
        "providers/gemini.js",
        "providers/chatgpt.js",
        "providers/kimi.js",
        "providers/zai.js",
        "content.js",
      ],
    });
  } catch (error) {
    await extensionLog(`Script injection: ${error.message || error}`, "warn");
  }
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
    await delay(200);
  }
  return false;
}

/**
 * Wait until the provider page exposes a usable chat input.
 * Critical after URL navigation to /new — agents send the next request too fast otherwise.
 */
async function waitForComposerReady(tabId, timeoutMs = 25000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    try {
      await injectScripts(tabId);
      const reply = await chrome.tabs.sendMessage(tabId, { action: "ping" });
      if (reply && reply.pong && reply.hasInput) {
        await extensionLog(`Composer ready on tab ${tabId}`);
        return true;
      }
    } catch (_) {
      // content script not ready yet
    }
    await delay(400);
  }
  await extensionLog(`Composer not ready after ${timeoutMs}ms on tab ${tabId}`, "warn");
  return false;
}

async function navigateToNewChat(provider, tabId) {
  const url = PROVIDER_HOME[provider];
  if (!url || !Number.isInteger(tabId)) return;
  settlingProviders[provider] = true;
  try {
    await extensionLog(`Fresh-chat URL → ${provider}: ${url}`);
    await chrome.tabs.update(tabId, { url, active: false });
    await waitTabComplete(tabId, 30000);
    await delay(1200);
    await injectScripts(tabId);
    await waitForComposerReady(tabId, 20000);
    await delay(500);
  } catch (e) {
    await extensionLog(`Fresh-chat navigate failed: ${e.message || e}`, "warn");
  } finally {
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

// Listen for messages from content scripts
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.action === "automationResult") {
    const provider = msg.provider;
    if (provider) {
      releaseJobSlot(provider, msg.jobId);
      postResult(msg.jobId, "result", msg.result, undefined, { source: msg.source, bridge: msg.bridge, partial: msg.partial });
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

// Main polling loop - FETCH JOBS FROM /job
async function doPoll() {
  if (pollInFlight) {
    schedule(2000);
    return;
  }
  pollInFlight = true;
  let polledJob = false;
  try {
    // Report our tabs FIRST: the proxy may auto-configure a target tab from
    // this report. Pulling the config afterwards makes sure that freshly
    // auto-configured tab is known here before we fetch a job in the same
    // cycle — otherwise the job is rejected with "No tab configured" and
    // retried a full cycle later (the first request after opening a tab
    // always failed).
    await reportTabs();
    await pullConfig();
    
    // Watchdog: libère les jobs bloqués (le proxy abandonne après jobTimeoutMs, donc tout
    // job plus vieux que JOB_WATCHDOG_MS est forcément mort côté proxy).
    try {
      const now = Date.now();
      for (const [provider, job] of Object.entries(activeJobs)) {
        if (now - job.startedAt > jobTimeoutMs + 15000) {
          delete activeJobs[provider];
          postResult(job.jobId, "error", "job abandoned locally (watchdog)", true);
          extensionLog(`Watchdog: released stuck ${provider} job ${job.jobId}`, "warn");
        }
      }
    } catch (_) {}

    // Check for pending jobs from proxy via /job endpoint
    try {
      // MV3: ne jamais laisser un fetch pendu figer le poll
      // long-poll: the proxy holds the request until a job is queued
      const response = await fetchWithTimeout(
        PROXY + "/job?wait=" + JOB_LONG_POLL,
        { cache: "no-store" },
        30000
      );
      const jobData = await response.json();
      
      if (jobData && jobData.action === "ask") {
        const provider = jobData.provider;
        const tabId = targetTabs[provider];
        polledJob = true;
        
        if (!tabId) {
          extensionLog(`No tab for provider ${provider}`, "warn");
          postResult(jobData.jobId, "error", `No tab configured for ${provider}`);
          return;
        }
        
        if (activeJobs[provider]) {
          // The job was already taken from the proxy queue: dropping it silently
          // leaves the client waiting for the whole job timeout (900 s). Report
          // the failure instead so the slot is released and the caller gets an
          // error right away.
          extensionLog(`Provider ${provider} busy — job rejected`, "warn");
          postResult(jobData.jobId, "error", `Provider ${provider} is still busy`);
          return;
        }

        if (settlingProviders[provider]) {
          extensionLog(`Provider ${provider} settling — job rejected`, "warn");
          postResult(jobData.jobId, "error", `Provider ${provider} is settling (page reload)`);
          return;
        }
        
        // Start the job
        activeJobs[provider] = { 
          jobId: jobData.jobId, 
          startedAt: Date.now(), 
          freshChat: false,
          tabId: tabId
        };
        
        try {
          extensionLog(`Starting job ${jobData.jobId} on ${provider}`);
          await injectScripts(tabId);
          
          // Ensure fresh chat if needed
          if (jobData.freshChat !== false) {
            await navigateToNewChat(provider, tabId);
          }
          
          await chrome.tabs.sendMessage(tabId, {
            action: "runAutomation",
            jobId: jobData.jobId,
            question: jobData.question,
            provider: provider,
            model: jobData.model,
            premiumMd: jobData.premiumMd !== false,
            jobTimeoutMs: Number(jobData.jobTimeoutMs) || jobTimeoutMs,
            // multimodal: OpenAI image_url parts, forwarded untouched
            images: Array.isArray(jobData.images) ? jobData.images : null,
          });
        } catch (e) {
          extensionLog(`Job start failed: ${e.message || e}`, "error");
          releaseJobSlot(provider, jobData.jobId);
          postResult(jobData.jobId, "error", e.message || String(e));
        }
      }
    } catch (e) {
      // No pending jobs or proxy not ready - ignore
    }
  } catch (e) {
    await extensionLog(`Poll error: ${e.message || e}`, "error");
  } finally {
    pollInFlight = false;
    // The long-poll already waited for a job (or timed out): a short delay is
    // enough, a long one would add latency to the next request.
    schedule(polledJob ? 300 : 1000);
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
  extensionLog("chrome.alarms indisponible → boucle de poll par setTimeout (mode compatible)");
  const tick = () => {
    pollInFlight = false; // décoince un fetch mort (navigateur revenu de veille)
    doPoll();
    pollLoopTimer = setTimeout(tick, 30000);
  };
  pollLoopTimer = setTimeout(tick, 30000);
}

// Start polling
doPoll();

// MV3 keep-alive: le service worker peut être suspendu après ~30s
// d'inactivité, ce qui tue les setTimeout du poll. Une alarme périodique
// réveille le SW et relance doPoll si la boucle s'est arrêtée.
if (HAS_ALARMS) {
  chrome.alarms.create("nemapi-poll", { periodInMinutes: 0.5 });
  chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm.name === "nemapi-poll") {
      pollInFlight = false;   // décoince un fetch mort (navigateur revenu de veille)
      doPoll();
    }
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
    if (SCRIPTS_WORLD_MAIN) return; // main world injection already handled by injectScripts
    injectScripts(details.tabId).catch(() => {});
  });
}

// Everything else (alarms, webNavigation, executeScript) may be missing on
// old Chromium builds: the extension must still load and work.
try {
  ensurePollLoop();
} catch (_) {}

// Clean up on tab close
chrome.tabs.onRemoved.addListener((tabId) => {
  for (const [provider, job] of Object.entries(activeJobs)) {
    if (job.tabId === tabId) {
      delete activeJobs[provider];
      extensionLog(`Cleaned up job for closed tab ${tabId}`);
    }
  }
});
