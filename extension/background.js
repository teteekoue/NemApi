/** NemApi v3.0 Firefox background: serialized jobs + wait for page ready after fresh-chat. */
"use strict";

const PROXY = "http://127.0.0.1:8080";
const PROVIDER_MATCH = [
  { id: "deepseek", re: /chat\.deepseek\.com/i },
  { id: "qwen", re: /chat\.qwen\.ai|qianwen\.com/i },
  { id: "claude", re: /claude\.ai/i },
  { id: "gemini", re: /gemini\.google\.com/i },
];
const PROVIDER_HOME = {
  deepseek: "https://chat.deepseek.com/",
  qwen: "https://chat.qwen.ai/",
  claude: "https://claude.ai/new",
  gemini: "https://gemini.google.com/app",
};

let pollTimer = null;
let busy = false;
let targetTabs = {};
let activeJobId = null;
let activeJobStartedAt = 0;
let activeJobProvider = null;
let activeJobFreshChat = false;
/** True while navigating to a blank chat OR waiting for the composer to appear. */
let settling = false;
let autoConfigEnabled = true;

function providerFromUrl(url) {
  return (PROVIDER_MATCH.find((p) => p.re.test(url || "")) || {}).id || null;
}
function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
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
  const tabs = await browser.tabs.query({});
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
        busy: busy || settling,
        settling: !!settling,
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
    await browser.scripting.executeScript({
      target: { tabId },
      files: [
        "providers/base.js",
        "providers/deepseek.js",
        "providers/qwen.js",
        "providers/claude.js",
        "providers/gemini.js",
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
      const t = await browser.tabs.get(tabId);
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
      const reply = await browser.tabs.sendMessage(tabId, { action: "ping" });
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
  settling = true;
  try {
    await extensionLog(`Fresh-chat URL → ${provider}: ${url}`);
    await browser.tabs.update(tabId, { url, active: true });
    await waitTabComplete(tabId, 30000);
    // SPA may report complete before React mounts the composer
    await delay(1200);
    await injectScripts(tabId);
    await waitForComposerReady(tabId, 20000);
    await delay(500);
  } catch (e) {
    await extensionLog(`Fresh-chat navigate failed: ${e.message || e}`, "warn");
  } finally {
    settling = false;
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

async function doPoll() {
  try {
    if (busy && activeJobId && Date.now() - activeJobStartedAt > 230000) {
      const timedOutId = activeJobId;
      const provider = activeJobProvider;
      await extensionLog(`Job ${timedOutId.slice(0, 8)} exceeded extension timeout (230s)`, "error");
      try {
        const tabId = provider ? targetTabs[provider] : null;
        if (Number.isInteger(tabId)) {
          await browser.tabs.sendMessage(tabId, { action: "stopAutomation" }).catch(() => {});
        }
      } catch (_) {}
      await postResult(timedOutId, "error", "Extension-side timeout (230s) waiting for AI response");
      busy = false;
      settling = false;
      activeJobId = null;
      activeJobProvider = null;
      activeJobFreshChat = false;
    }
    const online = await pullConfig();
    await reportTabs();
    if (!online) return schedule(2500);

    // Never pull a new job while working or while the new-chat page is still settling
    if (busy || settling) return schedule(800);

    const response = await fetch(PROXY + "/job", { cache: "no-store" });
    const job = await response.json();
    if (job.action === "ask" && job.jobId) {
      busy = true;
      activeJobId = job.jobId;
      activeJobStartedAt = Date.now();
      activeJobProvider = job.provider || null;
      activeJobFreshChat = !!job.freshChat;
      await executeJob(job);
    }
    schedule(busy || settling ? 800 : 1000);
  } catch (error) {
    await extensionLog(`Polling failed: ${error.message || error}`, "error");
    schedule(2500);
  }
}

async function executeJob(job) {
  const { jobId, question, provider, model } = job;
  const tabId = targetTabs[provider];
  if (!Number.isInteger(tabId)) {
    await postResult(jobId, "error", `No selected ${provider} tab. Select a ${provider} tab in the admin panel.`);
    busy = false;
    activeJobId = null;
    activeJobProvider = null;
    activeJobFreshChat = false;
    return;
  }
  const tab = (await listAiTabs()).find((item) => item.id === tabId);
  if (!tab || tab.provider !== provider) {
    await postResult(jobId, "error", `Selected tab ${tabId} is not an available ${provider} tab.`);
    busy = false;
    activeJobId = null;
    activeJobProvider = null;
    activeJobFreshChat = false;
    return;
  }
  try {
    await extensionLog(`Dispatch ${jobId.slice(0, 8)} → ${provider}/${model}, tab ${tabId}`);
    await browser.tabs.update(tabId, { active: true });
    await delay(200);
    await waitTabComplete(tabId, 15000);
    await injectScripts(tabId);
    // Ensure composer exists before typing (covers residual SPA load after previous fresh-chat)
    const ready = await waitForComposerReady(tabId, 20000);
    if (!ready) {
      await extensionLog("Composer still missing — attempting one more inject", "warn");
      await delay(800);
      await injectScripts(tabId);
    }
    await delay(250);

    let reply = null;
    let lastErr = null;
    for (let attempt = 1; attempt <= 3; attempt++) {
      try {
        await injectScripts(tabId);
        reply = await browser.tabs.sendMessage(tabId, {
          action: "runAutomation",
          jobId,
          question,
          provider,
          model,
          premiumMd: job.premiumMd !== false,
        });
        if (reply && reply.ok) break;
        lastErr = (reply && reply.error) || "Content script did not accept the job";
      } catch (e) {
        lastErr = e.message || String(e);
        await extensionLog(`sendMessage attempt ${attempt}/3 failed: ${lastErr}`, "warn");
        await delay(600);
        await injectScripts(tabId);
      }
    }
    if (!reply || !reply.ok) {
      throw new Error(lastErr || "Content script did not accept the job");
    }
    // busy stays true until automationResult / automationError
  } catch (error) {
    await extensionLog(`Dispatch failed: ${error.message || error}`, "error");
    await postResult(jobId, "error", error.message || String(error));
    busy = false;
    activeJobId = null;
    activeJobProvider = null;
    activeJobFreshChat = false;
  }
}

browser.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message.action === "automationResult") {
    (async () => {
      try {
        await extensionLog(
          `Response received for ${message.jobId.slice(0, 8)} (${(message.result || "").length} chars)`
        );
        // Post result FIRST so the HTTP client unblocks as soon as possible
        await postResult(message.jobId, "result", message.result || "");

        const provider = message.provider || activeJobProvider;
        const tabId = targetTabs[provider];
        const doFresh = activeJobFreshChat && message.jobId === activeJobId;

        // Keep busy=true through fresh-chat navigation so the next agent request
        // cannot start typing on a half-loaded page.
        if (doFresh && provider && Number.isInteger(tabId)) {
          settling = true;
          // Release "active job" identity but stay unavailable for new jobs
          if (message.jobId === activeJobId) {
            activeJobId = null;
            activeJobProvider = null;
            activeJobFreshChat = false;
          }
          try {
            await navigateToNewChat(provider, tabId);
          } finally {
            busy = false;
            settling = false;
          }
        } else {
          if (message.jobId === activeJobId) {
            busy = false;
            activeJobId = null;
            activeJobProvider = null;
            activeJobFreshChat = false;
          }
        }
      } catch (e) {
        await extensionLog(`automationResult handler: ${e.message || e}`, "error");
        busy = false;
        settling = false;
      }
      sendResponse({ ok: true });
    })();
    return true;
  }
  if (message.action === "automationError") {
    (async () => {
      await extensionLog(`Automation error: ${message.error}`, "error");
      await postResult(message.jobId, "error", message.error || "Automation failed");
      if (message.jobId === activeJobId) {
        busy = false;
        settling = false;
        activeJobId = null;
        activeJobProvider = null;
        activeJobFreshChat = false;
      }
      sendResponse({ ok: true });
    })();
    return true;
  }
  if (message.action === "contentReady") {
    reportTabs();
    sendResponse({ ok: true });
    return true;
  }
  return false;
});

browser.action.onClicked.addListener(() => browser.tabs.create({ url: PROXY + "/" }).catch(() => {}));
console.log("[NemApi] Background started");
doPoll();
