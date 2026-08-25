/**
 * NemApi – Claude adapter
 * Primary strategy: same path as the native Copy button (clipboard intercept).
 * Fallback: full turn DOM with every <pre><code> preserved.
 */
(function (global) {
  "use strict";
  const B = global.NemApiBase;

  const INPUT = [
    'div[contenteditable="true"][role="textbox"]',
    'div.ProseMirror[contenteditable="true"]',
    'div[contenteditable="true"].ProseMirror',
    '[data-testid="chat-input"]',
    'div[contenteditable="true"]',
  ];

  const SEND = [
    'button[aria-label*="Send message" i]',
    'button[aria-label*="Send" i]',
    'button[data-testid="send-button"]',
    'button[type="submit"]',
    '[role="button"][aria-label*="Send" i]',
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
        const len = (el.innerText || "").length;
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

  function isGenerating() {
    return !!(
      document.querySelector('[data-is-streaming="true"]') ||
      document.querySelector('button[aria-label*="Stop" i]')
    );
  }

  async function sendPrompt(text) {
    const input = findInput() || (await B.waitFor('div.ProseMirror[contenteditable="true"]', 12000));
    if (!input) throw new Error("Claude: input not found");
    B.pasteText(input, text);
    await B.sleep(180);
    const btn = findSend();
    if (btn) {
      B.clickEl(btn);
      return;
    }
    B.pressEnter(input);
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
      const fromClip = await B.tryClipboardFromCopyButton(copyRoot, [
        'button[data-testid="action-bar-copy"]',
        'button[aria-label*="Copy" i]',
      ]);
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
    await B.sleep(1000);
    await B.waitUntilStable(
      () => {
        if (isGenerating() && !getLastResponse()) return "";
        return getLastResponse();
      },
      { timeout: 180000, stableMs: 2500, previous }
    );
    // Small delay so action-bar-copy is mounted after stream ends
    await B.sleep(300);
    try {
      return await extractPremium();
    } catch (_) {
      return getLastResponse();
    }
  }

  global.NemApiProviders = global.NemApiProviders || {};
  global.NemApiProviders.claude = {
    id: "claude",
    match: (url) => /claude\.ai/i.test(url),
    sendPrompt,
    waitForResponse,
    getLastResponse,
    isGenerating,
    findInput,
  };
})(typeof window !== "undefined" ? window : self);
