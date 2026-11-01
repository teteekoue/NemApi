/** Regression: a 150 KB prompt must be SENT, not cleared and retyped forever.
 *
 * What the user saw: the prompt appears in the composer, disappears, appears
 * again, in a loop, and the message is never sent. Two causes, both real:
 *
 *  1. the composer does not hold everything 320 ms after the insert — a rich
 *     editor commits a 150 KB paste over several frames, so the reading is a
 *     PREFIX, which the caller took for "the app wiped it" and cleared;
 *  2. the comparison with what was written was character-exact, while an
 *     editor that keeps the text still rewrites its whitespace.
 */
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

// `require` honours NODE_PATH, `import` does not: this keeps the harness
// runnable with a jsdom installed anywhere (CI, a scratch directory…).
const require = createRequire(import.meta.url);
let JSDOM;
try {
  ({ JSDOM } = require("jsdom"));
} catch (e) {
  console.error("jsdom is not installed: npm install jsdom (or set NODE_PATH)");
  process.exit(77); // "skipped" for the pytest wrapper
}

import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

// proxy/tests/js/<this file> → repo root/extension
const HERE = dirname(fileURLToPath(import.meta.url));
const EXT = join(HERE, "..", "..", "..", "extension");
const baseSrc = readFileSync(`${EXT}/providers/base.js`, "utf8");
const deepseekSrc = readFileSync(`${EXT}/providers/deepseek.js`, "utf8");

let failures = 0;
const check = (name, ok, detail = "") => {
  if (ok) console.log("  ok   " + name);
  else {
    failures += 1;
    console.log("  FAIL " + name + " " + detail);
  }
};

function buildPrompt(size) {
  const blocks = [];
  let n = 0;
  while (n < size) {
    const line = `- step ${blocks.length}: read src/module_${blocks.length}.py and check its behaviour`;
    blocks.push(line);
    n += line.length + 2;
  }
  return "# context\n\n" + blocks.join("\n\n") + "\n\n# task\n\ncontinue";
}

/**
 * A Lexical-like composer.
 *  - `slowCommit`: the text is committed in slices over `commitMs`, like a
 *    real editor re-rendering a huge paste (innerText returns a PREFIX meanwhile)
 *  - `keepWhitespace`: false → the editor re-flows blank lines, like a paste
 *  - `truncateTo`: the site refuses everything past N characters
 */
function makeWorld({ size = 150000, slowCommit = false, commitMs = 1200, keepWhitespace = true, truncateTo = 0 } = {}) {
  const dom = new JSDOM(
    `<!doctype html><html><body>
       <div id="composer" contenteditable="true" role="textbox"></div>
       <button id="send" class="ds-icon-button" disabled>send</button>
     </body></html>`,
    { runScripts: "outside-only", pretendToBeVisual: true }
  );
  const w = dom.window;
  const composer = w.document.getElementById("composer");
  const send = w.document.getElementById("send");

  let model = "";          // what the app has accepted so far
  let pending = "";        // what is still being committed
  let clicks = 0;

  const shape = (t) => {
    const text = String(t);
    if (keepWhitespace) return truncateTo ? text.slice(0, truncateTo) : text;
    const reflowed = text.replace(/\n{2,}/g, "\n"); // blank lines dropped
    return truncateTo ? reflowed.slice(0, truncateTo) : reflowed;
  };

  const commit = (value) => {
    const full = shape(value);
    send.disabled = false;
    if (!slowCommit) {
      model = full;
      pending = "";
      return;
    }
    model = "";
    pending = full;
    const slices = 8;
    const part = Math.ceil(full.length / slices);
    let i = 0;
    const step = () => {
      model += pending.slice(i * part, (i + 1) * part);
      i += 1;
      if (i < slices) setTimeout(step, Math.max(1, commitMs / slices));
      else pending = "";
    };
    setTimeout(step, Math.max(1, commitMs / slices));
  };

  Object.defineProperty(composer, "innerText", { configurable: true, get: () => model });
  Object.defineProperty(composer, "textContent", {
    configurable: true,
    get: () => model,
    set: (v) => commit(v),
  });
  w.document.execCommand = (cmd, _ui, value) => {
    if (cmd !== "insertText") return false;
    commit(value);
    return true;
  };
  send.addEventListener("click", () => {
    clicks += 1;
    model = "";
    pending = "";
    send.disabled = true;
  });

  w.chrome = {
    runtime: { getURL: (p) => "chrome-extension://t/" + p, sendMessage: async () => ({ ok: true }) },
  };
  w.eval(baseSrc);
  w.eval(deepseekSrc);
  return { w, composer, send, prompt: buildPrompt(size), clicks: () => clicks };
}

async function main() {
  // ---- 1. 150 KB, committed slowly: the prompt is sent, once
  {
    const { w, prompt, clicks } = makeWorld({ size: 150000, slowCommit: true, commitMs: 1500 });
    const t0 = Date.now();
    await w.NemApiProviders.deepseek.sendPrompt(prompt);
    const ms = Date.now() - t0;
    check("a 150 KB prompt committed over 1.5 s is sent", clicks() === 1, `clicks=${clicks()}`);
    check("the write does not loop", ms < 20000, `${ms} ms`);
  }

  // ---- 2. 150 KB, the editor re-flows the text: still sent
  {
    const { w, prompt, clicks } = makeWorld({ size: 150000, keepWhitespace: false });
    await w.NemApiProviders.deepseek.sendPrompt(prompt);
    check("a re-flowed long prompt is sent (tolerant comparison)", clicks() === 1, `clicks=${clicks()}`);
  }

  // ---- 3. the site TRUNCATES: fail fast, say why, do not loop
  {
    const { w, prompt } = makeWorld({ size: 150000, truncateTo: 100000 });
    let error = null;
    const t0 = Date.now();
    try {
      await w.NemApiProviders.deepseek.sendPrompt(prompt);
    } catch (e) {
      error = e.message;
    }
    const ms = Date.now() - t0;
    check("a truncated prompt fails fast", ms < 25000, `${ms} ms`);
    check(
      "the error names the truncation",
      !!error && /too large for this composer/i.test(error),
      JSON.stringify(error)
    );
    if (error) console.log("        " + error);
  }

  // ---- 4. a SHORT prompt keeps the strict check and is sent at once
  {
    const { w, prompt, clicks } = makeWorld({ size: 200 });
    await w.NemApiProviders.deepseek.sendPrompt(prompt);
    check("a short prompt is sent", clicks() === 1);
  }

  // ---- 5. the comparison itself
  {
    const { w } = makeWorld({ size: 200 });
    const B = w.NemApiBase;
    check("exact text matches", B.composerMatches("hello world", "hello world"));
    check("a different short text does NOT match", !B.composerMatches("hello there", "hello world"));
    const long = buildPrompt(60000);
    check("a long text with re-flowed lines matches", B.composerMatches(long.replace(/\n{2,}/g, "\n"), long));
    check("a long text with a lost chunk does NOT match", !B.composerMatches(long.slice(0, 40000) + long.slice(45000), long));
    const report = B.composerReport(long.slice(0, 30000), long);
    check("a truncation is reported", report.truncated === true, JSON.stringify(report));
    check("the report gives both lengths", report.dom === 30000 && report.want > 50000, JSON.stringify(report));
  }

  console.log(failures ? `\n${failures} FAILURE(S)` : "\nall checks passed");
  process.exit(failures ? 1 : 0);
}

await main();
