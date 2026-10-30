/**
 * NemApi – Qwen adapter
 * Primary strategy: same path as the native Copy button (clipboard intercept via
 * .copy-response-button) so structured content (code fences, indentation) is
 * preserved exactly as the UI intends.
 * Fallback: line-by-line DOM extraction (Qwen highlighter collapses textContent)
 * + React fiber markdown when available.
 */
(function (global) {
  "use strict";
  const B = global.NemApiBase;

  const INPUT = [
    // The most specific selectors first: a bare "textarea" can match an
    // unrelated hidden field, and writing into the wrong surface is silent
    // (the text goes nowhere and the send button stays disabled).
    "#chat-input",
    "textarea.message-input-textarea",
    "textarea.text-area-box-web",
    'textarea[role="textbox"]',
    '[contenteditable="true"][role="textbox"]',
    'textarea[placeholder*="Ask" i]',
    'textarea[placeholder*="message" i]',
    "textarea",
  ];

  const SEND = [
    // Live DOM (rakaarwaky/qwen-web-arwaky): the send control lives in a
    // `.message-input-right-button-send` wrapper.
    ".message-input-right-button-send button",
    'button[type="submit"]',
    "button.send-button",
    'button[aria-label*="Send" i]',
    'button[class*="send"]',
    '[role="button"][aria-label*="Send" i]',
  ];

  const MESSAGE = [
    ".qwen-chat-message-assistant",
    ".chat-response-message",
    "[class*='qwen-chat-message-assistant']",
    "[class*='chat-response-message']",
    "[class*='response-message']",
    "[class*='assistant'] [class*='message']",
    "[class*='assistant']",
  ];

  // Image upload (rakaarwaky/qwen-web-arwaky, relevé 2026-08-09 ; live DOM
  // capture tamga 2026-10-18) : le passage passe par un MENU
  // (.mode-select-open → item « Upload attachment ») dont l'item ouvre le
  // SÉLECTEUR NATIF — qu'on ne peut pas piloter depuis un content script et
  // qui bloquerait la page. On ouvre donc le menu (DOM), on cherche l'input,
  // et sinon on passe par un collage synthétique (transport « paste »).
  //
  // `input#filesUpload` est PERSISTANT dans le DOM (tabindex=-1,
  // aria-label="Upload files") : c'est lui qu'on viserait en premier. Le
  // transport « drop » sur #dropzone-container est le plus fiable du site
  // (deux implémentations indépendantes le privilégient), il reste donc en
  // recours quand l'input passe inaperçu.
  const FILE_INPUT = [
    "input#filesUpload[type='file']",
    "#filesUpload",
    "input[type='file'][multiple]:not([capture])",
    "input[type='file']:not([capture])",
  ];
  const ATTACH = [".mode-select-open", "[class*='mode-select']"];
  const DROP_TARGET = ["#dropzone-container", ".message-input-container"];

  async function attachFiles(files) {
    return B.attachFilesToPage(files, {
      inputs: FILE_INPUT,
      openAttach: ATTACH,
      pasteTarget: INPUT,
      dropTarget: DROP_TARGET,
      name: "Qwen",
    });
  }

  const LANG_RE =
    /^(python|py|javascript|js|typescript|ts|java|c|cpp|c\+\+|csharp|c#|go|rust|ruby|php|swift|kotlin|scala|r|sql|bash|sh|shell|zsh|powershell|ps1|html|css|scss|json|yaml|yml|xml|markdown|md|text|plaintext|plain)$/i;

  function getMessageEls() {
    for (const s of MESSAGE) {
      const nodes = document.querySelectorAll(s);
      if (nodes.length) return Array.from(nodes);
    }
    return [];
  }

  function dedupeRepeatedContent(text) {
    let t = String(text || "").trim();
    if (t.length < 40) return t;
    for (let n = 2; n <= 4; n++) {
      const partLen = Math.floor(t.length / n);
      if (partLen < 30) break;
      const part = t.slice(0, partLen).trim();
      if (part.length < 30) break;
      let allSame = true;
      for (let i = 0; i < n; i++) {
        const slice = t.slice(i * partLen, (i + 1) * partLen).trim();
        if (slice.replace(/\s+/g, " ") !== part.replace(/\s+/g, " ")) {
          allSame = false;
          break;
        }
      }
      if (allSame) return part;
    }
    const chunks = t.split(/\n{2,}/);
    const out = [];
    let prevNorm = "";
    for (const c of chunks) {
      const norm = c.replace(/\s+/g, " ").trim();
      if (!norm || norm === prevNorm) continue;
      out.push(c);
      prevNorm = norm;
    }
    return out.join("\n\n").trim();
  }

  function cleanResponse(text) {
    let t = String(text || "")
      .replace(/<think>[\s\S]*?<\/think>/gi, "")
      .replace(/^[ \t]*(JSON|Copy|Download|Think)[ \t]*[:：]?[ \t]*$/gim, "")
      .trim();
    // Fix broken fences where language stuck to first token: ```\npythonimport → ```python\nimport
    t = t.replace(/```\s*\n?(python|javascript|js|ts|typescript|bash|sh|json|html|css|java|go|rust|sql|yaml|yml|cpp|c\+\+|csharp|php|ruby)(?=[a-zA-Z_])/gi, "```$1\n");
    if (B.cleanMarkdownCodeFences) t = B.cleanMarkdownCodeFences(t);
    t = dedupeRepeatedContent(t);
    return t;
  }

  function pickContentRoot(msgEl) {
    if (!msgEl) return null;
    const selectors = [
      ".response-message-content.phase-answer",
      ".response-message-content",
      ".qwen-markdown",
      ".markdown-body",
      ".response-message-body",
      "[class*='response-message-content']",
      "[class*='markdown']",
    ];
    const candidates = [];
    for (const s of selectors) {
      msgEl.querySelectorAll(s).forEach((el) => {
        if (el.closest("[class*='think'], [class*='reason'], [class*='reflect'], [class*='status-card'], [class*='thinking']"))
          return;
        candidates.push(el);
      });
    }
    const outer = candidates.filter(
      (el) => !candidates.some((other) => other !== el && other.contains(el))
    );
    if (outer.length === 1) return outer[0];
    if (outer.length > 1) {
      outer.sort((a, b) => (b.textContent || "").length - (a.textContent || "").length);
      return outer[0];
    }
    return msgEl;
  }

  /** Read language from header / class — never from code body. */
  function detectLang(blockEl, codeEl) {
    // class language-xxx
    const nodes = [codeEl, blockEl, blockEl && blockEl.parentElement].filter(Boolean);
    for (const n of nodes) {
      const cls = (n.className && String(n.className)) || "";
      const m = cls.match(/language-([a-z0-9_+-]+)/i) || cls.match(/lang(?:uage)?-([a-z0-9_+-]+)/i);
      if (m && LANG_RE.test(m[1])) return m[1].toLowerCase();
    }
    // header / toolbar label near the block
    const parent = blockEl.parentElement || blockEl;
    const header = parent.querySelector(
      "[class*='language'], [class*='lang'], .code-block-header, [class*='code-header'], [class*='toolbar']"
    );
    if (header) {
      const ht = (header.textContent || "").trim().split(/\s+/)[0];
      if (ht && LANG_RE.test(ht)) return ht.toLowerCase();
    }
    // data attributes
    for (const n of nodes) {
      const d =
        n.getAttribute("data-language") ||
        n.getAttribute("data-lang") ||
        n.getAttribute("data-mode");
      if (d && LANG_RE.test(d)) return d.toLowerCase();
    }
    return "";
  }

  /**
   * Qwen renders each code line as its own element. textContent of <pre>
   * often concatenates WITHOUT newlines → "import randomimport string".
   * So we reconstruct line-by-line.
   */
  function extractLinesFromCodeBlock(blockEl) {
    const lineSel = [
      ":scope > code > .line",
      ":scope > code > [class*='line']",
      ":scope > .line",
      ":scope > [class*='line']",
      "code .line",
      "code [class*='code-line']",
      ".hljs-line",
      "[class*='code-line']",
      "tr", // some UIs use table rows
    ];
    let lineNodes = [];
    for (const s of lineSel) {
      try {
        const found = blockEl.querySelectorAll(s);
        if (found.length >= 2) {
          lineNodes = Array.from(found);
          break;
        }
      } catch (_) {}
    }

    if (lineNodes.length >= 2) {
      return lineNodes
        .map((line) => {
          const clone = line.cloneNode(true);
          clone
            .querySelectorAll(
              "[class*='line-number'],[class*='linenumber'],[class*='line-num'],.line-numbers,[data-line-number],.cm-gutter,button,svg"
            )
            .forEach((n) => n.remove());
          // Prefer the code portion if structure is gutter|code
          const codePart =
            clone.querySelector("[class*='code-content'], [class*='line-content'], .content, code") ||
            clone;
          let text = codePart.textContent || "";
          text = text.replace(/\u00a0/g, " ").replace(/\r/g, "");
          // Single line: strip only trailing newline, keep indent
          text = text.replace(/\n$/, "");
          // If gutter number left at start: "  12  code"
          text = text.replace(/^\s*\d{1,4}\s{2,}/, "");
          text = text.replace(/^\s*\d+\s*[|：:.\)]\s?/, "");
          return text;
        })
        .join("\n");
    }

    // Fallback: textContent with post-cleanup
    const clone = blockEl.cloneNode(true);
    clone
      .querySelectorAll(
        "[class*='line-number'],[class*='linenumber'],[class*='line-num'],.line-numbers,[data-line-number],button,svg"
      )
      .forEach((n) => n.remove());
    const code = clone.querySelector("code") || clone;
    let body = (code.textContent || "").replace(/\u00a0/g, " ");
    if (B.stripLeadingLineNumbers) body = B.stripLeadingLineNumbers(body);
    return body.replace(/\n$/, "");
  }

  /** Find all code blocks under a message (pre OR code-block wrappers). */
  function findCodeBlocks(root) {
    const blocks = [];
    const seen = new Set();

    const candidates = root.querySelectorAll(
      "pre, [class*='code-block'], [class*='codeblock'], [class*='highlight']"
    );
    candidates.forEach((el) => {
      // Prefer outermost
      if ([...candidates].some((o) => o !== el && o.contains(el))) return;
      if (seen.has(el)) return;
      seen.add(el);

      const codeEl = el.tagName === "PRE" ? el.querySelector("code") || el : el.querySelector("pre, code") || el;
      const lang = detectLang(el, codeEl);
      let body = extractLinesFromCodeBlock(el.tagName === "PRE" ? el : codeEl.closest("pre") || codeEl || el);

      // Strip accidental language prefix glued to body: "pythonimport" → "import"
      if (lang && body.toLowerCase().startsWith(lang.toLowerCase()) && !body.startsWith(lang + "\n")) {
        const rest = body.slice(lang.length);
        if (rest && /^[a-zA-Z_]/.test(rest)) body = rest;
      }
      // Same without known lang
      const glued = body.match(
        /^(python|javascript|typescript|bash|json|html|css|java|go|rust|sql)(?=[a-zA-Z_])/i
      );
      if (glued) {
        body = body.slice(glued[1].length);
        if (!lang) return void blocks.push({ el, lang: glued[1].toLowerCase(), body });
      }

      if (body.trim()) blocks.push({ el, lang, body });
    });
    return blocks;
  }

  /**
   * Re-format code body when the DOM flattened it.
   * - JSON: parse + pretty-print with 2-space indent
   * - Glued language prefix: strip "python"/"json" stuck to first token
   * - Ensure real newlines (not a single mega-line)
   */
  function formatCodeBody(lang, body) {
    let b = String(body || "").replace(/\u00a0/g, " ").replace(/\r/g, "");
    b = b.replace(/^\n+|\n+$/g, "");

    // Language glued to first token: json{ → {  / pythonimport → import
    const glued = b.match(
      /^(python|javascript|typescript|json|bash|sh|html|css|java|go|rust|sql|yaml|yml|xml|php|ruby|markdown|md)(?=[\{\[a-zA-Z_])/i
    );
    if (glued) {
      if (!lang) lang = glued[1].toLowerCase();
      b = b.slice(glued[1].length);
    }

    const looksJson =
      lang === "json" ||
      /^[\s]*[\{\[]/.test(b) && /[\}\]][\s]*$/.test(b) && /"[^"]+"\s*:/.test(b);

    if (looksJson) {
      try {
        // Sometimes body is almost-JSON with soft line breaks already — join then parse
        const compact = b.replace(/\n/g, " ").replace(/\s+/g, " ").trim();
        const parsed = JSON.parse(compact);
        b = JSON.stringify(parsed, null, 2);
        lang = lang || "json";
      } catch (_) {
        // try original with newlines
        try {
          const parsed = JSON.parse(b);
          b = JSON.stringify(parsed, null, 2);
          lang = lang || "json";
        } catch (_) {}
      }
    }

    // Non-JSON: trust line-by-line DOM extraction only. Do NOT invent splits.
    return { lang: lang || "", body: b };
  }

  function fence(lang, body) {
    const formatted = formatCodeBody(lang, body);
    const l = formatted.lang || "";
    const b = formatted.body.replace(/\n$/, "");
    // ALWAYS newline after opening fence (never ```{ )
    return "```" + l + "\n" + b + "\n```";
  }


  function readMessageText(msgEl) {
    if (!msgEl) return "";
    const root = pickContentRoot(msgEl);
    if (!root) return "";

    // React source if it looks like real markdown with newlines
    if (B.extractReactMarkdown) {
      const react = B.extractReactMarkdown(root, ["markdown", "content", "text", "source", "raw"]);
      if (react && react.length > 20) {
        const hasRealNewlines = (react.match(/\n/g) || []).length >= 2;
        const hasFence = react.includes("```");
        if (hasRealNewlines || hasFence) return cleanResponse(react);
      }
    }

    const codes = findCodeBlocks(root);

    // Build shell without code blocks, inject proper fences
    const clone = root.cloneNode(true);
    if (B.restoreMath) B.restoreMath(clone);
    // Mark matching pres in clone by order
    const cloneBlocks = clone.querySelectorAll(
      "pre, [class*='code-block'], [class*='codeblock'], [class*='highlight']"
    );
    const outerClone = [...cloneBlocks].filter(
      (el) => ![...cloneBlocks].some((o) => o !== el && o.contains(el))
    );
    outerClone.forEach((el, i) => {
      const ph = document.createTextNode("\n\n@@QWENCODE" + i + "@@\n\n");
      if (el.parentNode) el.parentNode.replaceChild(ph, el);
    });
    clone
      .querySelectorAll("button,[role='button'],svg,[class*='line-number'],[class*='linenumber']")
      .forEach((n) => n.remove());

    let shell = B.htmlToMarkdown ? B.htmlToMarkdown(clone) : clone.textContent || "";
    // If placeholders missing, append fences at end
    let used = 0;
    shell = shell.replace(/@@QWENCODE(\d+)@@/g, (_, n) => {
      const i = parseInt(n, 10);
      used++;
      if (codes[i]) return fence(codes[i].lang, codes[i].body);
      return "";
    });
    if (used === 0 && codes.length) {
      // Placeholders lost — append all fences
      shell = shell.trim();
      for (const c of codes) shell += "\n\n" + fence(c.lang, c.body);
    }

    // If shell has a broken single-line fence from htmlToMarkdown, replace with ours
    if (codes.length) {
      shell = shell.replace(/```[a-z]*\n?[^\n`]{80,}```/gi, (broken) => {
        // Prefer first unused accurate block
        const c = codes[0];
        return c ? fence(c.lang, c.body) : broken;
      });
    }

    if (!shell.trim()) shell = root.innerText || "";
    return cleanResponse(shell);
  }

  /**
   * Locate the message-level Copy control.
   * Primary class used by chat.qwen.ai (see scrapeless / live UI): .copy-response-button
   * Also match aria / title / text for resilience when class hashes change.
   */
  function findCopyButton(root) {
    if (!root) root = document;
    const sels = [
      ".copy-response-button",
      "button.copy-response-button",
      '[class*="copy-response"]',
      'button[aria-label*="Copy" i]',
      'button[aria-label*="复制"]',
      'button[title*="Copy" i]',
      'button[title*="复制"]',
      '[class*="copy"][role="button"]',
      "button[class*='copy']",
    ];
    for (const s of sels) {
      try {
        const nodes = root.querySelectorAll(s);
        for (let i = nodes.length - 1; i >= 0; i--) {
          const n = nodes[i];
          if (!B.isVisible(n)) continue;
          const label = (
            (n.getAttribute("aria-label") || "") +
            " " +
            (n.title || "") +
            " " +
            (n.innerText || n.textContent || "")
          ).toLowerCase();
          // Prefer real copy controls; skip regenerate / like / etc.
          if (/regenerat|retry|like|dislike|share|edit|stop|download|json|think/i.test(label)) continue;
          if (s.includes("copy") || /copy|copier|复制|clipboard/.test(label)) return n;
        }
      } catch (_) {}
    }
    // Text-based last resort inside the message
    return B.findByText
      ? B.findByText(["button", "[role='button']"], [/copy/i, /copier/i, /复制/], root)
      : null;
  }

  /**
   * Clipboard path = same data the user gets when clicking Copy.
   * MUST prefer clipboard when capture succeeds — DOM path loses indentation on Qwen.
   * Filter known Qwen bug that sometimes writes "[object Object]".
   */
  async function extractPremium(msgEl) {
    if (!msgEl) return "";
    // The DOM extraction is only a SAFETY HINT for the clipboard path
    // (rejecting a clipboard payload that is the user's prompt): it must not
    // cost a full markdown rebuild before every answer, which is what the
    // code did — the clipboard usually succeeds and the rebuild was wasted.
    const domHint = readCheapText(msgEl);

    if (!B.premiumEnabled()) return readMessageText(msgEl);

    // Ensure MAIN-world hook is live before clicking (CSP-safe path via background)
    try {
      if (B.ensurePageClipboardHook) B.ensurePageClipboardHook();
    } catch (_) {}

    const copyBtn = findCopyButton(msgEl);
    const copyRoot = copyBtn
      ? copyBtn.closest(
          ".qwen-chat-message-assistant, .chat-response-message, [class*='message'], [class*='response']"
        ) ||
        copyBtn.parentElement ||
        msgEl
      : msgEl;

    // Attempt clipboard up to 3× — first click may race with React mount of the footer
    for (let attempt = 0; attempt < 3; attempt++) {
      const fromClip = await B.tryClipboardFromCopyButton(
        copyRoot,
        [
          ".copy-response-button",
          "button.copy-response-button",
          '[class*="copy-response"]',
          'button[aria-label*="Copy" i]',
          'button[aria-label*="复制"]',
          'button[title*="Copy" i]',
          "button[class*='copy']",
        ],
        { domAssistantText: domHint }
      );
      if (
        fromClip &&
        fromClip.length > 10 &&
        fromClip !== "[object Object]" &&
        !/^\[object\s+\w+\]$/i.test(fromClip.trim())
      ) {
        // Clipboard is the source of truth for structured content (code, lists, tables).
        // Never demote it because DOM invented more ``` fences.
        return cleanResponse(fromClip);
      }
      await B.sleep(180);
    }

    // React fiber (often holds original markdown)
    const root = pickContentRoot(msgEl) || msgEl;
    if (B.extractReactMarkdown) {
      const react = B.extractReactMarkdown(root, ["markdown", "content", "text", "source", "raw"]);
      if (
        react &&
        react.length > 20 &&
        ((react.match(/\n/g) || []).length >= 2 || react.includes("```"))
      ) {
        return cleanResponse(react);
      }
    }

    return readMessageText(msgEl) || domHint;
  }

  function log(msg) {
    try {
      console.log("[NemApi/qwen] " + msg);
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
    return B.findByText(["button", "[role='button']"], [/send/i, /submit/i], root);
  }

  /**
   * Cheap read of one answer: what the polling loops use.
   *
   * `readMessageText` clones the whole answer, rebuilds every code block and
   * runs htmlToMarkdown: it is O(answer). Polling it twice a second (what the
   * provider did before v5.4) was the main cost of the extension AND the
   * reason this tab felt slow while a job ran: the page shares its only main
   * thread with us.
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

  function getLastResponse() {
    const els = getMessageEls();
    if (els.length) return readMessageText(els[els.length - 1]);
    return "";
  }

  function lastMessageEl() {
    const els = getMessageEls();
    return els.length ? els[els.length - 1] : null;
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
        "Qwen: the prompt could not be written into the composer (the page keeps clearing it)"
      );
    }
    await B.clickSend({ input: res.input, btn: res.btn, findSend, text, log: (m) => log(m) });
  }

  function isGenerating() {
    // Stop button visible, or last assistant message still lacks a copy footer
    const stop =
      document.querySelector('button[aria-label*="Stop" i]') ||
      document.querySelector('button[aria-label*="停止"]');
    if (stop && B.isVisible(stop)) return true;
    const els = getMessageEls();
    if (!els.length) return false;
    const last = els[els.length - 1];
    // Stream finished once the per-message copy control is mounted
    if (findCopyButton(last)) return false;
    // Still streaming if content is short / changing and no copy yet
    // textContent: innerText would force a layout on every poll
    const t = (last.textContent || "").trim();
    if (t.length < 8) return true;
    const busy = last.querySelector('[class*="streaming"], [class*="generating"]');
    return !!(busy && B.isVisible(busy));
  }

  async function waitForResponse(previous = "") {
    // No leading sleep any more: the wait itself watches for the new bubble
    // from the very first tick (waitForGenerationStart already confirmed that
    // the answer started). A flat 900 ms here was paid by every request.
    await B.waitForNewResponse(getMessageEls, readMessageText, {
      timeout: 180000,
      stableMs: 2200,
      previous,
      newElTimeout: 25000,
      readCheap: readCheapText,
    });
    const last = lastMessageEl();
    if (!last) return "";
    // The action footer (.copy-response-button) mounts just after the stream
    // ends: a short bounded wait, not 2.4 s of polling on every answer.
    for (let i = 0; i < 5; i++) {
      if (findCopyButton(last)) break;
      await B.sleep(120);
    }
    try {
      return await extractPremium(last);
    } catch (_) {
      return readMessageText(last);
    }
  }

  global.NemApiProviders = global.NemApiProviders || {};
  
  function getResponseCount() {
    try {
      return getMessageEls().length;
    } catch (_) {
      return 0;
    }
  }

  global.NemApiProviders.qwen = {
    id: "qwen",
    match: (url) => /chat\.qwen\.ai|qianwen\.com/i.test(url),
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
