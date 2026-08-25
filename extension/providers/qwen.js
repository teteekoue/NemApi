/**
 * NemApi – Qwen adapter
 * Code blocks: extract LINE BY LINE (Qwen highlighter collapses textContent).
 * Language label is read from header, never concatenated into body.
 */
(function (global) {
  "use strict";
  const B = global.NemApiBase;

  const INPUT = [
    "#chat-input",
    '[contenteditable="true"][role="textbox"]',
    'textarea[role="textbox"]',
    "textarea.message-input-textarea",
    "textarea.text-area-box-web",
    'textarea[placeholder*="Ask" i]',
    'textarea[placeholder*="message" i]',
    "textarea",
  ];

  const SEND = [
    'button[type="submit"]',
    "button.send-button",
    'button[aria-label*="Send" i]',
    'button[class*="send"]',
    '[role="button"][aria-label*="Send" i]',
  ];

  const MESSAGE = [
    ".qwen-chat-message-assistant",
    "[class*='assistant'] [class*='message']",
    "[class*='assistant']",
  ];

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
      .replace(/(JSON|Copy|Download|Think)\s*[:：]?\s*/gi, "")
      .trim();
    // Fix broken fences where language stuck to first token: ```\npythonimport → ```python\nimport
    t = t.replace(/```\s*\n?(python|javascript|js|ts|typescript|bash|sh|json|html|css|java|go|rust|sql|yaml|yml|cpp|c\+\+|csharp|php|ruby)(?=[a-zA-Z_])/gi, "```$1\n");
    if (B.cleanMarkdownCodeFences) t = B.cleanMarkdownCodeFences(t);
    t = dedupeRepeatedContent(t);
    return t;
  }

  function pickContentRoot(msgEl) {
    if (!msgEl) return null;
    const selectors = [".qwen-markdown", ".markdown-body", ".response-message-content", "[class*='markdown']"];
    const candidates = [];
    for (const s of selectors) {
      msgEl.querySelectorAll(s).forEach((el) => {
        if (el.closest("[class*='think'], [class*='reason'], [class*='reflect'], [class*='status-card']")) return;
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

  async function extractPremium(msgEl) {
    if (!msgEl) return "";
    if (B.premiumEnabled()) {
      const root = pickContentRoot(msgEl) || msgEl;
      const react = B.extractReactMarkdown(root, ["markdown", "content", "text", "source", "raw"]);
      if (react && react.length > 20 && ((react.match(/\n/g) || []).length >= 2 || react.includes("```"))) {
        return cleanResponse(react);
      }
      const fromDom = readMessageText(msgEl);
      if (fromDom && fromDom.length > 10) return fromDom;
    }
    return readMessageText(msgEl);
  }

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

  function getLastResponse() {
    const els = getMessageEls();
    if (els.length) return readMessageText(els[els.length - 1]);
    return "";
  }

  function inputHasText(el, expected) {
    if (!el) return false;
    const sample = String(expected || "").slice(0, 40);
    if ("value" in el) {
      const v = el.value || "";
      return sample ? v.includes(sample) : v.length > 0;
    }
    const t = el.innerText || el.textContent || "";
    return sample ? t.includes(sample) : t.trim().length > 0;
  }

  async function sendPrompt(text) {
    const payload = String(text ?? "");
    let input = findInput();
    if (!input) {
      try {
        input = await B.waitFor("#chat-input, textarea, [contenteditable='true']", 12000);
      } catch (_) {
        input = findInput();
      }
    }
    if (!input) throw new Error("Qwen: input not found");

    // Wait until input is interactable (after previous reply finished)
    for (let i = 0; i < 25; i++) {
      const disabled =
        input.disabled ||
        input.getAttribute("aria-disabled") === "true" ||
        input.getAttribute("contenteditable") === "false";
      if (!disabled && B.isVisible(input)) break;
      await B.sleep(200);
      input = findInput() || input;
    }

    // Paste with verification + retry (2nd+ message in same thread often needs this)
    let pasted = false;
    for (let attempt = 1; attempt <= 3; attempt++) {
      input = findInput() || input;
      try {
        input.focus();
        input.click();
      } catch (_) {}
      await B.sleep(80);
      B.pasteText(input, payload);
      await B.sleep(200);
      if (inputHasText(input, payload)) {
        pasted = true;
        break;
      }
      await B.sleep(250);
    }
    if (!pasted) {
      // Last resort: native value + events
      try {
        if ("value" in input) {
          input.value = payload;
          input.dispatchEvent(new Event("input", { bubbles: true }));
        } else {
          input.textContent = payload;
          input.dispatchEvent(new Event("input", { bubbles: true }));
        }
      } catch (_) {}
      await B.sleep(150);
    }

    // Wait for Send to enable
    let btn = null;
    for (let i = 0; i < 20; i++) {
      btn = findSend();
      if (btn && !btn.disabled && btn.getAttribute("aria-disabled") !== "true") break;
      await B.sleep(100);
      btn = null;
    }
    if (btn) {
      B.clickEl(btn);
      return;
    }
    B.pressEnter(input);
    await B.sleep(60);
    B.pressEnter(input);
  }

  async function waitForResponse(previous = "") {
    await B.sleep(900);
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
  global.NemApiProviders.qwen = {
    id: "qwen",
    match: (url) => /chat\.qwen\.ai|qianwen\.com/i.test(url),
    sendPrompt,
    waitForResponse,
    getLastResponse,
    findInput,
  };
})(typeof window !== "undefined" ? window : self);
