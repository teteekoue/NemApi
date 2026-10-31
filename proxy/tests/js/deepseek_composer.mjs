/**
 * Regression: a 17 KB agent prompt into a DeepSeek-style Lexical composer.
 *
 * Reported from the field (v5.5.0 log):
 *
 *   09:32:03 Job queued → deepseek (17562 chars)
 *   09:32:08 Job error: DeepSeek: the prompt could not be written into the
 *                    composer (the page keeps clearing it)
 *
 * …while a 127-character and a 1 122-character prompt went through on the very
 * same page, one second apart. So the write itself was not the problem.
 *
 * Two real traits of that editor are modelled here:
 *
 *  1. **the commit is not instantaneous.** The text lands in the DOM at once,
 *     but the app's own state (and so the send button, `aria-disabled`) follows
 *     later — from a few milliseconds for a short prompt to seconds for a 17 KB
 *     one. A provider that believes "the send control is still disabled, so the
 *     app wiped the text" wipes the composer and types it again: that is the
 *     "the prompt is written and disappears" the user sees, and the final
 *     "could not be written into the composer".
 *  2. **the text is reflowed.** Each line becomes a paragraph, and reading the
 *     composer back gives a blank line between every two lines. A character-
 *     for-character comparison fails on text that is perfectly there.
 */
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
let JSDOM;
try {
  ({ JSDOM } = require("jsdom"));
} catch (e) {
  console.error("jsdom is not installed: npm install jsdom (or set NODE_PATH)");
  process.exit(77);
}

const EXT = process.env.NEMAPI_EXT || new URL("../../../extension/", import.meta.url).pathname;
const baseSrc = readFileSync(EXT + "/providers/base.js", "utf8");
const deepseekSrc = readFileSync(EXT + "/providers/deepseek.js", "utf8");

let failures = 0;
const check = (name, ok, detail = "") => {
  if (ok) console.log("  ok   " + name);
  else {
    failures += 1;
    console.log("  FAIL " + name + " " + detail);
  }
};

/** An agent prompt: prose, blank lines, list items, a code block. */
function agentPrompt(target) {
  const parts = [
    "# Contexte du projet",
    "",
    "Tu es un agent de code. Voici l'état du dépôt.",
    "",
    "## Arborescence",
    "",
    "```",
    "RIA/version1/",
    "  ├── tokenizer.py",
    "  ├── train.py",
    "  └── data/corpus.txt (UTF-8) - Prêt pour l'entraînement du tokenizer",
    "```",
    "",
    "## Tâche",
    "",
  ];
  let i = 0;
  while (parts.join("\n").length < target) {
    i += 1;
    parts.push(`- étape ${i} : vérifier le fichier ${i}.py et rapporter ce qui bloque`);
    parts.push("");
    if (i % 6 === 0) {
      parts.push("```python");
      parts.push(`def etape_${i}(x):`);
      parts.push(`    return x * ${i}`);
      parts.push("```");
      parts.push("");
    }
  }
  return parts.join("\n").slice(0, target);
}

/**
 * A page shaped like chat.deepseek.com.
 *
 *  - `commitMs(n)`: how long the app's state takes to follow a write of n chars
 *    (its send button stays disabled until then — the real Lexical/React lag);
 *  - reading the composer back reflows the text (paragraph per line);
 *  - clicking a disabled button does nothing;
 *  - `truncateAt` / `ignoreWrites` model a site that cuts or refuses the text.
 */
