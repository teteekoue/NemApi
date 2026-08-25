/**
 * NemApi – DeepSeek adapter
 * Extracts clean markdown from the DOM (strips Copy/Download/JSON UI chrome)
 * and is compatible with coding agents.
 */
(function (global) {
  "use strict";
  const B = global.NemApiBase;

  const INPUT = [
    'textarea[role="textbox"]',
    'textarea[placeholder*="DeepSeek" i]',
    'textarea[placeholder*="Message DeepSeek" i]',
    'textarea[placeholder*="发送"]',
    "textarea#chat-input",
    "textarea[data-testid*='chat' i]",
    "div[contenteditable='true'][role='textbox']",
    "div[contenteditable='true']",
    ".ds-textarea textarea",
    "textarea",
  ];

  const SEND = [
    'button[type="submit"]',
    'button[aria-label*="Send" i]',
    '[role="button"][aria-label*="Send" i]',
    "button.ds-icon-button",
    "button.ds-button--primary",
    '[role="button"].ds-button--circle',
    "button[class*='send']",
  ];

  const MESSAGE = [
    "[data-virtual-list-item-key] .ds-message",
    ".ds-message",
    "[class*='ds-message']",
    "[class*='message']",
  ];

  const UI_NOISE_RE = new RegExp(
    [
      "\\bCopy\\b",
      "\\bCopied\\b",
      "\\bDownload\\b",
      "\\bJSON\\b",
      "\\bThink\\b",
      "\\bThinking\\b",
      "\\bRegenerate\\b",
      "\\bRetry\\b",
      "\\bShare\\b",
      "\\bContinue\\b",
      "\\bStop\\b",
      "\\bEdit\\b",
      "\\bLike\\b",
      "\\bDislike\\b",
      "\\bReport\\b",
      "复制",
      "下载",
      "重新生成",
    ].join("|"),
    "gi"
  );

  function getMessageEls() {
    for (const s of MESSAGE) {
      const nodes = document.querySelectorAll(s);
      if (nodes.length) return Array.from(nodes);
    }
    return [];
  }

  function domToMarkdown(root) {
    if (!root) return "";
    const clone = root.cloneNode(true);

    const killSelectors = [
      "button",
      "[role='button']",
      "[class*='copy']",
      "[class*='download']",
      "[class*='toolbar']",
      "[class*='action']",
      "[class*='icon-button']",
      "[class*='ds-icon']",
      ".ds-think-content",
      "[class*='think']",
      "[class*='thinking']",
      "svg",
      "style",
      "script",
      "noscript",
    ];
    for (const sel of killSelectors) {
      clone.querySelectorAll(sel).forEach((n) => n.remove());
    }

    clone.querySelectorAll("pre").forEach((pre) => {
      const code = pre.querySelector("code");
      let lang = "";
      if (code) {
        const cls = code.className || "";
        const m = cls.match(/language-([a-z0-9_+-]+)/i);
        if (m) lang = m[1];
      }
      const body = (code ? code.textContent : pre.textContent) || "";
      const fence = "```" + lang + "\n" + body.replace(/\n$/, "") + "\n```";
      const replacement = document.createTextNode("\n\n" + fence + "\n\n");
      pre.parentNode.replaceChild(replacement, pre);
    });

    let text = clone.innerText || clone.textContent || "";
    return cleanResponse(text);
  }

  function cleanResponse(text) {
    let t = String(text || "");
    t = t.replace(/<think>[\s\S]*?<\/think>/gi, "");
    t = t.replace(/```think[\s\S]*?```/gi, "");

    t = t
      .split("\n")
      .filter((line) => {
        const s = line.trim();
        if (!s) return true;
        if (/^(Copy|Copied|Download|JSON|Think|Thinking|Regenerate|Retry|Share|Stop|Edit|Like|Dislike|Report|复制|下载|重新生成)$/i.test(s)) {
          return false;
        }
        if (/^(Copy|Download|JSON)\s*[:：.]?\s*$/i.test(s)) return false;
        return true;
      })
      .join("\n");

    t = t.replace(UI_NOISE_RE, (match, offset, full) => {
      const before = full.slice(0, offset);
      const opens = (before.match(/```/g) || []).length;
      if (opens % 2 === 1) return match;
      return "";
    });

    t = t.replace(/[ \t]+\n/g, "\n");
    t = t.replace(/\n{3,}/g, "\n\n");
    return t.trim();
  }

  function readMessageText(msgEl) {
    if (!msgEl) return "";

    // Fast sync path used while polling for stability
    const main =
      msgEl.querySelector(".ds-assistant-message-main-content") ||
      msgEl.querySelector("[class*='assistant-message-main']") ||
      msgEl.querySelector(".ds-markdown:not(.ds-think-content .ds-markdown)") ||
      msgEl.querySelector(".ds-markdown") ||
      msgEl.querySelector("[class*='markdown']");

    if (main) {
      const md = domToMarkdown(main);
      if (md) return md;
    }

    const blocks = msgEl.querySelectorAll(".ds-markdown, [class*='markdown']");
    const parts = [];
    for (const b of blocks) {
      if (b.closest(".ds-think-content, [class*='think']")) continue;
      const md = domToMarkdown(b);
      if (md) parts.push(md);
    }
    if (parts.length) return parts.join("\n\n").trim();

    return cleanResponse(msgEl.innerText || msgEl.textContent || "");
  }

  /**
   * Capture the same markdown the Copy button would write — without clicking.
   * DeepSeek stores the original MD in React memoizedProps.markdown on .ds-markdown
   * (see DeepSeek-Chat-Exporter). Thinking blocks use .ds-think-content — skip them.
   */
  async function extractPremium(msgEl) {
    if (!msgEl) return "";

    // 1) React source markdown (primary — no click)
    const answerNodes = [];
    msgEl.querySelectorAll("div.ds-markdown, .ds-markdown").forEach((el) => {
      if (el.closest(".ds-think-content, [class*='think'], .e1675d8b")) return;
      answerNodes.push(el);
    });
    let best = "";
    for (const el of answerNodes) {
      const md = B.extractReactMarkdown(el, ["markdown", "content", "text"]);
      if (md && md.length > best.length) best = md;
    }
    if (!best && answerNodes[0]) {
      // also try parent message fiber
      best = B.extractReactMarkdown(msgEl, ["markdown", "content", "text"]) || "";
    }
    if (best && best.length > 10) {
      return cleanResponse(best);
    }

    // 2) Clipboard intercept only if React failed (may fail on isTrusted)
    if (B.premiumEnabled()) {
      const fromClip = await B.tryClipboardFromCopyButton(msgEl, [
        'button[aria-label*="Copy" i]',
        'button[aria-label*="复制"]',
        "button.ds-icon-button",
      ]);
      if (fromClip && fromClip.length > 10 && fromClip !== "[object Object]") {
        return cleanResponse(fromClip);
      }
    }

    // 3) Structured HTML → MD on answer nodes only
    const parts = [];
    for (const el of answerNodes) {
      const md = B.htmlToMarkdown(el);
      if (md) parts.push(md);
    }
    if (parts.length) return cleanResponse(parts.join("\n\n"));

    return readMessageText(msgEl);
  }

  function findInput() {
    return B.queryFirst(INPUT);
  }

  function findSend(near) {
    const root = B.findComposerRoot(near);
    const btn = B.findByText(SEND, [/send/i, /^go$/i, /submit/i], root);
    if (btn && btn.getAttribute("aria-disabled") !== "true" && !btn.disabled) return btn;
    for (const s of SEND) {
      const nodes = root.querySelectorAll(s);
      for (const n of nodes) {
        const label = `${n.getAttribute("aria-label") || ""} ${n.title || ""} ${(n.innerText || n.textContent || "")}`.toLowerCase();
        if (n.getAttribute("aria-disabled") === "true") continue;
        if (n.disabled) continue;
        if (/stop|cancel|attach|upload|file/.test(label)) continue;
        if (!B.isVisible(n)) continue;
        return n;
      }
    }
    return B.findByText(["button", "[role='button']"], [/send/i, /submit/i, /chat/i], root);
  }

  function getLastResponse() {
    const els = getMessageEls();
    if (els.length) return readMessageText(els[els.length - 1]);
    return "";
  }

  function isGenerating() {
    return !!(
      document.querySelector('[class*="stop"]') ||
      document.querySelector('button[aria-label*="Stop" i]') ||
      document.querySelector(".ds-loading, [class*='loading']")
    );
  }

  async function sendPrompt(text) {
    const input = findInput() || (await B.waitFor(INPUT[0], 10000).catch(() => null));
    if (!input) throw new Error("DeepSeek: input not found");
    B.pasteText(input, text);
    await B.sleep(180);
    const btn = findSend(input);
    if (btn) {
      B.clickEl(btn);
      return;
    }
    B.pressEnter(input);
    B.pressEnter(input);
  }

  async function waitForResponse(previous = "") {
    await B.sleep(800);
    await B.waitForNewResponse(getMessageEls, readMessageText, {
      timeout: 180000,
      stableMs: 2200,
      previous,
    });
    const els = getMessageEls();
    const last = els.length ? els[els.length - 1] : null;
    if (!last) return "";
    try {
      return await extractPremium(last);
    } catch (_) {
      return readMessageText(last);
    }
  }

  global.NemApiProviders = global.NemApiProviders || {};
  global.NemApiProviders.deepseek = {
    id: "deepseek",
    match: (url) => /chat\.deepseek\.com/i.test(url),
    sendPrompt,
    waitForResponse,
    getLastResponse,
    isGenerating,
    findInput,
  };

})(typeof window !== "undefined" ? window : self);
