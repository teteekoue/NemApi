/**
 * NemApi – Kimi adapter (kimi.com / kimi.ai / www.kimi.ai)
 * Input: Lexical contenteditable (.chat-input-editor) — paste via execCommand.
 * Send: .send-button-container / aria-label 发送 / Enter.
 * Extract: Copy button → clipboard, else .markdown DOM.
 */
(function (global) {
  "use strict";
  const B = global.NemApiBase;

  const INPUT = [
    '.chat-input-editor[contenteditable="true"]',
    '.chat-input-editor[data-lexical-editor="true"]',
    '[contenteditable="true"][data-lexical-editor="true"][role="textbox"]',
    '[contenteditable="true"][data-lexical-editor="true"]',
    ".chat-input-editor",
    '[contenteditable="true"][role="textbox"]',
    'div[contenteditable="true"]',
    "textarea",
  ];

  const SEND = [
    ".send-button-container button",
    ".send-button-container",
    '[class*="send-button-container"] button',
    '[class*="send-btn"]',
    '[class*="send-button"]',
    'button[aria-label*="Send" i]',
    'button[aria-label*="发送"]',
    'button[type="submit"]',
    "button.send-button",
  ];

  const MESSAGE = [
    ".chat-content-item-assistant",
    "[class*='chat-content-item-assistant']",
    "[class*='segment-assistant']",
    "[class*='assistant-content']",
    ".markdown:not(.user-content)",
    "[class*='assistant'] .markdown",
  ];

  function findInput() {
    return B.queryFirst(INPUT);
  }

  function findSend() {
    const root = B.findComposerRoot ? B.findComposerRoot() : document;
    for (const s of SEND) {
      try {
        const nodes = root.querySelectorAll(s);
        for (const el of nodes) {
          if (!el || !B.isVisible(el)) continue;
          if (B.isDisabled && B.isDisabled(el)) continue;
          if (el.disabled || el.getAttribute("aria-disabled") === "true") continue;
          const label = `${el.getAttribute("aria-label") || ""} ${el.title || ""} ${el.className || ""}`.toLowerCase();
          if (/stop|cancel|attach|upload|file|mic|voice|toolkit/.test(label)) continue;
          // Prefer actual button inside container
          if (el.tagName !== "BUTTON" && el.getAttribute("role") !== "button") {
            const inner = el.querySelector("button, [role='button']");
            if (inner && B.isVisible(inner) && !(B.isDisabled && B.isDisabled(inner))) return inner;
          }
          return el;
        }
      } catch (_) {}
    }
    return B.findByText
      ? B.findByText(["button", "[role='button']"], [/send/i, /发送/, /submit/i], root)
      : null;
  }

  function getMessageEls() {
    for (const s of MESSAGE) {
      try {
        const nodes = document.querySelectorAll(s);
        if (!nodes.length) continue;
        const arr = Array.from(nodes).filter((el) => {
          if (el.closest("[class*='user-content'], [class*='chat-content-item-user'], [class*='segment-user']"))
            return false;
          return true;
        });
        if (arr.length) return arr;
      } catch (_) {}
    }
    const mds = document.querySelectorAll(".markdown, [class*='markdown']");
    const out = [];
    mds.forEach((el) => {
      if (el.closest("[class*='user-content'], [class*='chat-content-item-user']")) return;
      out.push(el);
    });
    return out;
  }

  function pickContentRoot(msgEl) {
    if (!msgEl) return null;
    return msgEl.querySelector(".markdown, [class*='markdown'], .paragraph") || msgEl;
  }

  function cleanResponse(text) {
    let t = String(text || "")
      .replace(/Kimi\s*[:：]?\s*/gi, "")
      .replace(/^\s*(Copy|复制|Share|分享)\s*$/gim, "")
      .trim();
    if (B.cleanMarkdownCodeFences) t = B.cleanMarkdownCodeFences(t);
    return t;
  }

  function readMessageText(msgEl) {
    if (!msgEl) return "";
    const root = pickContentRoot(msgEl);
    if (B.extractReactMarkdown) {
      const react = B.extractReactMarkdown(root, ["markdown", "content", "text", "source"]);
      if (react && react.length > 20) return cleanResponse(react);
    }
    const clone = root.cloneNode(true);
    clone
      .querySelectorAll(
        "button,[role='button'],svg,[class*='action'],[class*='toolbar'],[class*='copy']"
      )
      .forEach((n) => n.remove());
    let md = B.htmlToMarkdown ? B.htmlToMarkdown(clone) : "";
    if (!md || md.length < 5) md = root.innerText || root.textContent || "";
    return cleanResponse(md);
  }

  /**
   * Fast path for Kimi: React → DOM first (sync). Clipboard is optional and
   * used only once when DOM is empty/weak — avoids 3× slow copy clicks.
   */
  async function extractPremium(msgEl) {
    if (!msgEl) return "";

    // 1) React fiber (sync)
    const root = pickContentRoot(msgEl) || msgEl;
    if (B.extractReactMarkdown) {
      const react = B.extractReactMarkdown(root, ["markdown", "content", "text", "source"]);
      if (react && react.length > 20) return cleanResponse(react);
    }

    // 2) DOM htmlToMarkdown / text (sync) — preferred over clipboard
    const fromDom = readMessageText(msgEl);
    if (fromDom && fromDom.length > 15) return fromDom;

    // 3) Clipboard only if DOM failed — single attempt
    if (B.premiumEnabled()) {
      try {
        if (B.ensurePageClipboardHook) B.ensurePageClipboardHook();
      } catch (_) {}
      const copyRoot =
        msgEl.closest(
          "[class*='chat-content-item-assistant'], [class*='segment-assistant'], [class*='assistant']"
        ) || msgEl;
      try {
        const fromClip = await B.tryClipboardFromCopyButton(
          copyRoot,
          [
            'button[aria-label*="Copy" i]',
            'button[aria-label*="复制"]',
            'button[title*="Copy" i]',
            'button[title*="复制"]',
            "button[class*='copy']",
          ],
          { domAssistantText: fromDom }
        );
        if (
          fromClip &&
          fromClip.length > 10 &&
          fromClip !== "[object Object]" &&
          !/^\[object\s+\w+\]$/i.test(fromClip.trim())
        ) {
          return cleanResponse(fromClip);
        }
      } catch (_) {}
    }

    return fromDom || "";
  }

  function getLastResponse() {
    const els = getMessageEls();
    if (els.length) return readMessageText(els[els.length - 1]);
    return "";
  }

  function isGenerating() {
    return !!(
      document.querySelector('button[aria-label*="Stop" i]') ||
      document.querySelector('button[aria-label*="停止"]') ||
      document.querySelector('[class*="stop-generating"], [class*="streaming"], [class*="loading-dots"]')
    );
  }

  /**
   * Lexical/React contenteditable rejects many paste paths.
   * document.execCommand('insertText') is the reliable atomic write.
   */
  function fillLexicalEditor(el, text) {
    try {
      el.focus();
    } catch (_) {}
    try {
      const sel = window.getSelection();
      if (sel) {
        sel.selectAllChildren(el);
      }
    } catch (_) {}
    try {
      if (document.execCommand) {
        document.execCommand("selectAll", false, null);
        document.execCommand("insertText", false, text);
        return true;
      }
    } catch (_) {}
    // Fallback to shared paste helper
    try {
      B.pasteText(el, text);
      return true;
    } catch (_) {}
    return false;
  }

  function inputHasText(el, expected) {
    if (!el) return false;
    const sample = String(expected || "").slice(0, 40);
    if ("value" in el && el.tagName === "TEXTAREA") {
      const v = el.value || "";
      return sample ? v.includes(sample) : v.length > 0;
    }
    const t = el.innerText || el.textContent || "";
    return sample ? t.includes(sample) : t.trim().length > 0;
  }

  async function sendPrompt(text) {
    const payload = String(text ?? "");
    let input = findInput();
    if (!input) {
      try {
        input = await B.waitFor(
          '.chat-input-editor, [data-lexical-editor="true"], [contenteditable="true"]',
          15000
        );
      } catch (_) {
        throw new Error("Kimi: input not found");
      }
    }

    for (let i = 0; i < 20; i++) {
      if (B.isVisible(input) && input.getAttribute("contenteditable") !== "false") break;
      await B.sleep(150);
      input = findInput() || input;
    }

    let pasted = false;
    for (let attempt = 1; attempt <= 3; attempt++) {
      input = findInput() || input;
      fillLexicalEditor(input, payload);
      await B.sleep(400);
      if (inputHasText(input, payload)) {
        pasted = true;
        break;
      }
      await B.sleep(200);
    }
    if (!pasted) {
      try {
        B.pasteText(input, payload);
      } catch (_) {}
      await B.sleep(300);
    }

    let btn = B.waitForEnabledSend
      ? await B.waitForEnabledSend(findSend, 4000)
      : null;
    if (!btn) {
      for (let i = 0; i < 25; i++) {
        btn = findSend();
        if (btn && !(B.isDisabled && B.isDisabled(btn))) break;
        await B.sleep(120);
        btn = null;
      }
    }
    if (btn) {
      try {
        btn.focus();
      } catch (_) {}
      if (B.clickEl(btn)) {
        await B.sleep(250);
        return;
      }
    }
    // Enter submits on Kimi
    B.pressEnter(input);
    await B.sleep(120);
    B.pressEnter(input);
  }

  async function waitForResponse(previous = "", prevCount = 0) {
    await B.sleep(500);
    if (B.waitForNewResponse) {
      await B.waitForNewResponse(getMessageEls, readMessageText, {
        timeout: 180000,
        stableMs: 1500,
        previous: prevCount && getResponseCount() > prevCount ? "" : previous,
        newElTimeout: 25000,
      });
    } else {
      await B.waitUntilStable(
        () => {
          if (prevCount && getResponseCount() <= prevCount) return "";
          if (isGenerating() && !getLastResponse()) return "";
          return getLastResponse();
        },
        { timeout: 180000, stableMs: 1500, previous: "" }
      );
    }
    const els = getMessageEls();
    const last = els.length ? els[els.length - 1] : null;
    if (!last) return getLastResponse();
    await B.sleep(120);
    try {
      return await extractPremium(last);
    } catch (_) {
      return readMessageText(last);
    }
  }

  global.NemApiProviders = global.NemApiProviders || {};

  function getResponseCount() {
    try {
      return getMessageEls().length;
    } catch (_) {
      return 0;
    }
  }

  global.NemApiProviders.kimi = {
    id: "kimi",
    match: (url) => /kimi\.com|kimi\.ai|moonshot\.cn/i.test(url),
    sendPrompt,
    waitForResponse,
    getLastResponse,
    getResponseCount,
    isGenerating,
    findInput,
  };
})(typeof window !== "undefined" ? window : self);
