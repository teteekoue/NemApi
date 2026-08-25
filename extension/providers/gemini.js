/**
 * NemApi – Gemini adapter
 * Strips UI labels including markdown headings: "### Gemini a dit"
 */
(function (global) {
  "use strict";
  const B = global.NemApiBase;

  const INPUT = [
    "div.ql-editor.textarea",
    "div.ql-editor[contenteditable='true']",
    "div.ql-editor",
    ".ql-container",
    'rich-textarea [contenteditable="true"]',
    '[contenteditable="true"][role="textbox"]',
    '[aria-label*="prompt" i][contenteditable="true"]',
  ];

  const SEND = [
    'button[aria-label*="Send message" i]',
    'button[aria-label*="Send" i]',
    'button[aria-label*="送信"]',
    "button.send-button",
    'button[data-test-id="send-button"]',
    '[role="button"][aria-label*="Send" i]',
  ];

  const RESPONSE = [
    "model-response",
    "message-content.model-response-text",
    ".model-response-text",
    "div.response-content",
    ".markdown.markdown-main-panel",
  ];

  function findInput() {
    return B.queryFirst(INPUT);
  }

  function findSend() {
    const root = B.findComposerRoot();
    for (const s of SEND) {
      const el = root.querySelector(s);
      if (el && !el.disabled && el.getAttribute("aria-disabled") !== "true" && B.isVisible(el)) return el;
    }
    return B.findByText(["button", "[role='button']"], [/send/i, /submit/i, /generate/i], root);
  }

  /**
   * Remove Gemini speaker labels in plain text AND markdown heading form, e.g.:
   *   Gemini a dit :
   *   ### Gemini a dit
   *   ## Gemini said
   */
  function cleanResponse(text) {
    let t = String(text || "");

    // Markdown heading variants: # / ## / ### Gemini a dit
    const mdHeading =
      /^\s{0,3}#{1,6}\s*Gemini\s*((a\s+dit)|(said)|(says)|(a\s+déclaré)|(répond))?\s*[:：]?\s*\n*/gim;
    t = t.replace(mdHeading, "");
    t = t.replace(/^\s{0,3}#{1,6}\s*Gemini\s*((a\s+dit)|(said)|(says))?\s*[:：]?\s*/i, "");

    const prefixes = [
      /^\s*Gemini\s+a\s+dit\s*[:：]?\s*/gi,
      /^\s*Gemini\s+said\s*[:：]?\s*/gi,
      /^\s*Gemini\s+says\s*[:：]?\s*/gi,
      /^\s*Gemini\s+a\s+déclaré\s*[:：]?\s*/gi,
      /^\s*Gemini\s+répond\s*[:：]?\s*/gi,
      /^\s*Le\s+modèle\s+a\s+dit\s*[:：]?\s*/gi,
    ];
    for (let i = 0; i < 4; i++) {
      let changed = false;
      for (const re of prefixes) {
        const next = t.replace(re, "");
        if (next !== t) {
          t = next;
          changed = true;
        }
      }
      t = t.replace(mdHeading, "");
      if (!changed) break;
    }

    t = t.replace(/^\s*Gemini\s*(a\s+dit|said|says)?\s*[:：]?\s*\n+/i, "");
    t = t.replace(/^\s{0,3}#{1,6}\s*Gemini\s*$/gim, "");
    return t.trim();
  }

  function contentRoot(last) {
    return (
      last.querySelector(
        ".markdown.markdown-main-panel, .markdown, .response-content, message-content, .model-response-text"
      ) || last
    );
  }

  function getLastResponse() {
    for (const s of RESPONSE) {
      const nodes = document.querySelectorAll(s);
      if (!nodes.length) continue;
      const last = nodes[nodes.length - 1];
      const target = contentRoot(last);
      let t = "";
      if (B.premiumEnabled()) {
        t = B.htmlToMarkdown(target) || "";
      }
      if (!t) t = target.innerText || target.textContent || "";
      t = cleanResponse(t);
      if (t.length > 2) return t;
    }
    return "";
  }

  function isGenerating() {
    return !!(
      document.querySelector('[aria-busy="true"]') ||
      document.querySelector('button[aria-label*="Stop" i]')
    );
  }

  async function sendPrompt(text) {
    let input = findInput();
    if (!input) {
      try {
        input = await B.waitFor("div.ql-editor, .ql-container", 12000);
      } catch (_) {
        throw new Error("Gemini: input not found");
      }
    }
    B.pasteText(input, text);
    await B.sleep(180);
    const btn = findSend();
    if (btn) {
      B.clickEl(btn);
    } else {
      const ed = document.querySelector(".ql-editor") || input;
      B.pressEnter(ed);
    }
  }

  async function extractPremium() {
    for (const s of RESPONSE) {
      const nodes = document.querySelectorAll(s);
      if (!nodes.length) continue;
      const last = nodes[nodes.length - 1];
      const target = contentRoot(last);

      if (B.premiumEnabled()) {
        const react = B.extractReactMarkdown(target);
        if (react && react.length > 20) return cleanResponse(react);

        const fromClip = await B.tryClipboardFromCopyButton(last.parentElement || last, [
          'button[aria-label*="Copy" i]',
          'button[aria-label*="Copier" i]',
        ]);
        if (fromClip && fromClip.length > 10) return cleanResponse(fromClip);

        const htmlMd = B.htmlToMarkdown(target);
        if (htmlMd) return cleanResponse(htmlMd);
      }
    }
    return getLastResponse();
  }

  async function waitForResponse(previous = "") {
    await B.sleep(1000);
    await B.waitUntilStable(
      () => {
        if (isGenerating() && !getLastResponse()) return "";
        return getLastResponse();
      },
      { timeout: 180000, stableMs: 2200, previous }
    );
    try {
      return await extractPremium();
    } catch (_) {
      return getLastResponse();
    }
  }

  global.NemApiProviders = global.NemApiProviders || {};
  global.NemApiProviders.gemini = {
    id: "gemini",
    match: (url) => /gemini\.google\.com/i.test(url),
    sendPrompt,
    waitForResponse,
    getLastResponse,
    isGenerating,
    findInput,
  };
})(typeof window !== "undefined" ? window : self);
