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
    // Live DOM (ops120/kimi-brain, references/site-map.md, relevé 2026-09-12):
    //   div.chat-input-editor[data-lexical-editor="true"][role="textbox"]
    // The most specific selector first, the generic ones last (writing into
    // the wrong surface is silent — the text goes nowhere and the send
    // button stays disabled).
    'div.chat-input-editor[data-lexical-editor="true"][role="textbox"]',
    '.chat-input-editor[contenteditable="true"]',
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

  // Image upload: the file input is PERSISTENT in the DOM — live DOM capture
  // (tamga 2026-10-18): `<input class="hidden-input" type="file" multiple>`,
  // sitting in `div.chat-editor-action`, next to the tools button that opens
  // the upload menu (`.toolkit-trigger-btn`, data-testid="toolkit-trigger-btn").
  // The button is only a fallback: the input is already there.
  const FILE_INPUT = [
    ".chat-editor-action input.hidden-input[type='file']",
    "input.hidden-input[type='file']",
    "input[type='file'][multiple]:not([capture])",
    "input[type='file']:not([capture])",
  ];
  const ATTACH = [
    ".toolkit-trigger-btn",
    "[data-testid='toolkit-trigger-btn']",
  ];

  async function attachFiles(files) {
    return B.attachFilesToPage(files, {
      inputs: FILE_INPUT,
      openAttach: ATTACH,
      pasteTarget: INPUT,
      name: "Kimi",
    });
  }

  const MESSAGE = [
    ".chat-content-item-assistant",
    "[class*='chat-content-item-assistant']",
    "[class*='segment-assistant']",
    "[class*='assistant-content']",
    ".markdown:not(.user-content)",
    "[class*='assistant'] .markdown",
  ];

  function log(msg) {
    try {
      console.log("[NemApi/kimi] " + msg);
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
    const root = B.findComposerRoot ? B.findComposerRoot(input) : document;
    for (const s of SEND) {
      try {
        const nodes = root.querySelectorAll(s);
        for (const el of nodes) {
          if (!el || !B.isVisible(el)) continue;
          // the DISABLED state is NOT filtered here: the caller is the one
          // waiting for the button to become usable, and filtering it means
          // never finding it at all.
          const label = `${el.getAttribute("aria-label") || ""} ${el.title || ""} ${el.className || ""}`.toLowerCase();
          if (/stop|cancel|attach|upload|file|mic|voice|toolkit/.test(label)) continue;
          // Prefer actual button inside container
          if (el.tagName !== "BUTTON" && el.getAttribute("role") !== "button") {
            const inner = el.querySelector("button, [role='button']");
            if (inner && B.isVisible(inner)) return inner;
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

  /**
   * The answer block, without the reasoning.
   *
   * Live DOM (ops120/kimi-brain, site-map.md): a Kimi answer holds TWO
   * `.markdown-container` blocks — the first one is the "thinking"
   * (思考 / `toolcall-content-text`) and must be EXCLUDED, the second one is
   * the answer itself.
   */
  function pickContentRoot(msgEl) {
    if (!msgEl) return null;
    const blocks = Array.from(
      msgEl.querySelectorAll(
        ".markdown-container, .markdown, [class*='markdown'], .paragraph"
      )
    ).filter(
      (el) =>
        !el.closest(
          "[class*='toolcall-content'], [class*='thinking'], [class*='reason'], [class*='thought']"
        )
    );
    if (blocks.length) {
      // the answer is the LAST markdown block (the first one is the thinking)
      return blocks[blocks.length - 1];
    }
    return msgEl;
  }

  function cleanResponse(text) {
    let t = String(text || "")
      .replace(/^[ \t]*Kimi[ \t]*[:：]?[ \t]*$/gim, "")
      .replace(/^\s*(Copy|复制|Share|分享)\s*$/gim, "")
      .trim();
    if (B.cleanMarkdownCodeFences) t = B.cleanMarkdownCodeFences(t);
    return t;
  }

  /**
   * Cheap read of one answer — what the polling loops use.
   *
   * `readMessageText` clones the DOM and runs htmlToMarkdown (O(answer)); the
   * poll only needs "did the text move?". Polling the full rebuild also kept
   * the tab's main thread busy while an answer streamed.
   */
  function readCheapText(msgEl) {
    if (!msgEl) return "";
    try {
      return B.cheapText(pickContentRoot(msgEl) || msgEl);
    } catch (_) {
      return "";
    }
  }

  /** Cheap "is something moving?" probe (bubble count + length of the last one). */
  function getActivitySignature() {
    try {
      const els = getMessageEls();
      const last = els.length ? els[els.length - 1] : null;
      return els.length + ":" + (last ? readCheapText(last).length : 0);
    } catch (_) {
      return "0:0";
    }
  }

  function readMessageText(msgEl) {
    if (!msgEl) return "";
    const root = pickContentRoot(msgEl);
    if (B.extractReactMarkdown) {
      const react = B.extractReactMarkdown(root, ["markdown", "content", "text", "source"]);
      if (react && react.length > 20) return cleanResponse(react);
    }
    const clone = root.cloneNode(true);
    if (B.restoreMath) B.restoreMath(clone);
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

  /**
   * Is the model still streaming?
   *
   * Strict and local on purpose: a false positive is not free (the proxy
   * only accepts a stable answer once the provider is idle). The page-wide
   * `[class*="streaming"]` / `[class*="loading-dots"]` matches hit unrelated
   * elements and kept the page "busy" for good.
   */
  function isGenerating() {
    const stop =
      document.querySelector('button[aria-label*="Stop" i]') ||
      document.querySelector('button[aria-label*="停止"]');
    if (stop && B.isVisible(stop)) return true;
    // a streaming indicator INSIDE the last answer or the composer
    const els = getMessageEls();
    const scope = els.length ? els[els.length - 1] : findInput();
    if (scope && scope.querySelector) {
      const busy = scope.querySelector(
        '[class*="stop-generating"], [class*="streaming"], [class*="loading-dots"]'
      );
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
      // Kimi's send control is a `<div>` (not a `<button>`): it has no
      // `disabled` attribute, the state is carried by a `disabled` CLASS.
      sendDisabledCheck: (btn) =>
        !!(btn && btn.classList && btn.classList.contains("disabled")),
    });
    if (!res) {
      throw new Error(
        "Kimi: the prompt could not be written into the composer (the page keeps clearing it)"
      );
    }
    await B.clickSend({ input: res.input, btn: res.btn, findSend, text, log: (m) => log(m) });
  }

  async function waitForResponse(previous = "", prevCount = 0) {
    // No leading sleep: the loop must see the first tokens, and
    // waitForGenerationStart already confirmed that the answer started.
    if (B.waitForNewResponse) {
      await B.waitForNewResponse(getMessageEls, readMessageText, {
        timeout: 180000,
        stableMs: 1500,
        previous: prevCount && getResponseCount() > prevCount ? "" : previous,
        newElTimeout: 25000,
        readCheap: readCheapText,
      });
    } else {
      await B.waitUntilStable(
        () => {
          if (prevCount && getResponseCount() <= prevCount) return "";
          const cheap = readCheapText(lastOfList(getMessageEls()));
          if (isGenerating() && !cheap) return "";
          return cheap;
        },
        { timeout: 180000, stableMs: 1500, previous: "", getFinal: getLastResponse }
      );
    }
    const last = lastOfList(getMessageEls());
    if (!last) return getLastResponse();
    try {
      return await extractPremium(last);
    } catch (_) {
      return readMessageText(last);
    }
  }

  function lastOfList(list) {
    return list && list.length ? list[list.length - 1] : null;
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
