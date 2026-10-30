/**
 * NemApi – Claude adapter
 * Primary strategy: same path as the native Copy button (clipboard intercept).
 * Fallback: full turn DOM with every <pre><code> preserved.
 */
(function (global) {
  "use strict";
  const B = global.NemApiBase;

  const INPUT = [
    // Live DOM (timothy22000/claude-autocontinue, extension/content.js):
    //   div[contenteditable="true"][data-placeholder]  (a ProseMirror editor)
    // The most specific selector first, the generic ones last (writing into
    // the wrong surface is silent — the text goes nowhere and the send
    // button stays disabled).
    'div[contenteditable="true"][data-placeholder]',
    "div.ProseMirror[contenteditable='true']",
    "div[contenteditable='true'].ProseMirror",
    '[data-testid="chat-input"]',
    'div[contenteditable="true"][role="textbox"]',
    'div[contenteditable="true"]',
  ];

  const SEND = [
    'button[aria-label*="Send message" i]',
    'button[data-testid="send-button"]',
    'button[type="submit"]',
    'button[aria-label*="Send" i]',
    '[role="button"][aria-label*="Send" i]',
  ];

  // Image upload: the file input is PERSISTENT in the DOM (live DOM capture,
  // tamga 2026-10-11: `<input type="file" multiple hidden data-testid="file-upload">`).
  // `input[data-testid="file-upload"]` is the verified anchor; a bare
  // "input[type=file]" is ambiguous (the account/avatar menu has one too).
  // The "+" button opens the menu "Add files or photos" — it is only a
  // fallback, because the input is already there.
  const FILE_INPUT = [
    "input[data-testid='file-upload']",
    "input#chat-input-file-upload-onpage",
    "input[type='file'][multiple]:not([capture])",
    "input[type='file']:not([capture])",
  ];
  const ATTACH = [
    'button[aria-label="Add files, connectors, and more"]',
    'button[aria-label*="Add files" i]',
  ];

  async function attachFiles(files) {
    return B.attachFilesToPage(files, {
      inputs: FILE_INPUT,
      openAttach: ATTACH,
      pasteTarget: INPUT,
      name: "Claude",
    });
  }

  function log(msg) {
    try {
      console.log("[NemApi/claude] " + msg);
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
   *
   * `input` is the surface we wrote to: anchoring the search on it avoids
   * picking a send button that belongs to another part of the page.
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
        if (/stop|cancel|attach|upload|file/.test(label)) continue;
        return el;
      }
    }
    return B.findByText(["button", "[role='button']"], [/send/i, /submit/i], root);
  }

  /** Last completed assistant turn that owns an action-bar-copy button. */
  function getLastAssistantRoot() {
    // Prefer the message that has the most recent Copy button
    const copyBtns = document.querySelectorAll('button[data-testid="action-bar-copy"]');
    if (copyBtns.length) {
      const btn = copyBtns[copyBtns.length - 1];
      let el = btn.parentElement;
      for (let d = 0; d < 15 && el; d++) {
        const hasBody = el.querySelector(
          ".standard-markdown, [class*='prose'], [class*='markdown'], pre, code, p"
        );
        // textContent, not innerText: innerText forces a layout, and this
        // runs on every cheap poll while an answer streams.
        const len = (el.textContent || "").length;
        if (hasBody && len > 30) return el;
        el = el.parentElement;
      }
    }

    const done = document.querySelectorAll('[data-is-streaming="false"]');
    if (done.length) return done[done.length - 1];

    const md = document.querySelectorAll(".standard-markdown, [class*='font-claude-message']");
    if (md.length) return md[md.length - 1];
    return null;
  }

  function findCopyButton(root) {
    if (!root) root = document;
    let btn = root.querySelector('button[data-testid="action-bar-copy"]');
    if (btn) return btn;
    // Walk up to find sibling action bar
    let el = root;
    for (let i = 0; i < 8 && el; i++) {
      btn = el.querySelector('button[data-testid="action-bar-copy"]');
      if (btn) return btn;
      const group = el.querySelector('[role="group"][aria-label="Message actions"]');
      if (group) {
        btn = group.querySelector('button[data-testid="action-bar-copy"]');
        if (btn) return btn;
      }
      el = el.parentElement;
    }
    // Global last copy button
    const all = document.querySelectorAll('button[data-testid="action-bar-copy"]');
    return all.length ? all[all.length - 1] : null;
  }

  /**
   * Build markdown from DOM, forcing every <pre> into a fenced block with
   * exact textContent (indentation preserved).
   */
  function extractFromDom(root) {
    if (!root) return "";
    const clone = root.cloneNode(true);
    if (B.restoreMath) B.restoreMath(clone);
    clone
      .querySelectorAll(
        "button,[role='button'],[class*='copy'],[class*='toolbar'],[class*='action-bar'],svg,style,script"
      )
      .forEach((n) => n.remove());

    // Snapshot code blocks from ORIGINAL root (clone may lose some structure)
    const codeBlocks = [];
    root.querySelectorAll("pre").forEach((pre) => {
      const code = pre.querySelector("code");
      let lang = "";
      if (code) {
        const cls = code.className || "";
        const m = cls.match(/language-([a-z0-9_+-]+)/i) || cls.match(/lang-([a-z0-9_+-]+)/i);
        if (m) lang = m[1];
      }
      const body = ((code ? code.textContent : pre.textContent) || "")
        .replace(/\u00a0/g, " ")
        .replace(/\n$/, "");
      if (body.trim()) codeBlocks.push({ lang, body, pre });
    });

    // Replace pre in clone with placeholders so htmlToMarkdown doesn't mangle them
    const clonePres = clone.querySelectorAll("pre");
    clonePres.forEach((pre, i) => {
      const ph = document.createTextNode("\n\n@@CODE" + i + "@@\n\n");
      if (pre.parentNode) pre.parentNode.replaceChild(ph, pre);
    });

    const mdBody =
      clone.querySelector(".standard-markdown, [class*='prose'], [class*='markdown']") || clone;
    let md = B.htmlToMarkdown ? B.htmlToMarkdown(mdBody) : mdBody.textContent || "";

    // Inject exact code fences
    codeBlocks.forEach((c, i) => {
      const fence = "```" + c.lang + "\n" + c.body + "\n```";
      if (md.indexOf("@@CODE" + i + "@@") >= 0) {
        md = md.split("@@CODE" + i + "@@").join(fence);
      } else if (!md.includes(c.body.slice(0, Math.min(40, c.body.length)))) {
        md += "\n\n" + fence;
      }
    });
    // Clean leftover placeholders
    md = md.replace(/@@CODE\d+@@/g, "");

    return md.trim() || (root.innerText || "").trim();
  }

  function getLastResponse() {
    const root = getLastAssistantRoot();
    if (!root) return "";
    return extractFromDom(root);
  }

  /**
   * Cheap read of the current answer — what the polling loops use.
   *
   * `getLastResponse()` rebuilds the whole markdown (clone, math restore,
   * per-`<pre>` fences, htmlToMarkdown = O(answer)). Polling it every 400 ms
   * burned the tab's main thread for the whole duration of an answer and made
   * the page feel slow; the poll only needs "did the text move?".
   */
  function readCheapText(el) {
    const root = el || getLastAssistantRoot();
    return B.cheapText ? B.cheapText(root) : "";
  }

  /** Cheap "is something moving?" probe (turn count + length of the last one). */
  function getActivitySignature() {
    try {
      const root = getLastAssistantRoot();
      return getResponseCount() + ":" + (root ? readCheapText(root).length : 0);
    } catch (_) {
      return "0:0";
    }
  }

  /**
   * Is the model still streaming?
   *
   * Strict and local on purpose: a false positive is not free (the proxy
   * only accepts a stable answer once the provider is idle), and a
   * page-wide match on a class name keeps the page "busy" forever.
   * Claude exposes the state explicitly on the streaming node.
   */
  function isGenerating() {
    const streaming = document.querySelector('[data-is-streaming="true"]');
    if (streaming) return true;
    const stop = document.querySelector('button[aria-label*="Stop" i]');
    return !!(stop && B.isVisible(stop));
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
        "Claude: the prompt could not be written into the composer (the page keeps clearing it)"
      );
    }
    await B.clickSend({ input: res.input, btn: res.btn, findSend, text, log: (m) => log(m) });
  }

  /**
   * Clipboard path = same data the user gets when clicking Copy.
   * Try several times; fall back to DOM with forced code fences.
   */
  async function extractPremium() {
    const root = getLastAssistantRoot();
    if (!root) return getLastResponse();
    const fromDom = extractFromDom(root);

    if (!B.premiumEnabled()) return fromDom;

    const copyBtn = findCopyButton(root);
    const copyRoot = copyBtn
      ? copyBtn.closest('[role="group"]') || copyBtn.parentElement || root
      : root;

    // Attempt clipboard 2× (handlers can be flaky on first synthetic click)
    for (let attempt = 0; attempt < 2; attempt++) {
      const fromClip = await B.tryClipboardFromCopyButton(
        copyRoot,
        [
          'button[data-testid="action-bar-copy"]',
          'button[aria-label*="Copy" i]',
        ],
        { domAssistantText: fromDom }
      );
      if (fromClip && fromClip.length > 10) {
        const clipFences = (fromClip.match(/```/g) || []).length;
        const domFences = (fromDom.match(/```/g) || []).length;
        // Clipboard is authoritative when it has content and not fewer fences than DOM
        if (clipFences >= domFences || fromClip.length >= fromDom.length * 0.9) {
          return fromClip.trim();
        }
        // Clipboard missing code that DOM has → prefer DOM
        if (domFences > clipFences) return fromDom;
        return fromClip.trim();
      }
      await B.sleep(150);
    }

    const react = B.extractReactMarkdown(root);
    if (react && react.length > Math.max(30, fromDom.length * 0.85)) {
      return react.trim();
    }

    return fromDom || getLastResponse();
  }

  async function waitForResponse(previous = "") {
    // No leading sleep: the answer may already be streaming when the job is
    // dispatched, and the loop below sees it on its first tick.
    await B.waitUntilStable(
      () => {
        const cheap = readCheapText();
        if (isGenerating() && !cheap) return "";
        return cheap;
      },
      { timeout: 180000, stableMs: 2500, previous, getFinal: getLastResponse }
    );
    try {
      return await extractPremium();
    } catch (_) {
      return getLastResponse();
    }
  }

  global.NemApiProviders = global.NemApiProviders || {};
  
  function getResponseCount() {
    try {
      // Prefer completed assistant turns with a Copy action-bar button
      const copies = document.querySelectorAll('button[data-testid="action-bar-copy"]');
      if (copies.length) return copies.length;
      const done = document.querySelectorAll('[data-is-streaming="false"]');
      if (done.length) return done.length;
      return document.querySelectorAll(
        ".standard-markdown, [class*='font-claude-message']"
      ).length;
    } catch (_) {
      return 0;
    }
  }

  global.NemApiProviders.claude = {
    id: "claude",
    match: (url) => /claude\.ai/i.test(url),
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
