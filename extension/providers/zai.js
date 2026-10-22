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

  // Image upload — the manifest covers THREE different sites and they do NOT
  // share a front-end:
  //
  //  - chat.z.ai (international): a hidden `input[type=file][multiple]` in
  //    `div.messageInputContainer`, and a "More" button
  //    `button#upload-file-button` (aria-label="More", a "+" icon) that opens
  //    the upload menu (live DOM capture, tamga 2026-10-18).
  //  - chatglm.cn (智谱清言): a DIFFERENT app (Element-Plus upload). Its
  //    input is `.upload-demo input.el-upload__input[type=file]` — the
  //    `.img-input` entry is a dead legacy one and the avatar menu has a
  //    file input of its own, so the selector must stay scoped to the
  //    composer.
  //  - bigmodel.cn: the open platform, no chat UI — nothing to automate.
  //
  // The bare "input[type=file]" fallback is therefore NOT used here: on
  // chatglm.cn it would land on the avatar's input.
  const FILE_INPUT_CHATZ = [
    ".messageInputContainer input[type='file']",
    "input[type='file'][multiple][accept*='.png']",
    "input[type='file'][multiple]:not([capture])",
  ];
  const FILE_INPUT_CHATGLM = [
    ".upload-demo input.el-upload__input[type='file']",
    ".upload-demo input[type='file']",
  ];
  const ATTACH = [
    "button#upload-file-button",
    'button[aria-label*="More" i]',
  ];

  function fileInputs() {
    let host = "";
    try {
      host = String(location.hostname || "");
    } catch (_) {}
    if (/chatglm\.cn/i.test(host)) return FILE_INPUT_CHATGLM;
    return FILE_INPUT_CHATZ;
  }

  async function attachFiles(files) {
    return B.attachFilesToPage(files, {
      inputs: fileInputs(),
      openAttach: ATTACH,
      pasteTarget: INPUT,
      name: "Z.ai",
    });
  }

  function findInput() {
    return B.queryFirst(INPUT);
  }

  /** The editable surfaces of the composer, best candidate first. */
  function inputCandidates() {
    return B.inputCandidatesFrom(INPUT);
  }

  function log(msg) {
    try {
      console.log("[NemApi/zai] " + msg);
    } catch (_) {}
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
      .replace(/^[ \t]*GLM[ \t]*[:：]?[ \t]*$/gim, "")
      .replace(/^[ \t]*Z\.?ai[ \t]*[:：]?[ \t]*$/gim, "")
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
    if (B.restoreMath) B.restoreMath(clone);
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

  /**
   * Is the model currently streaming?
   *
   * Kept deliberately STRICT: a false positive here is not free. The proxy
   * only accepts a stable answer once the provider is idle, and a stuck
   * "generating" indicator delays every request (the answer is there, the
   * proxy just never returns it). A page-wide `[class*='stop']` match hits any
   * element whose class contains "stop" and keeps the page busy forever.
   */
  function isGenerating() {
    const stops = document.querySelectorAll(
      'button[aria-label^="Stop" i], [role="button"][aria-label^="Stop" i],' +
        'button[title^="Stop" i], [aria-label^="停止" i]'
    );
    for (const el of stops) {
      if (B.isVisible(el)) return true;
    }
    // a spinner inside the composer or the last answer, not anywhere on the page
    const scope = findComposerRoot(findInput());
    if (scope && scope.querySelector) {
      const spin = scope.querySelector(
        "[class*='streaming'], [class*='generating'], [class*='stop-generating'], [aria-busy='true']"
      );
      if (spin && B.isVisible(spin)) return true;
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
        "Z.ai: the prompt could not be written into the composer (the page keeps clearing it)"
      );
    }
    await B.clickSend({ input: res.input, btn: res.btn, findSend, text, log: (m) => log(m) });
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
    attachFiles,
    waitForResponse,
    getLastResponse,
    getResponseCount,
    isGenerating,
    findInput,
  };
})(typeof window !== "undefined" ? window : self);
