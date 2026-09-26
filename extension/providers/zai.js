/**
 * NemApi – Z.ai / Zhipu adapter (chat.z.ai)
 * Input: textarea#chat-input
 * Send: #send-message-button
 * Response: #response-content-container / assistant bubbles
 * Primary: Copy button clipboard; fallback DOM markdown.
 */
(function (global) {
  "use strict";
  const B = global.NemApiBase;

  const INPUT = [
    "textarea#chat-input",
    "#chat-input",
    'textarea[id="chat-input"]',
    'textarea[placeholder*="Ask" i]',
    'textarea[placeholder*="message" i]',
    'textarea[role="textbox"]',
    "textarea",
  ];

  const SEND = [
    "button#send-message-button",
    "#send-message-button",
    'button[id="send-message-button"]',
    'button[aria-label*="Send" i]',
    'button[type="submit"]',
    "button.send-button",
  ];

  const MESSAGE = [
    "#response-content-container",
    "[id='response-content-container']",
    "[class*='response-content']",
    "[class*='assistant-message']",
    "[class*='chat-message'][class*='assistant']",
    ".markdown-body",
    ".markdown",
  ];

  function findInput() {
    return B.queryFirst(INPUT);
  }

  function findSend() {
    const root = B.findComposerRoot();
    for (const s of SEND) {
      const nodes = root.querySelectorAll(s);
      for (const el of nodes) {
        if (!el || !B.isVisible(el)) continue;
        if (B.isDisabled && B.isDisabled(el)) continue;
        if (el.disabled || el.getAttribute("aria-disabled") === "true") continue;
        const label = `${el.getAttribute("aria-label") || ""} ${el.title || ""} ${el.id || ""}`.toLowerCase();
        if (/stop|cancel|attach|upload|file|mic|voice/.test(label)) continue;
        return el;
      }
    }
    return B.findByText(["button", "[role='button']"], [/send/i, /submit/i, /发送/], root);
  }

  function getMessageEls() {
    for (const s of MESSAGE) {
      try {
        const nodes = document.querySelectorAll(s);
        if (nodes.length) return Array.from(nodes);
      } catch (_) {}
    }
    return [];
  }

  function pickContentRoot(msgEl) {
    if (!msgEl) return null;
    return (
      msgEl.querySelector(".markdown-body, .markdown, [class*='markdown'], pre, .prose") || msgEl
    );
  }

  function cleanResponse(text) {
    let t = String(text || "")
      .replace(/GLM\s*[:：]?\s*/gi, "")
      .replace(/Z\.?ai\s*[:：]?\s*/gi, "")
      .replace(/^\s*(Copy|复制|Share)\s*$/gim, "")
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
      .querySelectorAll("button,[role='button'],svg,[class*='action'],[class*='toolbar']")
      .forEach((n) => n.remove());
    let md = B.htmlToMarkdown ? B.htmlToMarkdown(clone) : "";
    if (!md || md.length < 5) md = root.innerText || root.textContent || "";
    return cleanResponse(md);
  }

  /**
   * Prefer the full-message Copy control over per-code-block copies.
   * Z.ai / ChatGLM place the message actions outside the markdown body;
   * code blocks have their own small copy icons inside pre / code wrappers.
   */
  function findMessageCopyButton(msgEl) {
    if (!msgEl) return null;
    const root =
      msgEl.closest(
        "#response-content-container, [class*='response'], [class*='message'], [class*='chat'], [class*='assistant']"
      ) || msgEl;

    const candidates = [];
    const seen = new Set();
    const sels = [
      'button[aria-label*="Copy" i]',
      'button[aria-label*="复制"]',
      'button[title*="Copy" i]',
      'button[title*="复制"]',
      "button[class*='copy']",
      '[class*="copy"][role="button"]',
      "button[class*='icon']",
    ];
    for (const s of sels) {
      try {
        root.querySelectorAll(s).forEach((n) => {
          if (seen.has(n)) return;
          seen.add(n);
          if (!B.isVisible(n)) return;
          // Exclude code-block copies
          if (
            n.closest(
              "pre, code, [class*='code-block'], [class*='CodeBlock'], [class*='md-code'], [class*='hljs']"
            )
          ) {
            return;
          }
          const label = (
            (n.getAttribute("aria-label") || "") +
            " " +
            (n.title || "") +
            " " +
            (n.innerText || n.textContent || "")
          ).toLowerCase();
          if (
            /regenerat|retry|like|dislike|share|edit|stop|download|重新生成|点赞|分享/.test(
              label
            )
          ) {
            return;
          }
          const isCopy = /copy|copier|复制|clipboard/.test(label) || s.includes("copy");
          if (!isCopy) return;
          // Prefer buttons whose ancestors also contain other action controls
          let score = isCopy ? 20 : 0;
          let group = n.parentElement;
          for (let i = 0; i < 4 && group; i++) {
            const btns = group.querySelectorAll("button, [role='button']");
            if (btns.length >= 2) score += 15;
            if (
              /regenerat|retry|like|share|重新生成|分享|点赞/.test(
                (group.innerText || "").toLowerCase()
              )
            ) {
              score += 25;
            }
            group = group.parentElement;
          }
          candidates.push({ el: n, score });
        });
      } catch (_) {}
    }
    candidates.sort((a, b) => b.score - a.score);
    return candidates.length ? candidates[0].el : null;
  }

  async function extractPremium(msgEl) {
    if (!msgEl) return "";
    const fromDom = readMessageText(msgEl);
    if (!B.premiumEnabled()) return fromDom;

    try {
      if (B.ensurePageClipboardHook) B.ensurePageClipboardHook();
    } catch (_) {}

    const msgCopyBtn = findMessageCopyButton(msgEl);
    const copyRoot = msgCopyBtn
      ? msgCopyBtn.closest(
          "#response-content-container, [class*='message'], [class*='response'], [class*='chat'], [class*='assistant']"
        ) || msgEl
      : msgEl.closest("[class*='message'], [class*='response'], [class*='chat']") || msgEl;

    for (let attempt = 0; attempt < 3; attempt++) {
      // base.js skips user-message + code-block copies; validates against DOM
      const fromClip = await B.tryClipboardFromCopyButton(
        copyRoot,
        [
          'button[aria-label*="Copy" i]',
          'button[aria-label*="复制"]',
          'button[title*="Copy" i]',
          "button[class*='copy']",
          '[class*="copy"][role="button"]',
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
      await B.sleep(150);
    }

    const react = B.extractReactMarkdown
      ? B.extractReactMarkdown(pickContentRoot(msgEl), ["markdown", "content", "text"])
      : "";
    if (react && react.length > 20) return cleanResponse(react);

    return fromDom;
  }

  function getLastResponse() {
    const els = getMessageEls();
    if (els.length) return readMessageText(els[els.length - 1]);
    return "";
  }

  /** Bubble count — detect a new answer even when text is identical to previous. */
  function getResponseCount() {
    try {
      return getMessageEls().length;
    } catch (_) {
      return 0;
    }
  }

  function isGenerating() {
    return !!(
      document.querySelector('button[aria-label*="Stop" i]') ||
      document.querySelector('button[aria-label*="停止"]') ||
      document.querySelector("[class*='stop'], [class*='streaming'], [class*='generating']")
    );
  }

  async function sendPrompt(text) {
    let input = findInput();
    if (!input) {
      try {
        input = await B.waitFor("#chat-input, textarea", 12000);
      } catch (_) {
        throw new Error("Z.ai: input not found");
      }
    }
    try {
      input.focus();
      input.click();
    } catch (_) {}
    B.pasteText(input, text);
    await B.sleep(220);
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
    await B.sleep(150);
    btn = findSend();
    if (btn && !(B.isDisabled && B.isDisabled(btn))) B.clickEl(btn);
  }

  async function waitForResponse(previous = "", prevCount = 0) {
    await B.sleep(900);
    const skipPrevFilter = prevCount && getResponseCount() > prevCount;
    if (B.waitForNewResponse) {
      await B.waitForNewResponse(getMessageEls, readMessageText, {
        timeout: 180000,
        stableMs: 2400,
        previous: skipPrevFilter ? "" : previous,
        newElTimeout: 30000,
      });
    } else {
      await B.waitUntilStable(
        () => {
          if (prevCount && getResponseCount() <= prevCount) return "";
          if (isGenerating() && !getLastResponse()) return "";
          return getLastResponse();
        },
        { timeout: 180000, stableMs: 2400, previous: skipPrevFilter ? "" : previous }
      );
    }
    const els = getMessageEls();
    const last = els.length ? els[els.length - 1] : null;
    if (!last) return getLastResponse();
    await B.sleep(250);
    try {
      return await extractPremium(last);
    } catch (_) {
      return readMessageText(last);
    }
  }

  global.NemApiProviders = global.NemApiProviders || {};
  global.NemApiProviders.zai = {
    id: "zai",
    match: (url) => /chat\.z\.ai|z\.ai\/chat|chatglm\.cn|bigmodel\.cn/i.test(url),
    sendPrompt,
    waitForResponse,
    getLastResponse,
    getResponseCount,
    isGenerating,
    findInput,
  };
})(typeof window !== "undefined" ? window : self);
