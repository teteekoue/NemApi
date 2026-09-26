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
      const nodes = root.querySelectorAll(s);
      for (const el of nodes) {
        if (!el || !B.isVisible(el)) continue;
        if (B.isDisabled && B.isDisabled(el)) continue;
        if (el.disabled || el.getAttribute("aria-disabled") === "true") continue;
        const label = `${el.getAttribute("aria-label") || ""} ${el.title || ""}`.toLowerCase();
        if (/stop|cancel|attach|upload|file|mic|voice/.test(label)) continue;
        return el;
      }
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
    if (!last) return null;
    return (
      last.querySelector(
        ".markdown.markdown-main-panel, .markdown, .response-content, message-content, .model-response-text"
      ) || last
    );
  }

  /** Last <model-response> element only — never a user-query. */
  function getLastModelResponseEl() {
    try {
      const nodes = document.querySelectorAll("model-response");
      if (nodes.length) return nodes[nodes.length - 1];
    } catch (_) {}
    for (const s of RESPONSE) {
      try {
        const nodes = document.querySelectorAll(s);
        if (!nodes.length) continue;
        // Skip anything that is inside a user-query
        for (let i = nodes.length - 1; i >= 0; i--) {
          const n = nodes[i];
          if (n.closest && n.closest("user-query, .user-query, [class*='user-query']")) continue;
          if (B.isUserMessageContext && B.isUserMessageContext(n) && !(B.isAssistantMessageContext && B.isAssistantMessageContext(n))) {
            continue;
          }
          return n;
        }
      } catch (_) {}
    }
    return null;
  }

  /**
   * Scope for the assistant action bar (copy / like / share).
   * Prefer the model-response itself, then a dedicated response container,
   * but NEVER a parent that also contains the user-query.
   */
  function getAssistantActionScope(modelEl) {
    if (!modelEl) return null;
    // 1) Inside the model-response
    if (modelEl.querySelector('button[aria-label*="Copy" i], button[aria-label*="Copier" i]')) {
      return modelEl;
    }
    // 2) Immediate response wrapper (sibling actions under same response turn)
    let p = modelEl.parentElement;
    for (let i = 0; i < 4 && p; i++) {
      // Abort if this ancestor also contains a user-query (would mix user+assistant copies)
      if (p.querySelector && p.querySelector("user-query, .user-query, [class*='user-query']")) {
        // Still allow if the only copy buttons are outside the user-query
        break;
      }
      if (p.querySelector('button[aria-label*="Copy" i], button[aria-label*="Copier" i]')) {
        return p;
      }
      p = p.parentElement;
    }
    // 3) Next sibling containers (Gemini sometimes puts the footer after model-response)
    let sib = modelEl.nextElementSibling;
    for (let i = 0; i < 3 && sib; i++) {
      if (sib.matches && (sib.matches("user-query, .user-query") || /user-query/i.test(sib.className || ""))) {
        sib = sib.nextElementSibling;
        continue;
      }
      if (sib.querySelector && sib.querySelector('button[aria-label*="Copy" i], button[aria-label*="Copier" i]')) {
        return sib;
      }
      // Combine modelEl + sibling into a temporary search by returning parent of both
      if (sib.parentElement && !sib.parentElement.querySelector("user-query, .user-query")) {
        return sib.parentElement;
      }
      sib = sib.nextElementSibling;
    }
    return modelEl;
  }

  /**
   * Read the FULL assistant answer from a model-response element.
   * Aggregates every markdown / prose block — not only the first code panel.
   */
  function readModelText(modelEl) {
    if (!modelEl) return "";

    // Clone and strip action-bar / chrome so plain text is clean
    const clone = modelEl.cloneNode(true);
    try {
      clone
        .querySelectorAll(
          "button, [role='button'], svg, [class*='action'], [class*='toolbar'], " +
            "[class*='footer'], [class*='reaction'], [class*='thumb'], " +
            "[data-test-id*='copy'], [aria-label*='Copy' i], [aria-label*='Copier' i]"
        )
        .forEach((n) => n.remove());
    } catch (_) {}

    let t = "";
    // Prefer structured MD conversion of the whole bubble
    if (B.htmlToMarkdown) {
      t = B.htmlToMarkdown(clone) || "";
    }
    // Fallback: concatenate all markdown panels then plain text
    if (!t || t.length < 15) {
      const panels = clone.querySelectorAll(
        ".markdown.markdown-main-panel, .markdown, .response-content, message-content, .model-response-text, .prose"
      );
      if (panels.length) {
        const parts = [];
        panels.forEach((p) => {
          const piece = B.htmlToMarkdown
            ? B.htmlToMarkdown(p)
            : p.innerText || p.textContent || "";
          if (piece && piece.trim()) parts.push(piece.trim());
        });
        if (parts.length) t = parts.join("\n\n");
      }
    }
    if (!t || t.length < 10) {
      t = clone.innerText || clone.textContent || "";
    }

    t = String(t)
      .replace(/^\s*(Copy|Copier|Share|Like|Dislike|Good response|Bad response|Exporter|Écouter|Show more|Show less)\s*$/gim, "")
      .trim();
    return cleanResponse(t);
  }

  function getLastResponse() {
    const last = getLastModelResponseEl();
    if (!last) return "";
    return readModelText(last);
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
    try {
      input.focus();
      input.click();
    } catch (_) {}
    B.pasteText(input, text);
    await B.sleep(220);
    let btn = B.waitForEnabledSend
      ? await B.waitForEnabledSend(findSend, 3500)
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
    const ed = document.querySelector(".ql-editor") || input;
    B.pressEnter(ed);
    await B.sleep(200);
    btn = findSend();
    if (btn && !(B.isDisabled && B.isDisabled(btn))) B.clickEl(btn);
  }

  /**
   * Fast full-answer extraction for Gemini.
   * Priority: React → full DOM (entire model-response) → clipboard only if DOM is weak.
   * Clipboard is optional; Gemini's DOM already holds the complete answer.
   */
  async function extractPremium() {
    const modelEl = getLastModelResponseEl();
    if (!modelEl) return "";

    // 1) React fiber on content root(s)
    if (B.extractReactMarkdown) {
      const targets = [];
      const main = contentRoot(modelEl);
      if (main) targets.push(main);
      modelEl
        .querySelectorAll(".markdown, .response-content, message-content, .model-response-text")
        .forEach((n) => targets.push(n));
      targets.push(modelEl);
      let best = "";
      for (const t of targets) {
        try {
          const react = B.extractReactMarkdown(t, [
            "markdown",
            "content",
            "text",
            "rawContent",
            "source",
          ]);
          if (react && react.length > best.length) best = react;
        } catch (_) {}
      }
      if (best.length > 20) return cleanResponse(best);
    }

    // 2) Full DOM of the model-response (prose + code) — preferred path
    const fromDom = readModelText(modelEl);
    if (fromDom && fromDom.length > 15) return fromDom;

    // 3) Clipboard only as last resort (slow / flaky on Gemini)
    if (B.premiumEnabled()) {
      const actionScope = getAssistantActionScope(modelEl) || modelEl;
      try {
        const fromClip = await B.tryClipboardFromCopyButton(
          actionScope,
          [
            'button[aria-label*="Copy response" i]',
            'button[aria-label*="Copy" i]',
            'button[aria-label*="Copier" i]',
          ],
          { domAssistantText: fromDom }
        );
        if (fromClip && fromClip.length > 10) {
          let userText = "";
          try {
            const uq = document.querySelectorAll("user-query, .user-query");
            if (uq.length) userText = (uq[uq.length - 1].innerText || "").trim();
          } catch (_) {}
          if (!userText || fromClip.trim() !== userText) {
            return cleanResponse(fromClip);
          }
        }
      } catch (_) {}
    }

    return fromDom || "";
  }

  function getResponseCount() {
    // Count model-response bubbles — a NEW bubble is the reliable signal that
    // a fresh answer exists, even when its text equals the previous answer.
    let n = 0;
    try { n = document.querySelectorAll("model-response").length; } catch (_) {}
    return n;
  }

  async function waitForResponse(previous = "", prevCount = 0) {
    await B.sleep(500);
    await B.waitUntilStable(
      () => {
        if (prevCount && getResponseCount() <= prevCount) return "";
        if (isGenerating() && !getLastResponse()) return "";
        return getLastResponse();
      },
      { timeout: 180000, stableMs: 1500, previous: "" }
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
    getResponseCount,
    isGenerating,
    findInput,
  };
})(typeof window !== "undefined" ? window : self);
