/**
 * NemApi – shared DOM helpers for provider adapters
 */
(function (global) {
  "use strict";

  function sleep(ms) {
    return new Promise((r) => setTimeout(r, ms));
  }

  function waitFor(selector, timeout = 12000) {
    return new Promise((resolve, reject) => {
      const t0 = Date.now();
      const tick = () => {
        const el = document.querySelector(selector);
        if (el) return resolve(el);
        if (Date.now() - t0 > timeout) return reject(new Error("Timeout: " + selector));
        setTimeout(tick, 120);
      };
      tick();
    });
  }

  function queryFirst(selectors) {
    for (const s of selectors) {
      try {
        const el = document.querySelector(s);
        if (el) return el;
      } catch (_) {}
    }
    return null;
  }

  function queryAll(selectors, root = document) {
    const out = [];
    for (const s of selectors) {
      try {
        out.push(...root.querySelectorAll(s));
      } catch (_) {}
    }
    return out;
  }

  function isVisible(el) {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    return !!(style && style.display !== "none" && style.visibility !== "hidden" && style.opacity !== "0");
  }

  function textOf(el) {
    return (el && (el.innerText || el.textContent || "") || "").trim();
  }

  function findByText(selectors, patterns, root = document) {
    const els = queryAll(selectors, root);
    for (const el of els) {
      const label = `${el.getAttribute("aria-label") || ""} ${el.title || ""} ${textOf(el)}`.toLowerCase();
      if (patterns.some((re) => re.test(label)) && isVisible(el)) return el;
    }
    return null;
  }

  function findComposerRoot(el) {
    let node = el || document.activeElement || document.body;
    for (let i = 0; i < 5 && node; i += 1) {
      if (node.matches && node.matches("form, main, [role='main'], [class*='composer'], [class*='input'], [class*='prompt'], [class*='chat']")) {
        return node;
      }
      node = node.parentElement;
    }
    return document.body;
  }

  /** React-friendly textarea value set (works on 1st and Nth message in same chat). */
  function setTextareaValue(el, text) {
    const value = String(text ?? "");
    try {
      el.focus();
      el.click();
    } catch (_) {}
    // Clear first so React controlled inputs accept a fresh value
    const proto = window.HTMLTextAreaElement.prototype;
    const desc = Object.getOwnPropertyDescriptor(proto, "value");
    const setVal = (v) => {
      if (desc && desc.set) desc.set.call(el, v);
      else el.value = v;
    };
    setVal("");
    el.dispatchEvent(new Event("input", { bubbles: true }));
    setVal(value);
    try {
      el.dispatchEvent(
        new InputEvent("beforeinput", {
          bubbles: true,
          cancelable: true,
          inputType: "insertFromPaste",
          data: value,
        })
      );
    } catch (_) {}
    try {
      el.dispatchEvent(
        new InputEvent("input", { bubbles: true, inputType: "insertFromPaste", data: value })
      );
    } catch (_) {
      el.dispatchEvent(new Event("input", { bubbles: true }));
    }
    el.dispatchEvent(new Event("change", { bubbles: true }));
    // Some React builds only listen to keyup/keydown to enable Send
    try {
      el.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, key: "a", code: "KeyA" }));
      el.dispatchEvent(new KeyboardEvent("keyup", { bubbles: true, key: "a", code: "KeyA" }));
    } catch (_) {}
    try {
      el.style.height = "auto";
      el.style.height = el.scrollHeight + "px";
    } catch (_) {}
    // Final assert-friendly assignment
    if (el.value !== value) {
      setVal(value);
      el.dispatchEvent(new Event("input", { bubbles: true }));
    }
  }

  /** ProseMirror / contenteditable — clear then insert (Nth message safe). */
  function setContentEditable(el, text) {
    const value = String(text ?? "");
    try {
      el.focus();
      el.click();
    } catch (_) {}
    try {
      document.execCommand("selectAll", false, null);
      document.execCommand("delete", false, null);
    } catch (_) {
      el.textContent = "";
    }
    let ok = false;
    try {
      ok = document.execCommand("insertText", false, value);
    } catch (_) {}
    if (!ok || !(el.textContent || "").includes(value.slice(0, Math.min(20, value.length)))) {
      el.textContent = value;
    }
    try {
      el.dispatchEvent(
        new InputEvent("beforeinput", {
          bubbles: true,
          cancelable: true,
          inputType: "insertFromPaste",
          data: value,
        })
      );
    } catch (_) {}
    el.dispatchEvent(new Event("input", { bubbles: true }));
    try {
      el.dispatchEvent(
        new InputEvent("input", { bubbles: true, inputType: "insertFromPaste", data: value })
      );
    } catch (_) {}
  }

  /** Quill (Gemini) */
  function setQuill(container, text) {
    const editor =
      container.classList && container.classList.contains("ql-editor")
        ? container
        : container.querySelector(".ql-editor") || container;
    editor.focus();
    const escaped = text
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .split("\n")
      .map((l) => `<p>${l || "<br>"}</p>`)
      .join("");
    editor.innerHTML = escaped;
    editor.dispatchEvent(new InputEvent("beforeinput", { bubbles: true, cancelable: true, inputType: "insertFromPaste", data: text }));
    editor.dispatchEvent(new Event("input", { bubbles: true }));
  }

  function clickEl(el) {
    if (!el) return false;
    try {
      el.click();
      return true;
    } catch (_) {
      return false;
    }
  }

  function pressEnter(el) {
    const opts = { bubbles: true, cancelable: true, key: "Enter", code: "Enter", keyCode: 13, which: 13 };
    el.dispatchEvent(new KeyboardEvent("keydown", opts));
    el.dispatchEvent(new KeyboardEvent("keypress", opts));
    el.dispatchEvent(new KeyboardEvent("keyup", opts));
  }

  function setNativeValue(el, text) {
    if (!el) return;
    if ("value" in el) {
      const proto = Object.getPrototypeOf(el);
      const desc = Object.getOwnPropertyDescriptor(proto, "value") || Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value") || Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value");
      if (desc && desc.set) desc.set.call(el, text);
      else el.value = text;
      return;
    }
    el.textContent = text;
  }

  /** Insert the complete prompt in one DOM update (never simulated typing). */
  function pasteText(el, text) {
    if (!el) throw new Error("No input element");
    if ("value" in el) return setTextareaValue(el, String(text ?? ""));
    if (el.classList && (el.classList.contains("ql-editor") || el.classList.contains("ql-container")) || el.querySelector?.(".ql-editor")) {
      return setQuill(el, String(text ?? ""));
    }
    return setContentEditable(el, String(text ?? ""));
  }

  /**
   * Wait until text stabilizes (streaming finished).
   * getText() returns current assistant text; done when unchanged for stableMs.
   * Virtual-list safe: ignores previous answer and truncated prefixes of it.
   */
  async function waitUntilStable(getText, { timeout = 180000, stableMs = 1800, pollMs = 400, previous = "" } = {}) {
    const t0 = Date.now();
    const prev = (previous || "").trim();
    const prevFp = prev ? prev.length + ":" + prev.slice(0, 48) + ":" + prev.slice(-48) : "";
    let last = "";
    let lastFp = "";
    let stableSince = Date.now();
    while (Date.now() - t0 < timeout) {
      if (global.__NEMAPI_STOP__) throw new Error("Stopped");
      let cur = "";
      try {
        cur = (getText() || "").trim();
      } catch (_) {}
      // Ignore the last answer that was already present before this job, and
      // any partial render of it. Virtualized lists (DeepSeek/Qwen) re-render
      // old messages as a truncated prefix of the full text.
      if (cur && prev) {
        if (cur === prev) cur = "";
        else if (cur.length < prev.length && prev.startsWith(cur)) cur = "";
        else if (prev.length < cur.length && cur.startsWith(prev) && cur.length - prev.length < 8) {
          // still essentially the old message with trivial growth
          cur = "";
        } else {
          const fp = cur.length + ":" + cur.slice(0, 48) + ":" + cur.slice(-48);
          if (fp === prevFp) cur = "";
        }
      }
      const fp = cur ? cur.length + ":" + cur.slice(0, 48) + ":" + cur.slice(-48) : "";
      if (cur && fp === lastFp && cur.length > 0) {
        if (Date.now() - stableSince >= stableMs) return cur;
      } else {
        last = cur;
        lastFp = fp;
        stableSince = Date.now();
      }
      await sleep(pollMs);
    }
    if (last) return last;
    throw new Error("Timeout waiting for response");
  }

  function lastOf(arr) {
    return arr && arr.length ? arr[arr.length - 1] : null;
  }

  function textFingerprint(text) {
    const t = (text || "").trim();
    if (!t) return "";
    return t.length + ":" + t.slice(0, 64) + ":" + t.slice(-64);
  }

  /**
   * Wait for a NEW assistant message (new DOM node OR new fingerprint on the
   * last node — critical for virtualized lists that reuse elements).
   * getMessageEls() returns assistant message elements in DOM order;
   * readText(el) extracts the assistant answer text from a message element.
   */
  async function waitForNewResponse(getMessageEls, readText, { timeout = 180000, stableMs = 2000, pollMs = 400, previous = "", newElTimeout = 25000 } = {}) {
    const oldEls = getMessageEls() || [];
    const oldEl = lastOf(oldEls);
    const oldCount = oldEls.length;
    const oldFp = textFingerprint(previous || (oldEl ? readText(oldEl) : ""));
    const phase1End = Date.now() + newElTimeout;
    let newEl = null;
    let sawNewContent = false;

    while (Date.now() < phase1End) {
      if (global.__NEMAPI_STOP__) throw new Error("Stopped");
      const els = getMessageEls() || [];
      const last = lastOf(els);
      if (last && last !== oldEl) {
        newEl = last;
        break;
      }
      // Virtual list: same node, but message count grew or fingerprint changed
      if (last && els.length > oldCount) {
        newEl = last;
        break;
      }
      if (last) {
        let cur = "";
        try {
          cur = (readText(last) || "").trim();
        } catch (_) {}
        const fp = textFingerprint(cur);
        if (fp && fp !== oldFp && cur.length > (previous || "").length * 0.5) {
          sawNewContent = true;
          newEl = last;
          break;
        }
      }
      await sleep(pollMs);
    }

    const readLast = () => {
      const last = lastOf(getMessageEls());
      return last ? readText(last) : "";
    };
    // No new bubble detected → keep filtering against `previous` so we don't
    // return the old answer while streaming/idle.
    if (!newEl && !sawNewContent) {
      return waitUntilStable(readLast, { timeout, stableMs, pollMs, previous });
    }
    // A NEW message element (or fingerprint) was observed. Do NOT filter by
    // `previous` anymore — consecutive identical answers are valid and must
    // be delivered (same fix as Gemini bubble-count gate).
    const target = newEl;
    return waitUntilStable(() => (target ? readText(target) : readLast()), {
      timeout,
      stableMs,
      pollMs,
      previous: "",
    });
  }

  function isDisabled(el) {
    if (!el) return true;
    if (el.getAttribute("aria-disabled") === "true") return true;
    if (el.disabled) return true;
    const cls = (el.className && String(el.className)) || "";
    if (/\bdisabled\b/i.test(cls)) return true;
    return false;
  }

  /**
   * Wait until a send button becomes enabled after pasting text.
   * findSendFn() must return the current candidate button or null.
   */
  async function waitForEnabledSend(findSendFn, timeoutMs = 3500) {
    const t0 = Date.now();
    while (Date.now() - t0 < timeoutMs) {
      const btn = findSendFn();
      if (btn && !isDisabled(btn) && isVisible(btn)) return btn;
      await sleep(80);
    }
    return findSendFn();
  }

  /* ------------------------------------------------------------------ */
  /* Premium markdown extraction (no physical clicks)                    */
  /* ------------------------------------------------------------------ */

  function getReactFiber(el) {
    if (!el) return null;
    const key = Object.keys(el).find(
      (k) => k.startsWith("__reactFiber$") || k.startsWith("__reactInternalInstance$")
    );
    return key ? el[key] : null;
  }

  /** Navigate fiber path like "$0.return.return.return" (DeepSeek exporter). */
  function navigateFiberPath(el, pathStr) {
    let f = getReactFiber(el);
    if (!f || !pathStr) return null;
    const steps = String(pathStr).replace(/^\$0\.?/, "").split(".").filter(Boolean);
    for (const step of steps) {
      if (!f) return null;
      f = f[step];
    }
    return f || null;
  }

  /**
   * Read original markdown from React state — same source the Copy button uses.
   * DeepSeek: memoizedProps.markdown on .ds-markdown fiber.
   * No physical/synthetic click required.
   */
  function extractReactMarkdown(el, propNames) {
    if (!el) return "";
    const names = propNames || [
      "markdown", "content", "text", "rawContent", "source",
      "message", "value", "answer", "response", "children",
    ];

    // Proven DeepSeek paths (Chat Exporter)
    const knownPaths = [
      "$0.return.return.return",
      "$0.return.return",
      "$0.return",
      "$0.child.return",
      "$0.child.child.return",
    ];
    for (const path of knownPaths) {
      try {
        const fiber = navigateFiberPath(el, path);
        const props = fiber && (fiber.memoizedProps || fiber.pendingProps);
        if (props && typeof props.markdown === "string" && props.markdown.trim().length > 5) {
          return props.markdown.trim();
        }
      } catch (_) {}
    }

    const startFiber = getReactFiber(el);
    if (!startFiber) return "";
    const seen = new Set();
    const queue = [startFiber];
    let best = "";
    let bestScore = -1;
    let depth = 0;
    while (queue.length && depth < 120) {
      const f = queue.shift();
      depth += 1;
      if (!f || seen.has(f)) continue;
      seen.add(f);
      const props = f.memoizedProps || f.pendingProps;
      if (props && typeof props === "object") {
        for (const name of names) {
          if (!Object.prototype.hasOwnProperty.call(props, name)) continue;
          let v = props[name];
          if (v && typeof v === "object" && typeof v.markdown === "string") v = v.markdown;
          if (typeof v !== "string") continue;
          const s = v.trim();
          if (s.length < 3) continue;
          let score = s.length;
          if (name === "markdown") score += 100000;
          if (s.includes("```")) score += 8000;
          if (s.includes("\n")) score += 400;
          if (/^#{1,6}\s/m.test(s)) score += 500;
          if (score > bestScore) {
            bestScore = score;
            best = s;
          }
        }
      }
      if (f.child) queue.push(f.child);
      if (f.sibling) queue.push(f.sibling);
      if (f.return && depth < 60) queue.push(f.return);
    }
    return best;
  }

  /**
   * Convert a rendered markdown HTML subtree back to reasonably faithful MD.
   * Handles pre/code, headings, lists, tables, links, emphasis.
   */
  /**
   * Remove UI line-number gutters that leak into code text, e.g.:
   *   "1| def foo():"  or  "  12  const x = 1"
   */
  function stripLeadingLineNumbers(text) {
    const lines = String(text || "").split("\n");
    if (lines.length < 2) return String(text || "");
    // Pattern A: "1| code" or "1: code" or "1 code" at start of many lines
    let numbered = 0;
    for (const line of lines) {
      if (/^\s*\d+\s*[|：:.\)]\s/.test(line) || /^\s*\d{1,4}\s{2,}\S/.test(line)) numbered += 1;
    }
    if (numbered < Math.min(3, Math.ceil(lines.length * 0.4))) {
      // Not a line-number gutter — leave as-is
      return String(text || "");
    }
    return lines
      .map((line) =>
        line
          .replace(/^\s*\d+\s*[|：:.\)]\s?/, "")
          .replace(/^\s*\d{1,4}\s{2,}/, "")
      )
      .join("\n");
  }

  /** Strip line numbers from every fenced code block in a markdown string. */
  function cleanMarkdownCodeFences(md) {
    return String(md || "").replace(/```([^\n`]*)\n([\s\S]*?)```/g, (full, lang, body) => {
      return "```" + lang + "\n" + stripLeadingLineNumbers(body).replace(/\n$/, "") + "\n```";
    });
  }

  function htmlToMarkdown(root) {
    if (!root) return "";
    const clone = root.cloneNode(true);
    const kill = [
      "button",
      "[role='button']",
      "[class*='copy']",
      "[class*='download']",
      "[class*='toolbar']",
      "[class*='action-bar']",
      "[class*='icon-button']",
      "[class*='line-number']",
      "[class*='linenumber']",
      "[class*='line-num']",
      ".linenumber",
      ".line-numbers",
      "[data-line-number]",
      "svg",
      "style",
      "script",
      "noscript",
    ];
    for (const s of kill) {
      try {
        clone.querySelectorAll(s).forEach((n) => n.remove());
      } catch (_) {}
    }

    function walk(node) {
      if (!node) return "";
      if (node.nodeType === 3) return node.textContent || "";
      if (node.nodeType !== 1) return "";
      const tag = node.tagName.toLowerCase();
      if (tag === "pre") {
        // Remove gutter / line-number nodes before reading text
        node.querySelectorAll(
          "[class*='line-number'],[class*='linenumber'],[class*='line-num'],.line-numbers,[data-line-number],.code-block-gutter,.cm-gutter,.linenos"
        ).forEach((n) => n.remove());
        const code = node.querySelector("code");
        let lang = "";
        if (code) {
          const cls = code.className || "";
          const m = cls.match(/language-([a-z0-9_+-]+)/i) || cls.match(/lang-([a-z0-9_+-]+)/i);
          if (m) lang = m[1];
        }
        // Prefer textContent (preserves spaces). Avoid innerText (can collapse).
        let body = (code ? code.textContent : node.textContent) || "";
        body = body.replace(/\u00a0/g, " "); // nbsp → space
        body = stripLeadingLineNumbers(body);
        return "\n\n```" + lang + "\n" + body.replace(/\n$/, "") + "\n```\n\n";
      }
      if (tag === "code" && (!node.parentElement || node.parentElement.tagName.toLowerCase() !== "pre")) {
        return "`" + (node.textContent || "").replace(/`/g, "\\`") + "`";
      }
      if (/^h[1-6]$/.test(tag)) {
        const level = Number(tag[1]);
        return "\n\n" + "#".repeat(level) + " " + Array.from(node.childNodes).map(walk).join("").trim() + "\n\n";
      }
      if (tag === "strong" || tag === "b") {
        return "**" + Array.from(node.childNodes).map(walk).join("") + "**";
      }
      if (tag === "em" || tag === "i") {
        return "*" + Array.from(node.childNodes).map(walk).join("") + "*";
      }
      if (tag === "a") {
        const href = node.getAttribute("href") || "";
        const label = Array.from(node.childNodes).map(walk).join("") || href;
        return href ? "[" + label + "](" + href + ")" : label;
      }
      if (tag === "li") {
        return "\n- " + Array.from(node.childNodes).map(walk).join("").trim();
      }
      if (tag === "ul" || tag === "ol") {
        return "\n" + Array.from(node.childNodes).map(walk).join("") + "\n";
      }
      if (tag === "br") return "\n";
      if (tag === "p" || tag === "div") {
        // Detect code-line containers (Qwen/Claude often use div.line without <pre>)
        const cls = (node.className && String(node.className)) || "";
        const isCodeLine =
          /\b(line|code-line|hljs|cm-line|token-line)\b/i.test(cls) ||
          node.getAttribute("data-line") != null;
        if (isCodeLine) {
          // Preserve exact indentation — never trim leading spaces
          const lineText = node.textContent || "";
          return lineText.replace(/\r/g, "") + "\n";
        }
        // Whole code-block wrapper without a pre
        if (/\b(code-block|codeblock|highlight|hljs|prism|cm-content|monaco)\b/i.test(cls)) {
          node.querySelectorAll(
            "[class*='line-number'],[class*='linenumber'],[class*='line-num'],.line-numbers,[data-line-number],button,svg"
          ).forEach((n) => n.remove());
          let body = node.textContent || "";
          body = stripLeadingLineNumbers(body);
          // If children already walked would double — use textContent once
          return "\n\n```\n" + body.replace(/\n$/, "") + "\n```\n\n";
        }
        const inner = Array.from(node.childNodes).map(walk).join("");
        if (!inner || !inner.replace(/\s/g, "")) return "";
        // Preserve internal indentation: only strip blank edges, not per-line indent
        return "\n\n" + inner.replace(/^\n+|\n+$/g, "") + "\n\n";
      }
      if (tag === "blockquote") {
        const inner = Array.from(node.childNodes).map(walk).join("").trim();
        return "\n\n" + inner.split("\n").map((l) => "> " + l).join("\n") + "\n\n";
      }
      if (tag === "table") {
        // crude table → markdown
        const rows = Array.from(node.querySelectorAll("tr"));
        if (!rows.length) return "";
        const cells = rows.map((tr) =>
          Array.from(tr.querySelectorAll("th,td")).map((c) => (c.textContent || "").trim().replace(/\|/g, "\\|"))
        );
        const header = cells[0];
        const sep = header.map(() => "---");
        const lines = [
          "| " + header.join(" | ") + " |",
          "| " + sep.join(" | ") + " |",
          ...cells.slice(1).map((r) => "| " + r.join(" | ") + " |"),
        ];
        return "\n\n" + lines.join("\n") + "\n\n";
      }
      return Array.from(node.childNodes).map(walk).join("");
    }

    let md = walk(clone);
    md = md.replace(/[ \t]+\n/g, "\n");
    md = md.replace(/\n{3,}/g, "\n\n");
    return md.trim();
  }

  /**
   * Install a MAIN-world clipboard hook so page JS (Qwen/Claude/…) writeText calls
   * are visible to the isolated content-script world.
   * Content-script patches of navigator.clipboard do NOT affect the page realm —
   * that was why clipboard intercept silently failed while the real OS clipboard
   * still received the text.
   */
  function ensurePageClipboardHook() {
    if (global.__NEMAPI_PAGE_CLIP_READY__) return;
    global.__NEMAPI_PAGE_CLIP_READY__ = true;
    global.__NEMAPI_LAST_CLIP__ = "";

    // Content-script side: receive captures from the page world
    window.addEventListener(
      "__nemapi_clip_capture__",
      (e) => {
        try {
          const t = e && e.detail != null ? String(e.detail) : "";
          if (t && t !== "[object Object]" && t.length >= (global.__NEMAPI_LAST_CLIP__ || "").length) {
            global.__NEMAPI_LAST_CLIP__ = t;
          }
        } catch (_) {}
      },
      true
    );

    // CSP blocks inline <script> injection on ChatGPT/Kimi/etc.
    // Only use chrome.scripting world:MAIN via the background service worker.
    try {
      chrome.runtime.sendMessage({ action: "installPageClipHook" }).catch(() => {});
    } catch (_) {}
  }

  /**
   * Try synthetic click on a Copy button while intercepting the page's clipboard writes.
   * Returns markdown string or "".
   *
   * Strategy (in order):
   *  1) MAIN-world hook (page navigator.clipboard.writeText) via CustomEvent
   *  2) Isolated-world writeText patch (works only if page shares the same object — rare)
   *  3) document "copy" event
   *  4) navigator.clipboard.readText() after the click (needs clipboardRead permission)
   */
  /**
   * True if the button is a *code-block* copy control (not the full-message one).
   */
  function isCodeBlockCopyButton(el) {
    if (!el) return false;
    if (
      el.closest(
        "pre, code, .md-code-block, [class*='md-code'], [class*='code-block'], " +
          "[class*='CodeBlock'], [class*='codeblock'], [class*='hljs'], " +
          "[class*='syntax'], [data-code-block], .ds-code-block"
      )
    ) {
      return true;
    }
    const label = (
      (el.getAttribute("aria-label") || "") +
      " " +
      (el.title || "") +
      " " +
      (el.innerText || el.textContent || "")
    ).toLowerCase();
    if (/copy\s*code|copy\s*snippet|复制代码|复制片段|copy\s*block/i.test(label)) {
      return true;
    }
    const parent = el.parentElement;
    if (parent) {
      const siblings = parent.querySelectorAll(
        "button, [role='button'], .ds-icon-button, [class*='icon-button']"
      );
      if (siblings.length <= 2 && el.closest("pre, [class*='code']")) return true;
    }
    return false;
  }

  /**
   * True if the element lives inside a *user* / human prompt bubble.
   * Critical: never click a copy button that belongs to the user message.
   */
  function isUserMessageContext(el) {
    if (!el) return false;
    if (
      el.closest(
        // Generic
        '[data-role="user"], [data-message-author-role="user"], [data-message-author-role="human"], ' +
          '[data-testid*="user-message" i], [data-testid*="user_message" i], ' +
          // ChatGPT
          '[data-message-author-role="user"], ' +
          // Gemini
          "user-query, .user-query, .query-text, [class*='user-query'], [class*='query-content'], " +
          // Claude
          '[data-testid="user-message"], .font-user-message, ' +
          // DeepSeek / Qwen / Kimi / Z.ai class heuristics
          "[class*='user-message'], [class*='human-message'], [class*='UserMessage'], " +
          "[class*='user_message'], [class*='prompt-message'], [class*='question-message'], " +
          // Role attributes
          '[role="user"], [data-author="user"], [data-author="human"]'
      )
    ) {
      return true;
    }
    // Aria / data labels on ancestors
    let n = el;
    for (let i = 0; i < 8 && n; i++) {
      const role = (
        (n.getAttribute && (n.getAttribute("data-role") || n.getAttribute("data-message-author-role") || "")) +
        " " +
        (n.getAttribute && (n.getAttribute("aria-label") || "")) +
        " " +
        (n.className && String(n.className)) ||
        ""
      ).toLowerCase();
      if (/\b(user|human|prompt|query|question)\b/.test(role) && !/\b(assistant|model|bot|ai|response|answer)\b/.test(role)) {
        // Strong user signal without assistant signal
        if (/\b(user-message|human-message|user-query|data-role.?=.?user|author-role.?=.?user)\b/.test(role)) {
          return true;
        }
      }
      n = n.parentElement;
    }
    return false;
  }

  /**
   * True if the element lives inside an *assistant* / model response bubble.
   */
  function isAssistantMessageContext(el) {
    if (!el) return false;
    if (
      el.closest(
        '[data-role="assistant"], [data-message-author-role="assistant"], [data-message-author-role="model"], ' +
          '[data-testid*="assistant" i], [data-testid*="model-response" i], ' +
          "model-response, .model-response, .model-response-text, message-content.model-response-text, " +
          '[data-testid="assistant-message"], .font-claude-message, ' +
          "[class*='assistant-message'], [class*='bot-message'], [class*='model-message'], " +
          "[class*='ai-message'], [class*='response-message'], .ds-message, " +
          "[class*='AgentMessage'], [class*='AssistantMessage']"
      )
    ) {
      return true;
    }
    return false;
  }

  /**
   * Score a candidate copy button: higher = more likely the *assistant message* copy.
   * Strongly rejects user-message and code-block buttons.
   * Prefers toolbars with regenerate / like / dislike / share.
   */
  function scoreMessageCopyButton(el) {
    if (!el || !isVisible(el) || isCodeBlockCopyButton(el)) return -1;
    // Hard reject: belongs to a user/human prompt bubble
    if (isUserMessageContext(el) && !isAssistantMessageContext(el)) return -1;

    let score = 10;
    const label = (
      (el.getAttribute("aria-label") || "") +
      " " +
      (el.title || "") +
      " " +
      (el.innerText || el.textContent || "") +
      " " +
      (el.getAttribute("data-testid") || "")
    ).toLowerCase();

    // Explicit assistant/response copy labels
    if (
      /copy\s*(response|message|answer|full|prompt)?|copier\s*(la\s*)?(réponse|message)?|复制(全部|消息|回答|回复|内容)?/i.test(
        label
      )
    ) {
      score += 40;
    }
    if (/copy-response|copy-turn|action-bar-copy|copy_response/i.test(label)) score += 60;
    if (/copy|copier|复制|clipboard/.test(label)) score += 5;

    // Reject labels that clearly mean "copy prompt / user input"
    if (/copy\s*(prompt|query|question|input)|复制(提问|问题|输入)/i.test(label)) return -1;

    if (isAssistantMessageContext(el)) score += 35;
    if (isUserMessageContext(el)) score -= 80;

    // Sibling toolbar: regenerate / like / dislike / share = assistant action bar
    let group = el.parentElement;
    for (let i = 0; i < 5 && group; i++) {
      const groupText = (group.innerText || group.textContent || "").toLowerCase();
      const groupAria = (
        (group.getAttribute && (group.getAttribute("aria-label") || "")) +
        " " +
        (group.className && String(group.className)) ||
        ""
      ).toLowerCase();
      const btns = group.querySelectorAll(
        "button, [role='button'], .ds-icon-button, [class*='icon-button']"
      );
      if (btns.length >= 3) score += 15;
      if (btns.length >= 4) score += 10;
      if (
        /regenerat|retry|重新生成|like|dislike|thumb|share|分享|点赞|点踩|good\s*response|bad\s*response|report|修改/i.test(
          groupText + " " + groupAria
        )
      ) {
        score += 40;
      }
      if (/message\s*actions|action-bar|response-actions|footer-actions/i.test(groupAria)) {
        score += 25;
      }
      group = group.parentElement;
    }
    return score;
  }

  /**
   * Collect copy-button candidates under `root`, excluding user-message /
   * code-block controls. Returns sorted [{el, score}] (best first).
   */
  function collectAssistantCopyCandidates(root, buttonSelectors) {
    const sels = buttonSelectors || [
      'button[data-testid="action-bar-copy"]',
      'button[aria-label*="Copy response" i]',
      'button[aria-label*="Copy message" i]',
      'button[aria-label*="Copy" i]',
      'button[aria-label*="Copier" i]',
      'button[aria-label*="复制"]',
      'button[title*="Copy" i]',
    ];
    if (!root) root = document.body;
    const candidates = [];
    const seen = new Set();
    for (const s of sels) {
      try {
        const nodes = root.querySelectorAll(s);
        for (const n of nodes) {
          if (seen.has(n)) continue;
          seen.add(n);
          const label = (
            (n.getAttribute("aria-label") || "") +
            " " +
            (n.title || "") +
            " " +
            (n.innerText || n.textContent || "")
          ).toLowerCase();
          if (s === "button" && !/copy|copier|复制|clipboard/.test(label)) continue;
          if (!isVisible(n)) continue;
          if (isCodeBlockCopyButton(n)) continue;
          // Hard skip pure user-message copies
          if (isUserMessageContext(n) && !isAssistantMessageContext(n)) continue;
          const sc = scoreMessageCopyButton(n);
          if (sc >= 0) candidates.push({ el: n, score: sc });
        }
      } catch (_) {}
    }
    if (!candidates.length) {
      try {
        const all = root.querySelectorAll("button, [role='button']");
        for (const n of all) {
          if (seen.has(n)) continue;
          const label = (
            (n.getAttribute("aria-label") || "") +
            " " +
            (n.title || "") +
            " " +
            (n.innerText || n.textContent || "")
          ).toLowerCase();
          if (!/copy|copier|复制|clipboard/.test(label)) continue;
          if (!isVisible(n) || isCodeBlockCopyButton(n)) continue;
          if (isUserMessageContext(n) && !isAssistantMessageContext(n)) continue;
          const sc = scoreMessageCopyButton(n);
          if (sc >= 0) candidates.push({ el: n, score: sc });
        }
      } catch (_) {}
    }
    candidates.sort((a, b) => b.score - a.score);
    return candidates;
  }

  /**
   * Heuristic: does `clip` look like the *user prompt* rather than the assistant answer?
   * Used as a safety net after clipboard intercept.
   */
  function looksLikeUserPrompt(clip, domAssistantText) {
    if (!clip || typeof clip !== "string") return false;
    const c = clip.trim();
    if (c.length < 2) return true;
    // Matches known last user-query bubbles on the page
    try {
      const userNodes = document.querySelectorAll(
        'user-query, [data-message-author-role="user"], [data-role="user"], ' +
          '[data-testid="user-message"], .user-query, [class*="user-message"], [class*="human-message"]'
      );
      for (const n of userNodes) {
        const ut = (n.innerText || n.textContent || "").trim();
        if (ut.length > 5 && (c === ut || (c.length <= ut.length + 5 && ut.startsWith(c.slice(0, Math.min(80, c.length)))))) {
          return true;
        }
        // clipboard is a short prefix of user text
        if (ut.length > 20 && c.length > 10 && ut.startsWith(c.slice(0, Math.min(c.length, 120)))) {
          return true;
        }
      }
    } catch (_) {}
    // If we have a solid DOM assistant extract, and clipboard is wildly different & shorter → suspicious
    if (domAssistantText && domAssistantText.trim().length > 40) {
      const d = domAssistantText.trim();
      if (c.length < d.length * 0.25 && !d.includes(c.slice(0, Math.min(40, c.length)))) {
        // clipboard much shorter and not contained in DOM answer → likely wrong
        return true;
      }
    }
    return false;
  }

  async function tryClipboardFromCopyButton(root, buttonSelectors, opts) {
    opts = opts || {};
    const domHint = opts.domAssistantText || "";
    ensurePageClipboardHook();
    // Give MAIN-world inject a moment on first use (message is async)
    if (!global.__NEMAPI_CLIP_HOOK_WAITED__) {
      global.__NEMAPI_CLIP_HOOK_WAITED__ = true;
      await sleep(120);
    }

    if (!root) root = document.body;
    const candidates = collectAssistantCopyCandidates(root, buttonSelectors);
    // Require a minimum score so we never click a user-message / junk button
    const btn =
      candidates.length && candidates[0].score >= 20 ? candidates[0].el : null;
    if (!btn) return "";

    // Snapshot before click so we can detect a fresh write
    const before = global.__NEMAPI_LAST_CLIP__ || "";
    global.__NEMAPI_LAST_CLIP__ = "";

    // Prefer plain/markdown over HTML. ChatGPT often writes both text/plain AND
    // text/html; HTML is longer so a naive "longest wins" picks the wrong one.
    let capturedPlain = "";
    let capturedHtml = "";
    const looksHtml = (s) =>
      /<\/?(p|div|span|strong|em|br|ul|ol|li|h[1-6]|a|code|pre|table)\b/i.test(s || "");

    const capture = (text, mimeHint) => {
      if (typeof text !== "string" || !text || text === "[object Object]") return;
      const isHtml =
        mimeHint === "text/html" ||
        (mimeHint !== "text/plain" && looksHtml(text) && text.includes("<"));
      if (isHtml) {
        if (text.length > capturedHtml.length) capturedHtml = text;
      } else {
        if (text.length > capturedPlain.length) capturedPlain = text;
      }
    };

    // Isolated-world patch (harmless if page uses its own clipboard object)
    const clip = navigator.clipboard;
    const origWrite = clip && clip.writeText ? clip.writeText.bind(clip) : null;
    const origWriteFull = clip && clip.write ? clip.write.bind(clip) : null;
    if (clip) {
      try {
        clip.writeText = async (text) => {
          capture(text, "text/plain");
          if (origWrite) return origWrite(text);
        };
      } catch (_) {}
      try {
        clip.write = async (items) => {
          try {
            for (const item of items || []) {
              if (item && item.types) {
                // Prefer text/plain first when both are present
                const types = Array.from(item.types || []);
                const ordered = [
                  ...types.filter((t) => t === "text/plain"),
                  ...types.filter((t) => t !== "text/plain" && String(t).startsWith("text/")),
                ];
                for (const type of ordered) {
                  const blob = await item.getType(type);
                  capture(await blob.text(), String(type));
                }
              }
            }
          } catch (_) {}
          if (origWriteFull) return origWriteFull(items);
        };
      } catch (_) {}
    }

    const onCopy = (e) => {
      try {
        const plain =
          (e.clipboardData && e.clipboardData.getData("text/plain")) ||
          (window.clipboardData && window.clipboardData.getData("Text")) ||
          "";
        if (plain) capture(plain, "text/plain");
        const html = e.clipboardData && e.clipboardData.getData("text/html");
        if (html) capture(html, "text/html");
      } catch (_) {}
    };
    document.addEventListener("copy", onCopy, true);

    try {
      btn.focus && btn.focus();
      for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
        try {
          btn.dispatchEvent(
            new PointerEvent(type, {
              bubbles: true,
              cancelable: true,
              view: window,
              pointerType: "mouse",
            })
          );
        } catch (_) {
          btn.dispatchEvent(
            new MouseEvent(type.replace("pointer", "mouse"), {
              bubbles: true,
              cancelable: true,
              view: window,
            })
          );
        }
      }
      try {
        btn.click();
      } catch (_) {}

      // Page handlers are often async (React onClick → writeText). Poll briefly.
      for (let i = 0; i < 5; i++) {
        await sleep(i < 2 ? 40 : 70);
        if (global.__NEMAPI_LAST_CLIP__ && global.__NEMAPI_LAST_CLIP__ !== before) {
          capture(global.__NEMAPI_LAST_CLIP__);
        }
        if (capturedPlain && capturedPlain.length > 10) break;
      }
    } finally {
      document.removeEventListener("copy", onCopy, true);
      if (clip && origWrite) {
        try {
          clip.writeText = origWrite;
        } catch (_) {}
      }
      if (clip && origWriteFull) {
        try {
          clip.write = origWriteFull;
        } catch (_) {}
      }
    }

    if (!capturedPlain && global.__NEMAPI_LAST_CLIP__ && global.__NEMAPI_LAST_CLIP__ !== before) {
      capture(global.__NEMAPI_LAST_CLIP__);
    }

    // Last resort: read the system clipboard (requires clipboardRead permission)
    if ((!capturedPlain || capturedPlain.length < 10) && clip && typeof clip.readText === "function") {
      try {
        const read = await clip.readText();
        if (read && read.length > 10 && read !== "[object Object]" && read !== before) {
          capture(read, "text/plain");
        }
      } catch (_) {}
    }

    let result = capturedPlain;
    // If we only got HTML, convert tags to markdown-ish plain text
    if ((!result || result.length < 10) && capturedHtml) {
      result = htmlStringToMarkdown(capturedHtml);
    }
    // Safety: never return raw HTML blobs
    if (result && looksHtml(result) && result.includes("<")) {
      result = htmlStringToMarkdown(result);
    }

    if (result && result !== "[object Object]" && !/^\[object\s+\w+\]$/i.test(result.trim())) {
      result = result.trim();
      // Final safety: reject if clipboard looks like the user prompt
      if (looksLikeUserPrompt(result, domHint)) {
        return "";
      }
      return result;
    }
    return "";
  }

  /** Convert an HTML string (from clipboard text/html) into approximate markdown. */
  function htmlStringToMarkdown(html) {
    if (!html || typeof html !== "string") return "";
    try {
      const doc = new DOMParser().parseFromString(html, "text/html");
      const body = doc.body || doc.documentElement;
      if (!body) return stripTagsFallback(html);
      return htmlToMarkdown(body) || stripTagsFallback(html);
    } catch (_) {
      return stripTagsFallback(html);
    }
  }

  function stripTagsFallback(html) {
    return String(html || "")
      .replace(/<br\s*\/?>/gi, "\n")
      .replace(/<\/p>/gi, "\n\n")
      .replace(/<\/div>/gi, "\n")
      .replace(/<\/h[1-6]>/gi, "\n\n")
      .replace(/<\/li>/gi, "\n")
      .replace(/<li[^>]*>/gi, "- ")
      .replace(/<(strong|b)[^>]*>/gi, "**")
      .replace(/<\/(strong|b)>/gi, "**")
      .replace(/<(em|i)[^>]*>/gi, "*")
      .replace(/<\/(em|i)>/gi, "*")
      .replace(/<[^>]+>/g, "")
      .replace(/&nbsp;/g, " ")
      .replace(/&amp;/g, "&")
      .replace(/&lt;/g, "<")
      .replace(/&gt;/g, ">")
      .replace(/&quot;/g, '"')
      .replace(/\n{3,}/g, "\n\n")
      .trim();
  }

  /**
   * Cascade: React fiber → clipboard copy → htmlToMarkdown → plain text.
   * @param {Element} messageEl
   * @param {{ markdownRoots?: string[], copyRoot?: Element, preferPremium?: boolean }} opts
   */
  async function extractAssistantMarkdown(messageEl, opts) {
    opts = opts || {};
    const preferPremium = opts.preferPremium !== false;
    if (!messageEl) return "";

    if (preferPremium) {
      // 1) React source markdown (DeepSeek stores props.markdown on ds-markdown)
      const mdRoots = [];
      if (opts.markdownRoots) {
        for (const s of opts.markdownRoots) {
          try {
            mdRoots.push(...messageEl.querySelectorAll(s));
          } catch (_) {}
        }
      }
      if (!mdRoots.length) mdRoots.push(messageEl);
      for (const node of mdRoots) {
        const fromReact = extractReactMarkdown(node);
        if (fromReact && fromReact.length > 20) return fromReact;
      }

      // 2) Clipboard via synthetic Copy click (works when site doesn't require isTrusted)
      const copyRoot = opts.copyRoot || messageEl.parentElement || messageEl;
      const fromClip = await tryClipboardFromCopyButton(copyRoot, opts.copySelectors);
      if (fromClip && fromClip.length > 10) return fromClip;
    }

    // 3) Structured HTML → Markdown
    const htmlRoots = opts.markdownRoots
      ? opts.markdownRoots.flatMap((s) => {
          try {
            return Array.from(messageEl.querySelectorAll(s));
          } catch (_) {
            return [];
          }
        })
      : [messageEl];
    for (const node of htmlRoots.length ? htmlRoots : [messageEl]) {
      const md = htmlToMarkdown(node);
      if (md) return md;
    }

    // 4) Plain text last resort
    return (messageEl.innerText || messageEl.textContent || "").trim();
  }

  function premiumEnabled() {
    return !!(global.__NEMAPI_PREMIUM_MD__);
  }

  // Install page clipboard hook as early as possible (content-script load)
  try {
    if (typeof document !== "undefined") {
      if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", () => ensurePageClipboardHook(), { once: true });
      } else {
        ensurePageClipboardHook();
      }
    }
  } catch (_) {}

  global.NemApiBase = {
    sleep,
    waitFor,
    queryFirst,
    queryAll,
    isVisible,
    isDisabled,
    textOf,
    findByText,
    findComposerRoot,
    setTextareaValue,
    setContentEditable,
    setQuill,
    setNativeValue,
    pasteText,
    clickEl,
    pressEnter,
    waitUntilStable,
    waitForNewResponse,
    waitForEnabledSend,
    textFingerprint,
    getReactFiber,
    navigateFiberPath,
    extractReactMarkdown,
    htmlToMarkdown,
    stripLeadingLineNumbers,
    cleanMarkdownCodeFences,
    tryClipboardFromCopyButton,
    ensurePageClipboardHook,
    extractAssistantMarkdown,
    premiumEnabled,
    isCodeBlockCopyButton,
    isUserMessageContext,
    isAssistantMessageContext,
    scoreMessageCopyButton,
  };
})(typeof window !== "undefined" ? window : self);
