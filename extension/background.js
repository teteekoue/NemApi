/** NemApi v3.0 Firefox background: parallel jobs per provider + wait for page ready after fresh-chat. */
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
let targetTabs = {};
/** @type {Record<string, { jobId: string, startedAt: number, freshChat: boolean }>} */
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
  settlingProviders[provider] = true;
  try {
    await extensionLog(`Fresh-chat URL → ${provider}: ${url}`);
    await browser.tabs.update(tabId, { url, active: false });
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

/**
 * Timeout check for long-running jobs (per provider).
 */
async function checkTimeouts() {
  const now = Date.now();
  for (const [provider, info] of Object.entries(activeJobs)) {
    if (now - info.startedAt <= 230000) continue;
    const timedOutId = info.jobId;
    await extensionLog(
      `Job ${timedOutId.slice(0, 8)} (${provider}) exceeded extension timeout (230s)`,
      "error"
    );
    try {
      const tabId = targetTabs[provider];
      if (Number.isInteger(tabId)) {
        await browser.tabs.sendMessage(tabId, { action: "stopAutomation" }).catch(() => {});
      }
    } catch (_) {}
    await postResult(timedOutId, "error", "Extension-side timeout (230s) waiting for AI response");
    clearJob(provider, timedOutId);
  }
}

/**
 * Pull up to one job per free provider so DeepSeek + Gemini (etc.) run in parallel.
 * Proxy already serializes per provider via current_jobs; extension must not
 * use a single global busy flag.
 */
async function doPoll() {
  if (pollInFlight) return schedule(400);
  pollInFlight = true;
  try {
    await checkTimeouts();
    const online = await pullConfig();
    await reportTabs();
    if (!online) return schedule(2500);

    // Pull as many free-provider jobs as the proxy can give (max 4 providers)
    let pulled = 0;
    const maxPull = 4;
    while (pulled < maxPull) {
      const response = await fetch(PROXY + "/job", { cache: "no-store" });
      const job = await response.json();
      if (!job || job.action !== "ask" || !job.jobId) break;

      const provider = job.provider || null;
      if (!provider) {
        await postResult(job.jobId, "error", "Job missing provider");
        continue;
      }
      // Proxy should only dispatch free providers; double-check locally
      if (!isProviderFree(provider)) {
        // Race: mark complete as error and let client retry — should be rare
        await extensionLog(
          `Job ${job.jobId.slice(0, 8)} for busy provider ${provider} — rejecting`,
          "warn"
        );
        await postResult(job.jobId, "error", `Provider ${provider} is already running a job`);
        continue;
      }

      activeJobs[provider] = {
        jobId: job.jobId,
        startedAt: Date.now(),
        freshChat: !!job.freshChat,
      };
      pulled += 1;
      // Fire and forget — do not await so other providers can start immediately
      executeJob(job).catch(async (err) => {
        await extensionLog(`executeJob crash: ${err.message || err}`, "error");
        await postResult(job.jobId, "error", err.message || String(err));
        clearJob(provider, job.jobId);
      });
    }

    // Faster poll while work is running so free providers pick up new jobs quickly
    schedule(anyBusy() ? 600 : 1000);
  } catch (error) {
    await extensionLog(`Polling failed: ${error.message || error}`, "error");
    schedule(2500);
  } finally {
    pollInFlight = false;
  }
}

async function executeJob(job) {
  const { jobId, question, provider, model } = job;
  const tabId = targetTabs[provider];
  if (!Number.isInteger(tabId)) {
    await postResult(jobId, "error", `No selected ${provider} tab. Select a ${provider} tab in the admin panel.`);
    clearJob(provider, jobId);
    return;
  }
  const tab = (await listAiTabs()).find((item) => item.id === tabId);
  if (!tab || tab.provider !== provider) {
    await postResult(jobId, "error", `Selected tab ${tabId} is not an available ${provider} tab.`);
    clearJob(provider, jobId);
    return;
  }
  try {
    await extensionLog(
      `Dispatch ${jobId.slice(0, 8)} → ${provider}/${model}, tab ${tabId} [parallel active: ${Object.keys(activeJobs).join(",")}]`
    );
    // Do NOT force active:true — parallel jobs must not steal focus from each other.
    // Messaging works on background tabs in Firefox.
    try {
      await browser.tabs.update(tabId, { active: false }).catch(() => {});
    } catch (_) {}
    await waitTabComplete(tabId, 15000);
    await injectScripts(tabId);
    const ready = await waitForComposerReady(tabId, 20000);
    if (!ready) {
      await extensionLog("Composer still missing — attempting one more inject", "warn");
      await delay(800);
      await injectScripts(tabId);
    }
    await delay(200);

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
    // Slot stays occupied until automationResult / automationError for this jobId
  } catch (error) {
    await extensionLog(`Dispatch failed (${provider}): ${error.message || error}`, "error");
    await postResult(jobId, "error", error.message || String(error));
    clearJob(provider, jobId);
  }
}

browser.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message.action === "automationResult") {
    (async () => {
      try {
        const provider =
          message.provider ||
          Object.keys(activeJobs).find((p) => activeJobs[p].jobId === message.jobId) ||
          null;
        await extensionLog(
          `Response received for ${message.jobId.slice(0, 8)} (${(message.result || "").length} chars) provider=${provider || "?"}`
        );
        await postResult(message.jobId, "result", message.result || "");

        const info = provider ? activeJobs[provider] : null;
        const doFresh = !!(info && info.freshChat && info.jobId === message.jobId);
        const tabId = provider ? targetTabs[provider] : null;

        // Release this provider immediately so the next job for it can start;
        // fresh-chat only blocks THIS provider, not others.
        if (provider) clearJob(provider, message.jobId);

        if (doFresh && provider && Number.isInteger(tabId)) {
          try {
            await navigateToNewChat(provider, tabId);
          } catch (e) {
            await extensionLog(`fresh-chat after result: ${e.message || e}`, "warn");
          }
        }
      } catch (e) {
        await extensionLog(`automationResult handler: ${e.message || e}`, "error");
        const provider = message.provider;
        if (provider) clearJob(provider, message.jobId);
      }
      sendResponse({ ok: true });
    })();
    return true;
  }
  if (message.action === "automationError") {
    (async () => {
      await extensionLog(`Automation error: ${message.error}`, "error");
      await postResult(message.jobId, "error", message.error || "Automation failed");
      const provider =
        message.provider ||
        Object.keys(activeJobs).find((p) => activeJobs[p].jobId === message.jobId);
      if (provider) clearJob(provider, message.jobId);
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
console.log("[NemApi] Background started (parallel per-provider)");
doPoll();