function world({ commitMs = () => 120, truncateAt = 0, ignoreWrites = false, reflow = true } = {}) {
  const dom = new JSDOM(
    `<!doctype html><html><body>
       <div class="chat-input"><div class="chat-input-editor ds-scroll-area" contenteditable="true" role="textbox"></div>
         <div class="send-wrap"><button class="ds-icon-button" aria-label="Send message">→</button></div>
       </div>
     </body></html>`,
    { runScripts: "outside-only", pretendToBeVisual: true }
  );
  const w = dom.window;
  const editor = w.document.querySelector(".chat-input-editor");
  const button = w.document.querySelector("button.ds-icon-button");

  let stored = ""; // what the editor holds (the text as written)
  let writes = 0;
  let sent = [];
  let commitTimer = null;
  const read = () => (reflow ? stored.split("\n").join("\n\n") : stored);
  const setText = (text) => {
    if (ignoreWrites) return;
    writes += 1;
    stored = truncateAt ? String(text).slice(0, truncateAt) : String(text);
    button.setAttribute("aria-disabled", "true");
    if (commitTimer) clearTimeout(commitTimer);
    commitTimer = setTimeout(() => {
      if (stored.trim()) button.setAttribute("aria-disabled", "false");
    }, commitMs(stored.length));
  };
  const clear = () => {
    stored = "";
    button.setAttribute("aria-disabled", "true");
  };

  Object.defineProperty(editor, "innerText", { configurable: true, get: read });
  Object.defineProperty(editor, "textContent", {
    configurable: true,
    get: read,
    set: (v) => setText(v),
  });
  w.document.execCommand = (cmd, _ui, v) => {
    if (cmd === "insertText") return setText(v), true;
    if (cmd === "selectAll") return true;
    if (cmd === "delete") return clear(), true;
    return false;
  };
  button.addEventListener("click", () => {
    if (button.getAttribute("aria-disabled") !== "false" || !stored.trim()) return;
    sent.push(stored);
    clear();
  });
  const box = (el, r) => {
    el.getBoundingClientRect = () => ({ ...r, right: r.left + r.width, bottom: r.top + r.height, x: r.left, y: r.top });
  };
  box(editor, { left: 100, top: 600, width: 700, height: 60 });
  box(button, { left: 810, top: 610, width: 32, height: 32 });
  Object.defineProperty(w.HTMLElement.prototype, "offsetParent", {
    configurable: true,
    get() {
      return this.parentElement;
    },
  });
  w.chrome = { runtime: { getURL: (p) => `chrome-extension://t/${p}`, sendMessage: async () => ({ ok: true }) } };
  w.eval(baseSrc);
  w.eval(deepseekSrc);
  return {
    w,
    editor,
    button,
    stored: () => stored,
    read,
    sent: () => sent,
    writes: () => writes,
    provider: w.NemApiProviders.deepseek,
  };
}

/** A short prompt is committed in a moment, a 17 KB one takes seconds. */
const realCommit = (n) => (n > 10000 ? 3200 : n > 4000 ? 900 : 120);

async function main() {
  // 1. the DeepSeek path, on all the sizes of the report
  for (const size of [127, 1122, 17562]) {
    const page = world({ commitMs: realCommit });
    const prompt = agentPrompt(size);
    let error = null;
    try {
      await page.provider.sendPrompt(prompt);
    } catch (e) {
      error = e;
    }
    check(
      `${size} chars: the prompt reaches the page`,
      !error && page.sent().length === 1,
      error ? error.message : `sent=${page.sent().length} writes=${page.writes()}`
    );
    if (page.sent().length) {
      check(`${size} chars: what was sent is the whole prompt`, page.sent()[0] === prompt,
        `${page.sent()[0].length}c / ${prompt.length}c`);
    }
    check(`${size} chars: the prompt is written once, not re-typed`, page.writes() === 1, `writes=${page.writes()}`);
  }

  // 2. a 17 KB prompt with a slow editor: no re-paste loop, one write only
  {
    const page = world({ commitMs: () => 6000 });
    const prompt = agentPrompt(17562);
    let error = null;
    const t0 = Date.now();
    try {
      await page.provider.sendPrompt(prompt);
    } catch (e) {
      error = e;
    }
    const ms = Date.now() - t0;
    check(
      "a slow (6 s) send button still sends, with one single write",
      !error && page.sent().length === 1 && page.writes() === 1,
      error ? error.message : `sent=${page.sent().length} writes=${page.writes()} ${ms} ms`
    );
    check("the wait for the send control is bounded", ms < 45000, `${ms} ms`);
  }

  // 3. a REAL truncation must never be reported as written
  {
    const page = world({ truncateAt: 8000 });
    const prompt = agentPrompt(17562);
    let error = null;
    try {
      await page.provider.sendPrompt(prompt);
    } catch (e) {
      error = e;
    }
    check(
      "a truncating composer is refused, with numbers",
      !!error && /truncated|wanted/.test(error.message) && page.sent().length === 0,
      error ? error.message : "no error"
    );
  }

  // 4. a page that ignores every write reports a failure, fast, once
  {
    const page = world({ ignoreWrites: true });
    const prompt = agentPrompt(17562);
    let error = null;
    const t0 = Date.now();
    try {
      await page.provider.sendPrompt(prompt);
    } catch (e) {
      error = e;
    }
    const ms = Date.now() - t0;
    check("a composer that refuses the text fails once", !!error && ms < 40000, `${ms} ms`);
    check("the prompt was not typed again and again", page.writes() === 0, `writes=${page.writes()}`);
  }

  console.log(failures ? `\n${failures} FAILURE(S)` : "\nall checks passed");
  process.exit(failures ? 1 : 0);
}

await main();
