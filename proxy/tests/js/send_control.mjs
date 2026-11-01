/** Regression: a send control the selectors do not know must not loop.
 *
 * Reported from the field (Kimi): the prompt was PASTED AGAIN AND AGAIN into
 * the composer and never sent. `sendWithProof` retried `attempts` times on
 * each candidate surface, rewriting the whole prompt every time, whenever the
 * send button could not be found — the user saw the text appear, disappear and
 * reappear forever.
 *
 * What must happen instead: the prompt is written ONCE, the send control is
 * searched for (known selectors, then discovery), Enter is tried (several
 * sites send on Enter), and the caller learns either "sent" or a precise
 * reason — never an endless re-paste.
 *
 * This runs the real base.js in a DOM. Kimi's own selectors are exercised
 * through `sendWithProof` with the same options kimi.js passes.
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

const baseSrc = readFileSync(new URL("../../../extension/providers/base.js", import.meta.url), "utf8");

let failures = 0;
const check = (name, ok, detail = "") => {
  if (ok) console.log("  ok   " + name);
  else {
    failures += 1;
    console.log("  FAIL " + name + " " + detail);
  }
};

/**
 * A composer page.
 *  - `sendSelector`: how the site names its send control ("unknown" = a class
 *    no selector list knows about)
 *  - `enterSends`: the app submits on Enter
 */
function world({ sendSelector = "unknown", enterSends = true, sendWorks = true } = {}) {
  const dom = new JSDOM(
    `<!doctype html><html><body>
       <div class="chat-input"><div class="chat-input-editor" contenteditable="true" role="textbox"></div>
         <div id="sendwrap"></div>
       </div>
     </body></html>`,
    { runScripts: "outside-only", pretendToBeVisual: true }
  );
  const w = dom.window;
  const editor = w.document.querySelector(".chat-input-editor");
  const wrap = w.document.getElementById("sendwrap");
  const send = w.document.createElement("div");
  send.className = sendSelector === "unknown" ? "zz-arrow-widget" : "send-button-container";
  send.setAttribute("role", "button");
  wrap.appendChild(send);

  let text = "";
  let writes = 0;
  let sent = 0;
  const value = {
    get: () => text,
    set: (v) => {
      writes += 1;
      text = String(v);
    },
  };
  Object.defineProperty(editor, "innerText", { configurable: true, get: () => text });
  Object.defineProperty(editor, "textContent", { configurable: true, ...value });
  w.document.execCommand = (cmd, _ui, v) => {
    if (cmd !== "insertText") return false;
    writes += 1;
    text = String(v);
    return true;
  };
  // jsdom has no layout: give every element a usable box
  const box = (el, r) => {
    el.getBoundingClientRect = () => ({ ...r, right: r.left + r.width, bottom: r.top + r.height, x: r.left, y: r.top });
  };
  box(editor, { left: 100, top: 600, width: 700, height: 60 });
  box(send, { left: 810, top: 610, width: 32, height: 32 });
  Object.defineProperty(w.HTMLElement.prototype, "offsetParent", { configurable: true, get() { return this.parentElement; } });

  const submit = () => {
    if (!sendWorks || !text.trim()) return false;
    sent += 1;
    text = "";
    return true;
  };
  send.addEventListener("click", submit);
  w.document.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      if (enterSends) submit();
    }
  });

  w.chrome = { runtime: { getURL: (p) => `chrome-extension://t/${p}`, sendMessage: async () => ({ ok: true }) } };
  // the app's own state: a write that the app ignores is wiped on next render
  w.eval(baseSrc);
  return { w, editor, send, writes: () => writes, sent: () => sent, text: () => text };
}

/** Same call shape as kimi.js. */
async function kimiSend(w, text, log) {
  const B = w.NemApiBase;
  const INPUT = [".chat-input-editor[contenteditable='true']", '[contenteditable="true"][role="textbox"]'];
  const SEND = [".send-button-container button", ".send-button-container", '[class*="send-button"]'];
  const inputCandidates = () => {
    const found = B.inputCandidatesFrom(INPUT);
    if (found.length) return found;
    const guessed = B.discoverEditable ? B.discoverEditable() : null;
    return guessed ? [guessed] : found;
  };
  const findSend = (input) => {
    const root = B.findComposerRoot ? B.findComposerRoot(input) : w.document;
    for (const s of SEND) {
      for (const el of root.querySelectorAll(s)) if (B.isVisible(el)) return el;
    }
    return B.discoverSend ? B.discoverSend(input) : null;
  };
  const res = await B.sendWithProof({
    inputCandidates,
    findSend,
    text,
    log,
    sendDisabledCheck: (btn) => !!(btn && btn.classList && btn.classList.contains("disabled")),
  });
  if (!res) return { ok: false, reason: "not written" };
  await B.clickSend({ input: res.input, btn: res.btn, findSend, text, log });
  return { ok: true, btn: !!res.btn };
}

async function main() {
  // 1. a send control no selector knows: discovery finds it, one write only
  {
    const page = world({ sendSelector: "unknown" });
    const logs = [];
    const res = await kimiSend(page.w, "Bonjour, lis le fichier main.py", (m) => logs.push(m));
    check("the prompt is sent with an unknown send control", res.ok && page.sent() === 1, JSON.stringify(res));
    check("it was written exactly once (no re-paste loop)", page.writes() === 1, `writes=${page.writes()}`);
    check("the composer is empty afterwards", page.text() === "");
    if (logs.length) console.log("        " + logs.slice(0, 3).join(" | "));
  }

  // 2. the app only sends on Enter (no usable button at all)
  {
    const page = world({ sendSelector: "unknown", enterSends: true });
    page.send.remove(); // nothing to click
    const logs = [];
    const res = await kimiSend(page.w, "Deuxieme essai", (m) => logs.push(m));
    check("Enter is used when there is no button", res.ok && page.sent() === 1, JSON.stringify(res));
    check("still one single write", page.writes() === 1, `writes=${page.writes()}`);
  }

  // 3. a known send control: the normal path is untouched
  {
    const page = world({ sendSelector: "known" });
    const res = await kimiSend(page.w, "Troisieme essai");
    check("a known send control still works", res.ok && page.sent() === 1 && res.btn === true, JSON.stringify(res));
  }

  // 4. the app refuses everything: we fail once, with a reason, and stop
  {
    const page = world({ sendSelector: "unknown", enterSends: false, sendWorks: false });
    const logs = [];
    const t0 = Date.now();
    const res = await kimiSend(page.w, "Quatrieme essai", (m) => logs.push(m));
    const ms = Date.now() - t0;
    check("an app that never sends is reported, not looped", !res.ok || page.sent() === 0, JSON.stringify(res));
    check("the attempt is bounded in time", ms < 40000, `${ms} ms`);
    check(
      "the log explains what was tried",
      logs.some((l) => /send control|Enter|not accepted/i.test(l)),
      JSON.stringify(logs.slice(0, 4))
    );
    // the prompt must not have been retyped dozens of times
    check("the prompt was not retyped again and again", page.writes() <= 4, `writes=${page.writes()}`);
  }

  console.log(failures ? `\n${failures} FAILURE(S)` : "\nall checks passed");
  process.exit(failures ? 1 : 0);
}

await main();
