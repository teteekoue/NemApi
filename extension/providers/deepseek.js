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

  // File input: one hidden <input type=file multiple> living next to the
  // attach button (div.bf38813a). Probed on chat.deepseek.com (Oct 2026): it is
  // present as soon as the composer renders, no click on the button needed.
  const FILE_INPUT = [
    "input[type='file']",
    "input[accept*='image/']",
    "input[accept*='.png']",
  ];

  function findFileInput() {
    return B.queryFirst(FILE_INPUT);
  }

  /**
   * Did the page take the files?
   *
   * The file input is the ONLY reliable witness: its `files` list is filled
   * by the browser itself. Guessing the class of the preview (blob: img,
   * ds-animated-size-item, …) is what broke: the preview does not match
   * those names, the code concluded "no attachment", the prompt was typed
   * DURING the upload and was wiped by the re-render that ends it.
   */
  function filesAccepted() {
    const input = findFileInput();
    if (!input) return false;
    try {
      return !!(input.files && input.files.length);
    } catch (_) {
      return false;
    }
  }

  /** Is an attachment present in the composer? (any media next to the input) */
  function hasAttachment() {
    if (filesAccepted()) return true;
    return attachmentShown();
  }

  /**
   * Does the page SHOW an attachment next to the composer?
   *
   * The DOM witness (a thumbnail, a canvas, the attachment chip). Unlike
   * `filesAccepted()` it does NOT look at the file input: we fill that one
   * ourselves, so it proves nothing about the app.
   */
  function attachmentShown() {
    try {
      const el = findInput();
      const root = el ? el.parentElement : null;
      const scope = root || document.body;
      return !!scope.querySelector(
        "img[src^='blob:'], img[src^='data:image'], canvas, [class*='attachment'], [class*='upload']"
      );
    } catch (_) {
      return false;
    }
  }

  /**
   * Attach files (images or documents) to the next message.
   * `files` are File/Blob objects built by the content script from the API's
   * image_url parts. Returns the number of files the page accepted.
   */
  // Why the last write failed (lengths, first difference, truncation): the
  // error handed to the user must say what the site did, not "it failed".
  let lastComposerReport = null;

  // Files attached for the NEXT message. `sendPrompt` only waits for an
  // upload when there is one: that wait is useless (and can last its whole
  // timeout) for the 99 % of sends that carry no image.
  let pendingUploads = 0;

  async function attachFiles(files) {
    const input = findFileInput();
    if (!input) throw new Error("DeepSeek: file input not found");
    const list = Array.from(files || []);
    if (!list.length) return 0;
    // baseline: the previews already on the page (a new one proves the upload
    // was accepted even when the page sets no usable alt on the thumbnail)
    const baseline = document.querySelectorAll("img[src^='blob:'], img[src^='data:image']").length;
    const accepted = await B.attachFiles(input, list);
    // The page must SHOW that it took the file. Filling `input.files` from
    // JS is NOT a proof — we did it ourselves — so an app that ignores the
    // dispatched events would leave the image nowhere and the request would
    // send a prompt whose image is silently missing.
    const settled = await B.waitUploadFinished(list, {
      inputs: FILE_INPUT,
      baseline,
      probe: attachmentShown,
      timeout: 30000,
    });
    if (!settled) {
      throw new Error(
        "DeepSeek: attachment not accepted (check the file type/size; the UI shows an error next to the composer)"
      );
    }
    pendingUploads += list.length;
    return accepted;
  }

  /** Is an upload still running (spinner on the attachment chip)? */
  function uploading() {
    try {
      const chip = document.querySelector(
        "[class*='ds-animated-size-item'], [class*='_6d1b417']"
      );
      if (!chip) return false;
      return !!chip.querySelector(".ds-loading, [class*='loading'], [class*='spinner']");
    } catch (_) {
      return false;
    }
  }

  /**
   * Wait until the page has taken the files.
   *
   * The file input is the only reliable witness: the browser fills its
   * `files` list. We do NOT wait for the DOM to be quiet: a page that
   * updates a counter or a spinner keeps mutating forever, and waiting for
   * a quiet DOM cost up to 90 s for nothing. The upload that is still
   * running is handled by the write/retry loop, which rewrites the prompt
   * as long as the app wipes it.
   */
  async function waitUploadSettled(timeoutMs) {
    if (!filesAccepted() && !hasAttachment()) return true;
    const t0 = Date.now();
    const budget = Math.min(timeoutMs || 15000, 20000);
    while (Date.now() - t0 < budget) {
      if (filesAccepted()) return true;
      await B.sleep(150);
    }
    log(`the page did not take the files within ${budget}ms`);
    return filesAccepted();
  }

  function getMessageEls() {
    for (const s of MESSAGE) {
      const nodes = document.querySelectorAll(s);
      if (nodes.length) return Array.from(nodes);
    }
    return [];
  }

  function domToMarkdown(root) {
    if (!root) return "";
    if (B.noteExtractSource) B.noteExtractSource("dom");
    const clone = root.cloneNode(true);
    if (B.restoreMath) B.restoreMath(clone);

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
      const ticks = B.fenceFor ? B.fenceFor(body) : "```";
      const fence = ticks + lang + "\n" + body.replace(/\n$/, "") + "\n" + ticks;
      const replacement = document.createTextNode("\n\n" + fence + "\n\n");
      pre.parentNode.replaceChild(replacement, pre);
    });

    let text = clone.innerText || clone.textContent || "";
    return cleanResponse(text);
  }

  // Spans passed through verbatim: fenced code blocks and tool-call blocks
  // (their JSON carries file contents and shell commands).
  const PROTECTED_RE = /(^|\n)[ \t]*(`{3,}|~{3,})[^\n]*\n[\s\S]*?(?:\n[ \t]*\2[`~]*[ \t]*(?=\n|$)|$)|<(tool_call|tool)[\s>:][\s\S]*?(?:<\/\3(?::[^>]*)?\s*>|$)|<[｜|]{1,2}\s*DSML[\s\S]*?(?:<\s*\/\s*[｜|]{1,2}\s*DSML\s*[｜|]{1,2}\s*(?:tool_calls|function_calls|calls)\s*>|$)/g;

  function stripThink(text) {
    let t = String(text || "");
    t = t.replace(/<think>[\s\S]*?<\/think>/gi, "");
    t = t.replace(/(^|\n)```think[\s\S]*?```/gi, "$1");
    return t;
  }

  /**
   * Remove UI chrome left by DOM extraction: only WHOLE lines that are a
   * button label ("Copy", "Download"…), never inside code or tool calls.
   * (An older inline pass removed every "json", "edit", "stop", "continue"…
   * word — e.g. the JS keyword `continue;` or "package.json" in a write_file.)
   */
  function cleanResponse(text) {
    const t0 = stripThink(text);
    const parts = [];
    let pos = 0;
    t0.replace(PROTECTED_RE, (m, _lead, _f, _t, offset) => {
      if (offset > pos) parts.push([false, t0.slice(pos, offset)]);
      parts.push([true, m]);
      pos = offset + m.length;
      return m;
    });
    if (pos < t0.length) parts.push([false, t0.slice(pos)]);
    let t = parts
      .map(([prot, seg]) => {
        if (prot) return seg;
        return seg
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
          .join("\n")
          .replace(/[ \t]+\n/g, "\n");
      })
      .join("");
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
   * Fast path (same as the stable DeepSeek exporter):
   *  1) React fiber props.markdown on .ds-markdown  — sync, complete MD
   *  2) DOM htmlToMarkdown / readMessageText       — sync, complete text
   *  3) Clipboard ONLY if both above are empty/weak — slow path, 1 attempt
   * Never prefer clipboard when DOM/React already has the full answer.
   */
  async function extractPremium(msgEl) {
    if (!msgEl) return "";

    // Collect answer nodes (skip thinking chain)
    const answerNodes = [];
    msgEl.querySelectorAll("div.ds-markdown, .ds-markdown").forEach((el) => {
      if (el.closest(".ds-think-content, [class*='think'], .e1675d8b")) return;
      answerNodes.push(el);
    });

    // 1) React source markdown — primary, no click, full content
    let best = "";
    for (const el of answerNodes) {
      const md = B.extractReactMarkdown(el, ["markdown", "content", "text"]);
      if (md && md.length > best.length) best = md;
    }
    if (!best && answerNodes[0]) {
      best = B.extractReactMarkdown(msgEl, ["markdown", "content", "text"]) || "";
    }
    if (best && best.length > 10) {
      // original markdown: no UI chrome to remove — only the thinking part
      return stripThink(best).trim();
    }

    // 2) Structured HTML → MD (sync, preserves code fences)
    const parts = [];
    for (const el of answerNodes) {
      const md = B.htmlToMarkdown(el);
      if (md) parts.push(md);
    }
    if (parts.length) {
      const joined = cleanResponse(parts.join("\n\n"));
      if (joined.length > 10) return joined;
    }

    // 3) Plain DOM text (already strips think / UI chrome)
    const fromDom = readMessageText(msgEl);
    if (fromDom && fromDom.length > 10) return fromDom;

    // 4) Clipboard last resort only — single attempt, scoped to this message
    if (B.premiumEnabled()) {
      try {
        const fromClip = await B.tryClipboardFromCopyButton(
          msgEl,
          [
            'button[aria-label*="Copy" i]',
            'button[aria-label*="复制"]',
            'button[title*="Copy" i]',
          ],
          { domAssistantText: fromDom }
        );
        if (fromClip && fromClip.length > 10 && fromClip !== "[object Object]") {
          if (B.noteExtractSource) B.noteExtractSource("clipboard");
          return cleanResponse(fromClip);
        }
      } catch (_) {}
    }

    return fromDom || "";
  }

  /**
   * The editable surfaces of the composer, best candidate first.
   *
   * DeepSeek does not always use the same widget: the current build drives a
   * rich-text editor (contenteditable, Lexical — `role="textbox"`), the
   * previous ones a plain textarea. Writing into the wrong one is silent:
   * the text goes nowhere, the send control stays disabled and the message
   * is never sent. The caller therefore tries them in turn and keeps the one
   * that really enables the send control.
   */
  function inputCandidates() {
    const out = [];
    const seen = new Set();
    const order = [
      "div[contenteditable='true'][role='textbox']",
      "div[contenteditable='true']",
      "textarea#chat-input",
      'textarea[placeholder*="DeepSeek" i]',
      'textarea[placeholder*="Message" i]',
      "textarea[role='textbox']",
      ".ds-textarea textarea",
      "textarea[data-testid*='chat' i]",
      "textarea",
    ];
    for (const sel of order) {
      let els = [];
      try {
        els = Array.from(document.querySelectorAll(sel));
      } catch (_) {
        continue;
      }
      for (const el of els) {
        if (seen.has(el)) continue;
        if (!isVisible(el)) continue;
        if (el.disabled || el.readOnly) continue;
        seen.add(el);
        out.push(el);
      }
    }
    return out;
  }

  function findInput() {
    const cands = inputCandidates();
    if (cands.length) return cands[0];
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

  /** Bubble count — used by content.js to detect a new answer even when text is identical. */
  function getResponseCount() {
    try {
      return getMessageEls().length;
    } catch (_) {
      return 0;
    }
  }

  /**
   * Cheap activity signature: the number of answers and the RAW size of the
   * last one. getLastResponse() rebuilds the whole markdown and is far too
   * heavy to call every 800 ms while waiting for the page to be idle.
   */
  function getActivitySignature() {
    try {
      const els = getMessageEls();
      const last = els.length ? els[els.length - 1] : null;
      const raw = last ? String(last.innerText || last.textContent || "") : "";
      return els.length + ":" + raw.length;
    } catch (_) {
      return "0:0";
    }
  }

  /**
   * Is the model currently streaming?
   *
   * Kept deliberately STRICT: a false positive here is not free. The proxy
   * only accepts a stable answer once the provider is idle, and a stuck
   * "Stop" indicator delays every request by the whole `busyStuckMs` window
   * (and the NEXT job too, while it waits for the page to be idle). A
   * page-wide `[class*="loading"]` match hits any element of the page (a
   * skeleton, a lazy image) and permanently "keeps the page busy".
   */
  function isGenerating() {
    // 1) the Stop button, by label PREFIX (a plain substring match also
    //    catches unrelated elements whose class contains "stop")
    const stops = document.querySelectorAll(
      'button[aria-label^="Stop" i], [role="button"][aria-label^="Stop" i],' +
        'button[title^="Stop" i], [aria-label^="停止" i], [aria-label^="Stop generating" i]'
    );
    for (const el of stops) {
      if (isVisible(el)) return true;
    }
    // 2) a spinner INSIDE the last answer or the composer (not anywhere on
    //    the page: the page has plenty of "loading" elements that are not
    //    the model thinking)
    const els = getMessageEls();
    const scope = els.length ? els[els.length - 1] : findComposerRoot(findInput());
    if (scope && scope.querySelector) {
      const spin = scope.querySelector(
        ".ds-loading, [aria-busy='true'], [class*='stop-generating']"
      );
      if (spin && isVisible(spin)) return true;
    }
    return false;
  }

  function log(msg) {
    try {
      console.log("[NemApi/deepseek] " + msg);
    } catch (_) {}
  }

  /** Current text really held by the composer (React/Lexical controlled). */
  function editorText(el) {
    if (!el) return "";
    if (B.editorText) return B.editorText(el);
    return String(el.value != null ? el.value : el.textContent || "");
  }

  /**
   * Does the composer really hold the text we want to send?
   *
   * The DOM value is not enough: the app keeps its own state and rewrites
   * the DOM on the next render, so a write it ignored leaves a field that
   * looks filled and is emptied a moment later (the image stays, the text
   * vanishes). The value the app holds is read through the page bridge.
   */
  function composerHolds(want, el) {
    const target = el || findInput();
    if (!target) return false;
    const dom = editorText(target);
    const text = String(want || "");
    // Compare the CONTENT, at every size: the editor reflows what it is given
    // (a paragraph per line, so a blank line is read back as two), and an
    // exact check then says "wiped" about a text that is perfectly there. That
    // wrong verdict is what made the provider wipe the composer and type the
    // prompt again — the "it is written and it disappears" of a 17 KB agent
    // prompt, while a one-line 127-character prompt went through.
    const match = B.composerMatches || B.textMatches;
    if (!match(dom, text)) return false;
    const react = B.readReactValue(target);
    return react === null || match(react, text);
  }

  /**
   * Write the prompt into a candidate editor and PROVE the app took it.
   *
   * The proof is the send control: DeepSeek enables it only when the
   * composer really holds the text. The DOM value alone is not proof (a
   * rich-text editor keeps the text in its own state, and a field the app
   * ignored is emptied at the next render).
   *
   * While an upload is running, the app wipes the prompt at the end of it.
   * The write is therefore repeated until it SURVIVES: the loop ends when
   * the send control is usable AND the text is still there a moment later.
   *
   * Returns {input, btn} on success, null when this editor never took the
   * text — the caller then tries the next candidate.
   */
  async function typePrompt(text, el) {
    const want = String(text || "").trim();
    const target = el || findInput();
    if (!target) return null;
    const holds = () => composerHolds(want, target);

    // Write ONCE. A second write happens only when the composer is EMPTY (a
    // real wipe, e.g. the end of an upload) — never when it already holds the
    // prompt: re-typing a prompt that is there is the loop the user sees as
    // "the text appears, disappears, comes back".
    let written = null;
    for (let attempt = 1; attempt <= 2; attempt += 1) {
      if (holds()) {
        written = target;
        break;
      }
      written = await B.writeAndConfirm(() => target, text, {
        attempts: 1,
        log: (m) => log(m),
      });
      if (written || holds()) break;
      await B.sleep(250);
    }
    // the editor may have taken the text while the confirmation was reading a
    // half-committed value (a rich editor inserts in several frames)
    if (!written && holds()) written = target;
    if (!written) return null;

    // The send control follows the APP's state, which lags behind the DOM —
    // and the longer the prompt, the longer the lag: a 17 562-character prompt
    // needed more than the 2.5 s of the old budget, and the old code concluded
    // "the app wiped the text", wiped it for real and typed it again. Budget
    // grows with the prompt, bounded.
    const budget = Math.min(20000, 3000 + Math.round(want.length / 4));
    const found = await waitForEnabledSend(written, budget);
    const usable = !!(found && !isDisabled(found) && isVisible(found));

    // the app took it — make sure it is not wiped a moment later
    await B.sleep(usable ? 700 : 200);
    if (!holds()) {
      log("the text was wiped after the write (the app restored its own state)");
      return null;
    }
    if (!usable) {
      // A send control that stays disabled does NOT mean the text is lost:
      // the composer still holds it. Keep it and let the caller press Enter —
      // DeepSeek sends on Enter — instead of wiping and retyping.
      log(
        `no usable send control after ${budget} ms (found=${found ? "yes" : "no"}) ` +
          "→ keeping the text, Enter fallback"
      );
    } else {
      log(`editor holds ${editorText(written).length}c (want ${want.length}c) → send enabled`);
    }
    return { input: written, btn: usable ? found : null };
  }

  async function sendPrompt(text) {
    const want = String(text || "").trim();

    // 1) The upload must be OVER before anything is typed: a prompt written
    //    while the page still re-renders is wiped by the app (the image
    //    stays, the text disappears).
    //
    //    Only when this message really carries a file: the check scans the
    //    whole page for previews and spinners, and a site that always shows a
    //    spinner made it wait its full timeout on EVERY send (90 s before
    //    v5.5.0) — with nothing to upload.
    if (pendingUploads) {
      try {
        await waitUploadSettled({ timeout: 20000 });
      } finally {
        pendingUploads = 0;
      }
    }

    // 2) Write the prompt, trying every candidate editor: the current
    //    DeepSeek build drives a rich-text editor, the previous ones a
    //    textarea, and writing into the wrong one is silent.
    const cands = inputCandidates();
    if (!cands.length) throw new Error("DeepSeek: composer not found");
    let input = null;
    let btn = null;
    let lastTried = null;
    for (const el of cands.slice(0, 3)) {
      lastTried = el;
      const res = await typePrompt(text, el);
      if (res) {
        input = res.input;
        btn = res.btn;
        break;
      }
      log("this editor did not take the text → trying the next one");
      // NO clear here: the next write clears the field itself, and clearing
      // now erased the only evidence of what the site had accepted (it also
      // produced the visible flicker: text appears, disappears, appears…).
      if (!lastComposerReport && B.composerReport) {
        try {
          lastComposerReport = B.composerReport(editorText(el), want);
        } catch (_) {}
      }
    }
    if (!input) {
      // The last editor may already hold the prompt even though our
      // confirmation failed (normalisation, or the site truncated it): send it
      // rather than clearing everything and starting over a fourth time.
      const last = lastTried || findInput();
      if (last && want && B.composerMatches && B.composerMatches(editorText(last), want)) {
        log("the composer holds the prompt (tolerated comparison) → sending it");
        input = last;
        btn = findSend(last);
      }
    }
    if (!input) {
      const last = lastTried || findInput();
      let r = last && B.composerReport ? B.composerReport(editorText(last), want) : null;
      // the composer may have been emptied by a retry: keep what we measured
      if (lastComposerReport && (!r || r.dom === 0)) r = lastComposerReport;
      const detail = r
        ? ` (composer ${r.dom}c, wanted ${r.want}c` +
          (r.truncated
            ? ", the site truncated it — this prompt is too large for this composer"
            : ", first difference at " + r.firstDiff) +
          `, editors=${cands.length}, send=${findSend(last) ? "found" : "none"})`
        : "";
      throw new Error(
        "DeepSeek: the prompt could not be written into the composer" + detail
      );
    }

    // 3) Click the send control (when the page has given us a usable one).
    try {
      btn && btn.focus();
    } catch (_) {}
    if (btn && B.clickEl(btn)) {
      // Verify that the message was accepted (the composer is cleared)
      await B.sleep(400);
      if (!want || !composerHolds(want, input)) return; // success
    }

    // Fallback 1: native Enter (works when "Enter to send" is enabled)
    B.pressEnter(input);
    await B.sleep(300);
    if (!want || !composerHolds(want, input)) return;

    // Fallback 2: try clicking again after the Enter attempt
    const again = findSend(input);
    if (again && !isDisabled(again)) {
      B.clickEl(again);
    }
  }

  /**
   * Cheap text read: the RAW text of the answer, without the markdown
   * conversion. Used for the polling (a change signal is enough), so the
   * expensive DOM clone + markdown conversion runs only once, at the end.
   */
  function readCheapText(msgEl) {
    if (!msgEl) return "";
    try {
      const main =
        msgEl.querySelector(".ds-assistant-message-main-content") ||
        msgEl.querySelector("[class*='assistant-message-main']") ||
        msgEl.querySelector(".ds-markdown") ||
        msgEl.querySelector("[class*='markdown']");
      const node = main || msgEl;
      return String(node.innerText || node.textContent || "");
    } catch (_) {
      return "";
    }
  }

  async function waitForResponse(previous = "", prevCount = 0) {
    await B.sleep(400);

    // Phase 1: wait for generation to start (Stop button, new bubble, or new content)
    const genStart = Date.now();
    while (Date.now() - genStart < 10000) {
      if (isGenerating()) break;
      if (prevCount && getResponseCount() > prevCount) break;
      const els = getMessageEls();
      const last = els.length ? readCheapText(els[els.length - 1]) : "";
      if (last && last !== previous && last.length > (previous || "").length) {
        break;
      }
      await B.sleep(200);
    }

    await B.waitForNewResponse(getMessageEls, readMessageText, {
      timeout: 180000,
      stableMs: 1600,
      previous: prevCount && getResponseCount() > prevCount ? "" : previous,
      newElTimeout: 20000,
      readCheap: readCheapText,
    });

    // Brief settle for virtual-list re-render (keep short for speed)
    await B.sleep(150);

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
    extractPremium, // final read of an answer (exported for tests)
    getResponseCount,
    getActivitySignature,
    isGenerating,
    findInput,
    findFileInput,
    attachFiles,
    uploading,
  };
})(typeof window !== "undefined" ? window : self);
