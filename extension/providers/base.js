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
   */
  async function waitUntilStable(getText, { timeout = 180000, stableMs = 1800, pollMs = 400, previous = "" } = {}) {
    const t0 = Date.now();
    const prev = (previous || "").trim();
    let last = "";
    let stableSince = Date.now();
    while (Date.now() - t0 < timeout) {
      if (global.__NEMAPI_STOP__) throw new Error("Stopped");
      let cur = "";
      try {
        cur = (getText() || "").trim();
      } catch (_) {}
      // Ignore the last answer that was already present before this job, and
      // any partial render of it. Virtualized lists (DeepSeek) re-render old
      // messages as a truncated prefix of the full text; without this guard a
      // stale fragment is returned as the new response.
      if (cur && prev && (cur === prev || (cur.length < prev.length && prev.startsWith(cur)))) {
        cur = "";
      }
      if (cur && cur === last && cur.length > 0) {
        if (Date.now() - stableSince >= stableMs) return cur;
      } else {
        last = cur;
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

  /**
   * Wait for a NEW assistant message element to appear (one that was not
   * present before the request was sent), then wait for its text to stabilize.
   * getMessageEls() must return assistant message elements in DOM order;
   * readText(el) extracts the assistant answer text from a message element.
   * Falls back to text-based stability on the last element when no distinct
   * new element is detected (e.g. virtual lists that reuse DOM nodes).
   */
  async function waitForNewResponse(getMessageEls, readText, { timeout = 180000, stableMs = 2000, pollMs = 400, previous = "", newElTimeout = 20000 } = {}) {
    const oldEl = lastOf(getMessageEls());
    const phase1End = Date.now() + newElTimeout;
    let newEl = null;
    while (Date.now() < phase1End) {
      if (global.__NEMAPI_STOP__) throw new Error("Stopped");
      const last = lastOf(getMessageEls());
      if (last && last !== oldEl) {
        newEl = last;
        break;
      }
      await sleep(pollMs);
    }
    const readLast = () => {
      const last = lastOf(getMessageEls());
      return last ? readText(last) : "";
    };
    if (!newEl) {
      return waitUntilStable(readLast, { timeout, stableMs, pollMs, previous });
    }
    return waitUntilStable(() => readText(newEl), { timeout, stableMs, pollMs, previous });
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
   * Try synthetic click on a Copy button while intercepting clipboard.writeText.
   * Returns markdown string or "". Does NOT require a physical/trusted click to
   * *read* the intercept — but the site's handler may still ignore untrusted clicks.
   */
  async function tryClipboardFromCopyButton(root, buttonSelectors) {
    const sels = buttonSelectors || [
      'button[data-testid="action-bar-copy"]',
      'button[aria-label*="Copy" i]',
      'button[aria-label*="Copier" i]',
      'button[aria-label*="复制"]',
      'button[title*="Copy" i]',
    ];
    if (!root) root = document.body;
    let btn = null;
    for (const s of sels) {
      try {
        const nodes = root.querySelectorAll(s);
        for (let i = nodes.length - 1; i >= 0; i--) {
          const n = nodes[i];
          const label = (
            (n.getAttribute("aria-label") || "") +
            " " +
            (n.title || "") +
            " " +
            (n.innerText || n.textContent || "")
          ).toLowerCase();
          if (s === "button" && !/copy|copier|复制|clipboard/.test(label)) continue;
          if (!isVisible(n)) continue;
          btn = n;
          break;
        }
      } catch (_) {}
      if (btn) break;
    }
    if (!btn) {
      // Text-based fallback
      btn = findByText(["button", "[role='button']"], [/copy/i, /copier/i, /复制/], root);
    }
    if (!btn) return "";

    let captured = "";
    const clip = navigator.clipboard;
    const origWrite = clip && clip.writeText ? clip.writeText.bind(clip) : null;
    const origWriteFull = clip && clip.write ? clip.write.bind(clip) : null;

    const capture = (text) => {
      if (typeof text === "string" && text.length > captured.length) {
        captured = text;
      }
    };

    if (clip) {
      try {
        clip.writeText = async (text) => {
          capture(text);
          return undefined;
        };
      } catch (_) {}
      try {
        clip.write = async (items) => {
          try {
            for (const item of items || []) {
              if (item && item.types) {
                for (const type of item.types) {
                  if (String(type).startsWith("text/")) {
                    const blob = await item.getType(type);
                    const text = await blob.text();
                    capture(text);
                  }
                }
              }
            }
          } catch (_) {}
          return undefined;
        };
      } catch (_) {}
    }

    // Also listen for legacy copy event
    const onCopy = (e) => {
      try {
        const txt =
          (e.clipboardData && e.clipboardData.getData("text/plain")) ||
          (window.clipboardData && window.clipboardData.getData("Text")) ||
          "";
        capture(txt);
      } catch (_) {}
    };
    document.addEventListener("copy", onCopy, true);

    try {
      // Multi-event sequence — some UIs listen to pointerup rather than click
      btn.focus && btn.focus();
      for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
        try {
          btn.dispatchEvent(
            new PointerEvent(type, { bubbles: true, cancelable: true, view: window, pointerType: "mouse" })
          );
        } catch (_) {
          btn.dispatchEvent(new MouseEvent(type.replace("pointer", "mouse"), { bubbles: true, cancelable: true, view: window }));
        }
      }
      // Fallback native click
      try {
        btn.click();
      } catch (_) {}
      // Wait for async clipboard handlers
      await sleep(120);
      await sleep(80);
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
    if (captured && captured !== "[object Object]") return captured.trim();
    return "";
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

  global.NemApiBase = {
    sleep,
    waitFor,
    queryFirst,
    queryAll,
    isVisible,
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
    getReactFiber,
    navigateFiberPath,
    extractReactMarkdown,
    htmlToMarkdown,
    stripLeadingLineNumbers,
    cleanMarkdownCodeFences,
    tryClipboardFromCopyButton,
    extractAssistantMarkdown,
    premiumEnabled,
  };
})(typeof window !== "undefined" ? window : self);
