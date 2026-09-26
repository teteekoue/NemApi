/**
 * NemApi – ChatGPT adapter (chatgpt.com / chat.openai.com)
 * Primary: copy-turn-action-button clipboard intercept.
 * Fallback: assistant turn .markdown → htmlToMarkdown.
 */
(function (global) {
  "use strict";
  const B = global.NemApiBase;

  const INPUT = [
    "#prompt-textarea",
    'form[data-type="unified-composer"] #prompt-textarea[contenteditable="true"]',
    "#prompt-textarea.ProseMirror[contenteditable='true']",
    'div[contenteditable="true"][role="textbox"][aria-label*="Chat" i]',
    'div[contenteditable="true"][data-id="root"]',
    'div.ProseMirror[contenteditable="true"]',
    'textarea[aria-label*="Chat" i]',
    'textarea[placeholder*="Ask" i]',
  ];

  const SEND = [
    'button[data-testid="send-button"]',
    "#composer-submit-button",
    'button[aria-label="Send prompt"]',
    'button[aria-label*="Send prompt" i]',
    'button[aria-label*="Send" i]',
    'button[data-testid*="send" i]',
  ];

  const MESSAGE = [
    '[data-message-author-role="assistant"]',
    'section[data-turn="assistant"]',
    '[data-turn="assistant"]',
    ".agent-turn",
    'article[data-testid^="conversation-turn-"] [data-message-author-role="assistant"]',
  ];

  function findInput() {
    return B.queryFirst(INPUT);
  }

  function findSend() {
    const root = B.findComposerRoot ? B.findComposerRoot() : document;
    for (const s of SEND) {
      const nodes = root.querySelectorAll(s);
      for (const el of nodes) {
        if (!el || !B.isVisible(el)) continue;
        if (B.isDisabled && B.isDisabled(el)) continue;
        if (el.disabled || el.getAttribute("aria-disabled") === "true") continue;
        const label = `${el.getAttribute("aria-label") || ""} ${el.title || ""}`.toLowerCase();
        if (/stop|cancel|attach|upload|file|mic|voice|dictate/.test(label)) continue;
        return el;
      }
    }
    return B.findByText
      ? B.findByText(["button", "[role='button']"], [/send/i, /submit/i], root)
      : null;
  }

  function getMessageEls() {
    const out = [];
    const seen = new Set();

    // Prefer explicit assistant role nodes
    document.querySelectorAll('[data-message-author-role="assistant"]').forEach((el) => {
      if (!seen.has(el)) {
        seen.add(el);
        out.push(el);
      }
    });
    if (out.length) return out;

    document.querySelectorAll('section[data-turn="assistant"], [data-turn="assistant"], .agent-turn').forEach((el) => {
      if (seen.has(el)) return;
      // Skip pure user turns
      if (el.getAttribute("data-message-author-role") === "user") return;
      if (el.querySelector('[data-message-author-role="user"]') && !el.querySelector('[data-message-author-role="assistant"]'))
        return;
      seen.add(el);
      out.push(el);
    });
    if (out.length) return out;

    // Last resort: conversation turns that contain .markdown and a copy button
    document.querySelectorAll('[data-testid^="conversation-turn-"]').forEach((el) => {
      if (seen.has(el)) return;
      if (el.querySelector('[data-message-author-role="user"]') && !el.querySelector(".markdown, .agent-turn"))
        return;
      if (!el.querySelector(".markdown, pre, [data-message-author-role='assistant']")) return;
      seen.add(el);
      out.push(el);
    });
    return out;
  }

  function pickContentRoot(msgEl) {
    if (!msgEl) return null;
    const md =
      msgEl.querySelector(
        '.markdown, .prose, [class*="markdown"], [data-message-author-role="assistant"]'
      ) || msgEl;
    return md;
  }

  function cleanResponse(text) {
    let t = String(text || "");
    // Convert residual HTML (clipboard text/html or bad intercept) to markdown
    if (/<\/?(p|div|strong|em|br|ul|ol|li|h[1-6])\b/i.test(t) && t.includes("<")) {
      try {
        const doc = new DOMParser().parseFromString(t, "text/html");
        if (B.htmlToMarkdown && doc.body) {
          const md = B.htmlToMarkdown(doc.body);
          if (md && md.length > 5) t = md;
        }
      } catch (_) {}
      t = t
        .replace(/<br\s*\/?>/gi, "\n")
        .replace(/<\/p>/gi, "\n\n")
        .replace(/<(strong|b)[^>]*>/gi, "**")
        .replace(/<\/(strong|b)>/gi, "**")
        .replace(/<(em|i)[^>]*>/gi, "*")
        .replace(/<\/(em|i)>/gi, "*")
        .replace(/<[^>]+>/g, "")
        .replace(/&nbsp;/g, " ")
        .replace(/&amp;/g, "&")
        .replace(/&lt;/g, "<")
        .replace(/&gt;/g, ">");
    }
    t = t
      .replace(/ChatGPT\s*(said|says)?\s*[:：]?\s*/gi, "")
      .replace(/^\s*Copy\s*(code)?\s*$/gim, "")
      .replace(/^\s*4o\s*mini\s*$/gim, "")
      .replace(/\n{3,}/g, "\n\n")
      .trim();
    if (B.cleanMarkdownCodeFences) t = B.cleanMarkdownCodeFences(t);
    return t;
  }

  function readMessageText(msgEl) {
    if (!msgEl) return "";
    const root = pickContentRoot(msgEl);
    // Prefer structured markdown conversion of the rendered DOM (never raw HTML)
    const clone = root.cloneNode(true);
    clone
      .querySelectorAll(
        "button,[role='button'],svg,nav,[class*='action'],[class*='toolbar'],[data-testid*='copy']"
      )
      .forEach((n) => n.remove());
    let md = B.htmlToMarkdown ? B.htmlToMarkdown(clone) : "";
    if (!md || md.length < 5) {
      if (B.extractReactMarkdown) {
        const react = B.extractReactMarkdown(root, ["markdown", "content", "text"]);
        if (react && react.length > 20 && !/<\/?[a-z][\s\S]*>/i.test(react)) md = react;
      }
    }
    if (!md || md.length < 5) md = root.innerText || root.textContent || "";
    return cleanResponse(md);
  }

  function findCopyButton(root) {
    if (!root) root = document;
    const sels = [
      'button[data-testid="copy-turn-action-button"]',
      'button[aria-label*="Copy response" i]',
      'button[aria-label*="Copy" i]',
      'button[data-testid*="copy" i]',
    ];
    for (const s of sels) {
      try {
        const nodes = root.querySelectorAll(s);
        for (let i = nodes.length - 1; i >= 0; i--) {
          const n = nodes[i];
          if (!B.isVisible(n)) continue;
          const label = (
            (n.getAttribute("aria-label") || "") +
            " " +
            (n.title || "") +
            " " +
            (n.innerText || "")
          ).toLowerCase();
          if (/regenerat|retry|like|dislike|share|edit|stop/.test(label)) continue;
          if (/copy|copier|复制/.test(label) || s.includes("copy")) return n;
        }
      } catch (_) {}
    }
    return B.findByText
      ? B.findByText(["button", "[role='button']"], [/copy/i, /copier/i], root)
      : null;
  }

  async function extractPremium(msgEl) {
    if (!msgEl) return "";
    const fromDom = readMessageText(msgEl);
    if (!B.premiumEnabled()) return fromDom;

    try {
      if (B.ensurePageClipboardHook) B.ensurePageClipboardHook();
    } catch (_) {}

    const turn =
      msgEl.closest('[data-testid^="conversation-turn-"], section[data-turn], [data-turn], .agent-turn') ||
      msgEl.parentElement ||
      msgEl;

    for (let attempt = 0; attempt < 3; attempt++) {
      const fromClip = await B.tryClipboardFromCopyButton(
        turn,
        [
          'button[data-testid="copy-turn-action-button"]',
          'button[aria-label*="Copy response" i]',
          'button[aria-label*="Copy" i]',
          'button[data-testid*="copy" i]',
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
      await B.sleep(180);
    }

    const react = B.extractReactMarkdown
      ? B.extractReactMarkdown(pickContentRoot(msgEl), ["markdown", "content", "text"])
      : "";
    if (react && react.length > Math.max(20, fromDom.length * 0.8)) return cleanResponse(react);

    return fromDom;
  }

  function getLastResponse() {
    const els = getMessageEls();
    if (els.length) return readMessageText(els[els.length - 1]);
    return "";
  }

  function isGenerating() {
    return !!(
      document.querySelector('.result-streaming[aria-busy="true"]') ||
      document.querySelector('[aria-busy="true"] .result-streaming') ||
      document.querySelector(".result-streaming") ||
      document.querySelector('button[aria-label*="Stop" i]') ||
      document.querySelector('button[data-testid="stop-button"]') ||
      document.querySelector('button[aria-label*="Stop generating" i]')
    );
  }

  async function sendPrompt(text) {
    let input = findInput();
    if (!input) {
      try {
        input = await B.waitFor("#prompt-textarea, [contenteditable='true']", 12000);
      } catch (_) {
        throw new Error("ChatGPT: input not found");
      }
    }
    try {
      input.focus();
      input.click();
    } catch (_) {}
    B.pasteText(input, text);
    await B.sleep(280);
    let btn = B.waitForEnabledSend
      ? await B.waitForEnabledSend(findSend, 4000)
      : findSend();
    if (btn && !(B.isDisabled && B.isDisabled(btn))) {
      try {
        btn.focus();
      } catch (_) {}
      if (B.clickEl(btn)) {
        await B.sleep(300);
        return;
      }
    }
    B.pressEnter(input);
    await B.sleep(200);
    btn = findSend();
    if (btn && !(B.isDisabled && B.isDisabled(btn))) B.clickEl(btn);
  }

  async function waitForResponse(previous = "") {
    await B.sleep(1200);
    // Wait until streaming ends and text stabilizes
    if (B.waitForNewResponse) {
      await B.waitForNewResponse(getMessageEls, readMessageText, {
        timeout: 180000,
        stableMs: 2800,
        previous,
        newElTimeout: 40000,
      });
    } else {
      await B.waitUntilStable(
        () => {
          if (isGenerating()) return "";
          return getLastResponse();
        },
        { timeout: 180000, stableMs: 2800, previous }
      );
    }

    // Extra: wait until Stop button gone
    for (let i = 0; i < 30; i++) {
      if (!isGenerating()) break;
      await B.sleep(300);
    }

    const els = getMessageEls();
    const last = els.length ? els[els.length - 1] : null;
    if (!last) return getLastResponse();

    // Wait for copy footer to mount
    for (let i = 0; i < 15; i++) {
      if (findCopyButton(last) || findCopyButton(last.parentElement)) break;
      await B.sleep(200);
    }
    await B.sleep(250);

    try {
      return await extractPremium(last);
    } catch (_) {
      return getLastResponse();
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

  global.NemApiProviders.chatgpt = {
    id: "chatgpt",
    match: (url) => /chatgpt\.com|chat\.openai\.com/i.test(url),
    sendPrompt,
    waitForResponse,
    getLastResponse,
    getResponseCount,
    isGenerating,
    findInput,
  };
})(typeof window !== "undefined" ? window : self);
