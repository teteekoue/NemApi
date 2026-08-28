/**
 * NemApi – DeepSeek adapter
 * Extracts clean markdown from the DOM (strips Copy/Download/JSON UI chrome)
 * and is compatible with coding agents.
 *
 * Updated for DeepSeek UI changes (2025–2026): more reliable send-button
 * detection (SVG path + aria-disabled wait) and virtual-list response handling.
 */
(function (global) {
  "use strict";
  const B = global.NemApiBase;

  const INPUT = [
    "textarea#chat-input",
    'textarea[placeholder*="DeepSeek" i]',
    'textarea[placeholder*="Message DeepSeek" i]',
    'textarea[placeholder*="发送" i]',
    'textarea[placeholder*="Message" i]',
    'textarea[role="textbox"]',
    ".ds-textarea textarea",
    "textarea[data-testid*='chat' i]",
    "div[contenteditable='true'][role='textbox']",
    "div[contenteditable='true']",
    "textarea",
  ];

  // Broad CSS candidates; real filtering happens in isSendButton()
  const SEND_CANDIDATES = [
    '[role="button"].ds-icon-button',
    "button.ds-icon-button",
    '[role="button"].ds-button--primary',
    "button.ds-button--primary",
    '[role="button"].ds-button--circle',
    "button.ds-button--circle",
    '[role="button"][class*="_52c986b"]',
    "button[class*='_52c986b']",
    '[role="button"][class*="_7436101"]',
    "div[role='button'][aria-disabled]",
    'button[type="submit"]',
    'button[aria-label*="Send" i]',
    '[role="button"][aria-label*="Send" i]',
    "button[class*='send']",
    '[role="button"]',
    "button",
  ];

  const MESSAGE = [
    "[data-virtual-list-item-key] .ds-message",
    ".ds-message",
    "[class*='ds-message']",
  ];

  // Known send-icon SVG path fragments (paper-plane / arrow) used by DeepSeek
  const SEND_SVG_PATH_RE =
    /M8\.3125|M13\.12\s*19\.98|M12\s*5\.25|paper|send|arrow/i;

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
        if (
          /^(Copy|Copied|Download|JSON|Think|Thinking|Regenerate|Retry|Share|Stop|Edit|Like|Dislike|Report|复制|下载|重新生成)$/i.test(
            s
          )
        ) {
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

  function isVisible(el) {
    return B.isVisible(el);
  }

  function isDisabled(el) {
    if (B.isDisabled) return B.isDisabled(el);
    if (!el) return true;
    if (el.getAttribute("aria-disabled") === "true") return true;
    if (el.disabled) return true;
    const cls = (el.className && String(el.className)) || "";
    if (/\bdisabled\b/i.test(cls)) return true;
    return false;
  }

  /**
   * Decide whether a candidate is the real Send button (not attach / stop / mic…).
   */
  function isSendButton(el) {
    if (!el || !isVisible(el) || isDisabled(el)) return false;

    const label = `${el.getAttribute("aria-label") || ""} ${el.title || ""} ${
      el.innerText || el.textContent || ""
    }`.toLowerCase();

    // Explicit reject list
    if (
      /stop|cancel|attach|upload|file|camera|image|voice|microphone|mic|plus|添加|附件|上传|图片|语音|停止|中止/.test(
        label
      )
    ) {
      return false;
    }
    if (el.classList && el.classList.contains("ds-toggle-button")) return false;
    if (el.classList && el.classList.contains("bds-plus-btn")) return false;

    // Strong positive: SVG path that matches known DeepSeek send icon
    const paths = el.querySelectorAll("svg path");
    for (const p of paths) {
      const d = p.getAttribute("d") || "";
      if (SEND_SVG_PATH_RE.test(d)) return true;
    }

    // aria-label / title
    if (/send|envoyer|发送|submit/i.test(label)) return true;

    // Class-based heuristics used by recent DeepSeek builds
    const cls = (el.className && String(el.className)) || "";
    if (
      (cls.includes("ds-icon-button") ||
        cls.includes("ds-button--primary") ||
        cls.includes("ds-button--circle") ||
        cls.includes("_52c986b") ||
        cls.includes("_7436101")) &&
      !/attach|upload|file|plus/.test(cls)
    ) {
      // Prefer the one that is not a stop button and is near the composer
      return true;
    }

    return false;
  }

  function findSend(near) {
    const root = B.findComposerRoot(near) || document.body;

    // 1) Prefer candidates inside the composer, reverse order (rightmost / latest)
    const candidates = [];
    for (const s of SEND_CANDIDATES) {
      try {
        root.querySelectorAll(s).forEach((n) => candidates.push(n));
      } catch (_) {}
    }
    // Deduplicate while preserving reverse order preference
    const seen = new Set();
    const unique = [];
    for (let i = candidates.length - 1; i >= 0; i--) {
      const n = candidates[i];
      if (seen.has(n)) continue;
      seen.add(n);
      unique.push(n);
    }

    for (const n of unique) {
      if (isSendButton(n)) return n;
    }

    // 2) Global fallback with text patterns
    const byText = B.findByText(
      ["button", "[role='button']"],
      [/send/i, /submit/i, /发送/],
      root
    );
    if (byText && isSendButton(byText)) return byText;

    return null;
  }

  /**
   * Wait until the Send button becomes enabled after pasting text.
   * Critical for 3rd+ messages where React state update can lag.
   */
  async function waitForEnabledSend(input, timeoutMs = 3500) {
    const t0 = Date.now();
    while (Date.now() - t0 < timeoutMs) {
      const btn = findSend(input);
      if (btn && !isDisabled(btn) && isVisible(btn)) return btn;
      await B.sleep(80);
    }
    return findSend(input); // last attempt even if still looking disabled
  }

  function getLastResponse() {
    const els = getMessageEls();
    if (els.length) return readMessageText(els[els.length - 1]);
    return "";
  }

  function isGenerating() {
    // Prefer explicit Stop button
    const stop =
      document.querySelector('button[aria-label*="Stop" i]') ||
      document.querySelector('[role="button"][aria-label*="Stop" i]') ||
      document.querySelector('[class*="stop"]');
    if (stop && isVisible(stop)) return true;

    // Loading indicators
    if (
      document.querySelector(".ds-loading, [class*='loading'], [class*='spinner']")
    ) {
      return true;
    }
    return false;
  }

  async function sendPrompt(text) {
    const input =
      findInput() || (await B.waitFor(INPUT[0], 12000).catch(() => null));
    if (!input) throw new Error("DeepSeek: input not found");

    // Ensure focus and clear any residual selection
    try {
      input.focus();
      input.click();
    } catch (_) {}

    B.pasteText(input, text);

    // Extra events some React builds need after the 2nd/3rd message
    try {
      input.dispatchEvent(
        new InputEvent("input", {
          bubbles: true,
          cancelable: true,
          inputType: "insertFromPaste",
          data: String(text ?? ""),
        })
      );
      input.dispatchEvent(new Event("change", { bubbles: true }));
    } catch (_) {}

    // Give React time to enable the send control (critical on later turns)
    await B.sleep(220);

    let btn = await waitForEnabledSend(input, 3500);

    if (btn && !isDisabled(btn)) {
      // Prefer a trusted-looking click sequence
      try {
        btn.focus();
      } catch (_) {}
      const clicked = B.clickEl(btn);
      if (clicked) {
        // Verify that the input was cleared (message accepted) within a short window
        await B.sleep(350);
        const stillFull =
          (input.value || input.textContent || "").trim().length >
          Math.min(20, String(text).length * 0.6);
        if (!stillFull) return; // success
      }
    }

    // Fallback 1: native Enter (works when "Enter to send" is enabled)
    B.pressEnter(input);
    await B.sleep(250);

    // Fallback 2: try clicking again after Enter attempt
    btn = findSend(input);
    if (btn && !isDisabled(btn)) {
      B.clickEl(btn);
    }
  }

  async function waitForResponse(previous = "") {
    await B.sleep(600);

    // Phase 1: wait for generation to start (Stop button or new content)
    const genStart = Date.now();
    while (Date.now() - genStart < 12000) {
      if (isGenerating()) break;
      const els = getMessageEls();
      const last = els.length ? readMessageText(els[els.length - 1]) : "";
      if (last && last !== previous && last.length > (previous || "").length) {
        break;
      }
      await B.sleep(250);
    }

    await B.waitForNewResponse(getMessageEls, readMessageText, {
      timeout: 180000,
      stableMs: 2400,
      previous,
      newElTimeout: 25000,
    });

    // Small extra settle time after stability for virtual list re-renders
    await B.sleep(300);

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
