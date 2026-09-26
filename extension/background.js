/** NemApi v4.0 Chromium background: parallel jobs per provider + wait for page ready after fresh-chat. */
"use strict";

const PROXY = "http://100.115.92.14:8090";
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

async function pullConfig() {
  try {
    const response = await fetch(PROXY + "/extension/config", { cache: "no-store" });
    const config = await response.json();
    targetTabs = config.targetTabs || {};
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

async function postResult(jobId, action, value) {
  try {
    await fetch(PROXY + "/job", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        jobId,
        action,
        result: action === "result" ? value : "",
        error: action === "result" ? "" : value,
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
      postResult(msg.jobId, "result", msg.result);
      extensionLog(`Job ${msg.jobId} completed on ${provider}`);
    }
    sendResponse({ ok: true });
    return true;
  }
  if (msg.action === "automationError") {
    const provider = msg.provider;
    if (provider) {
      releaseJobSlot(provider, msg.jobId);
      postResult(msg.jobId, "error", msg.error);
      extensionLog(`Job ${msg.jobId} failed on ${provider}: ${msg.error}`, "error");
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
  try {
    await pullConfig();
    await reportTabs();

    // Watchdog: libère les jobs bloqués (le proxy timeout à 240s, donc tout
    // job plus vieux que JOB_WATCHDOG_MS est forcément mort côté proxy).
    try {
      const now = Date.now();
      for (const [provider, job] of Object.entries(activeJobs)) {
        if (now - job.startedAt > 230000) {
          delete activeJobs[provider];
          postResult(job.jobId, "error", "job abandoned locally (watchdog)");
          extensionLog(`Watchdog: released stuck ${provider} job ${job.jobId}`, "warn");
        }
      }
    } catch (_) {}

    // Check for pending jobs from proxy via /job endpoint
    try {
      const response = await fetch(PROXY + "/job", {
        cache: "no-store",
        signal: AbortSignal.timeout(30000),   // MV3: ne jamais laisser un fetch pendu figer le poll
      });
      const jobData = await response.json();

      if (jobData && jobData.action === "ask") {
        const provider = jobData.provider;
        const tabId = targetTabs[provider];

        if (!tabId) {
          extensionLog(`No tab for provider ${provider}`, "warn");
          postResult(jobData.jobId, "error", `No tab configured for ${provider}`);
          return;
        }

        if (activeJobs[provider]) {
          extensionLog(`Provider ${provider} busy`, "warn");
          return;
        }

        if (settlingProviders[provider]) {
          extensionLog(`Provider ${provider} settling`, "warn");
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
    schedule(3000);
  }
}

chrome.action.onClicked.addListener(async () => {
  await chrome.tabs.create({ url: "http://100.115.92.14:8090/admin.html" });
});

// Start polling
doPoll();

// MV3 keep-alive: le service worker peut être suspendu après ~30s
// d'inactivité, ce qui tue les setTimeout du poll. Une alarme périodique
// réveille le SW et relance doPoll si la boucle s'est arrêtée.
chrome.alarms.create("nemapi-poll", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "nemapi-poll") {
    pollInFlight = false;   // décoince un fetch mort (navigateur revenu de veille)
    doPoll();
  }
});

// Clean up on tab close
chrome.tabs.onRemoved.addListener((tabId) => {
  for (const [provider, job] of Object.entries(activeJobs)) {
    if (job.tabId === tabId) {
      delete activeJobs[provider];
      extensionLog(`Cleaned up job for closed tab ${tabId}`);
    }
  }
});
