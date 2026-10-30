/**
 * NemApi – Gemini adapter
 * Strips UI labels including markdown headings: "### Gemini a dit"
 */
(function (global) {
  "use strict";
  const B = global.NemApiBase;

  const INPUT = [
    // Live DOM (Vineetpandey0/Context-Sync, injectors/gemini.js):
    //   <rich-textarea class="ql-container ql-bubble">
    //     <div class="ql-editor ql-blank textarea new-input-ui"
    //          contenteditable="true" role="textbox"
    //          aria-label="Enter a prompt for Gemini">
    // The editable root is the `.ql-editor`; the container is the
    // `rich-textarea` itself (it carries the `ql-container` class).
    // ⚠️ `.ql-clipboard` is ALSO contenteditable — it must never be written.
    "rich-textarea div.ql-editor[contenteditable='true']",
    "div.ql-editor[contenteditable='true']",
    "div.ql-editor.textarea",
    "div.ql-editor",
    'rich-textarea div.ql-editor',
    '[contenteditable="true"][role="textbox"]:not(.ql-clipboard)',
    '[aria-label*="prompt" i][contenteditable="true"]:not(.ql-clipboard)',
  ];

  const SEND = [
    // the send control enables itself when the prompt is really there
    'button[aria-label="Send message"]',
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

  // Image upload (lumingya/universal-web-api, config/sites/gemini.google.com
  // ; LangQueue, src/content/adapters/gemini.ts):
  //   button[aria-label="Upload & tools"][aria-haspopup="menu"]
  //   images-files-uploader > input.hidden-file-input[type="file"]
  // The input lives INSIDE the opened menu: it is mounted only once the menu
  // is open, which is why the attach button is clicked first.
  const FILE_INPUT = [
    "images-files-uploader input.hidden-file-input[type='file']",
    "input.hidden-file-input[type='file']",
    "input[type='file'][accept='image/*']:not([capture])",
    "input[type='file']:not([capture])",
  ];
  const ATTACH = ['button[aria-label="Upload & tools"]', 'button[aria-label="Open upload file menu"]'];
  const DROP_TARGET = ["rich-textarea", ".input-area"];

  async function attachFiles(files) {
    return B.attachFilesToPage(files, {
      inputs: FILE_INPUT,
      openAttach: ATTACH,
      pasteTarget: INPUT,
      dropTarget: DROP_TARGET,
      name: "Gemini",
    });
  }

  function log(msg) {
    try {
      console.log("[NemApi/gemini] " + msg);
    } catch (_) {}
  }

  function findInput() {
    return B.queryFirst(INPUT);
  }

  /** The editable surfaces of the composer, best candidate first. */
  function inputCandidates() {
    return B.inputCandidatesFrom(INPUT);
  }

  /**
   * The send control, anchored on the composer we just wrote into.
   */
  function findSend(input) {
    const root = B.findComposerRoot(input);
    for (const s of SEND) {
      const nodes = root.querySelectorAll(s);
      for (const el of nodes) {
        if (!el || !B.isVisible(el)) continue;
        // the DISABLED state is NOT filtered here: the caller is the one
        // waiting for the button to become usable, and filtering it means
        // never finding it at all.
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
    if (B.restoreMath) B.restoreMath(clone);
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

  /**
   * Cheap read of the current answer — what the polling loops use.
   *
   * `readModelText` clones the bubble, restores math, strips the action bar and
   * runs htmlToMarkdown: O(answer). Running it on every 400 ms tick of a wait
   * kept the tab's main thread busy for the whole answer (the page shares it
   * with us) and grew worse as the answer got longer. The poll only needs to
   * know whether the text moved.
   */
  function readCheapText(el) {
    const root = el || getLastModelResponseEl();
    return B.cheapText ? B.cheapText(root) : "";
  }

  /** Cheap "is something moving?" probe (bubble count + length of the last one). */
  function getActivitySignature() {
    try {
      const last = getLastModelResponseEl();
      return getResponseCount() + ":" + (last ? readCheapText(last).length : 0);
    } catch (_) {
      return "0:0";
    }
  }

  /**
   * Is the model still streaming?
   *
   * Strict and local on purpose: a false positive is not free (the proxy
   * only accepts a stable answer once the provider is idle). The page-wide
   * `[aria-busy="true"]` match hits any busy widget of the page and kept it
   * "busy" for good.
   */
  function isGenerating() {
    const stop =
      document.querySelector('button[aria-label*="Stop" i]') ||
      document.querySelector('button[aria-label*="Arrêter" i]');
    if (stop && B.isVisible(stop)) return true;
    // a busy flag INSIDE the last answer only
    const last = getLastModelResponseEl();
    if (last) {
      const busy = last.querySelector('[aria-busy="true"]');
      if (busy && B.isVisible(busy)) return true;
    }
    return false;
  }

  async function sendPrompt(text) {
    const res = await B.sendWithProof({
      inputCandidates,
      findSend,
      text,
      log: (m) => log(m),
    });
    if (!res) {
      throw new Error(
        "Gemini: the prompt could not be written into the composer (the page keeps clearing it)"
      );
    }
    await B.clickSend({
      input: res.input,
      btn: res.btn,
      findSend,
      text,
      log: (m) => log(m),
      // Gemini turns the send button into a "Stop response" button the
      // instant the message leaves — a far more reliable "it was sent"
      // signal than the composer text clearing, which can lag.
      sentCheck: () =>
        !!document.querySelector(
          'button[aria-label="Stop response"], button[aria-label*="Stop" i]'
        ),
    });
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
    // No leading sleep: the bubble count gate already decides whether the
    // answer is this turn's, and the loop must see the tokens as they land.
    await B.waitUntilStable(
      () => {
        if (prevCount && getResponseCount() <= prevCount) return "";
        const cheap = readCheapText();
        if (isGenerating() && !cheap) return "";
        return cheap;
      },
      { timeout: 180000, stableMs: 1500, previous: "", getFinal: getLastResponse }
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
    attachFiles,
    waitForResponse,
    getLastResponse,
    getResponseCount,
    getActivitySignature,
    readCheapText,
    isGenerating,
    findInput,
  };
})(typeof window !== "undefined" ? window : self);
