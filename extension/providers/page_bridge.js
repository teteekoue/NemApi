/*
 * NemApi — page (MAIN world) bridge.
 *
 * Content scripts run in an ISOLATED world: they share the DOM with the page
 * but not its JavaScript objects, so the React fiber properties
 * (`__reactFiber$…`) that hold the ORIGINAL markdown of an answer are
 * invisible to them. Without this bridge the extension had to rebuild the
 * answer from the rendered HTML, which is lossy for tool calls:
 *   - `$…$` in shell commands is rendered as KaTeX math (text lost),
 *   - markdown backslash escapes are consumed (`\"` → `"`), breaking JSON,
 *   - `*` / `_` become emphasis…
 *
 * Protocol (synchronous — DOM events are dispatched to every world at once):
 *   isolated world: el.setAttribute("data-nemapi-md-req", "markdown,content")
 *                   document.dispatchEvent(new CustomEvent("nemapi:read-md"))
 *                   md = el.getAttribute("data-nemapi-md")
 */
(function () {
  if (window.__nemapiPageBridge) return;
  window.__nemapiPageBridge = true;
  // visible from the isolated world: lets the content script report whether
  // the original markdown is reachable (diagnostics in the proxy logs)
  try {
    document.documentElement.setAttribute("data-nemapi-bridge", "1");
  } catch (_) {}

  function fiberOf(el) {
    if (!el) return null;
    const key = Object.keys(el).find(
      (k) => k.startsWith("__reactFiber$") || k.startsWith("__reactInternalInstance$")
    );
    return key ? el[key] : null;
  }

  function follow(fiber, path) {
    let f = fiber;
    for (const step of path) {
      if (!f) return null;
      f = f[step];
    }
    return f || null;
  }

  function readMarkdown(el, propNames) {
    const names = propNames && propNames.length
      ? propNames
      : ["markdown", "content", "text", "rawContent", "source", "message", "value", "answer", "response", "children"];
    const start = fiberOf(el);
    if (!start) return "";

    // Proven DeepSeek paths (Chat Exporter): props.markdown a few levels up
    const known = [["return", "return", "return"], ["return", "return"], ["return"], ["child", "return"], ["child", "child", "return"]];
    for (const path of known) {
      try {
        const f = follow(start, path);
        const props = f && (f.memoizedProps || f.pendingProps);
        if (props && typeof props.markdown === "string" && props.markdown.trim().length > 5) {
          return props.markdown;
        }
      } catch (_) {}
    }

    const seen = new Set();
    const queue = [start];
    let best = "";
    let bestScore = -1;
    let steps = 0;
    while (queue.length && steps < 120) {
      const f = queue.shift();
      steps += 1;
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
          if (score > bestScore) {
            bestScore = score;
            best = v;
          }
        }
      }
      if (f.child) queue.push(f.child);
      if (f.sibling) queue.push(f.sibling);
      if (f.return && steps < 60) queue.push(f.return);
    }
    return best;
  }

  // Clipboard hook, needed before Chrome 111 only: background.js cannot run
  // executeScript in the MAIN world there, while the page itself (React
  // "Copy" buttons) keeps calling navigator.clipboard.writeText. The isolated
  // world listens for the CustomEvent and shares the DOM with this world.
  window.__nemapiInstallClipHook = function () {
    if (window.__NEMAPI_CLIP_HOOK_INSTALLED__) return;
    window.__NEMAPI_CLIP_HOOK_INSTALLED__ = true;
    function emit(text, force) {
      try {
        if (typeof text !== "string") {
          if (text == null) return;
          try {
            text = String(text);
          } catch (_) {
            return;
          }
        }
        if (!text || text === "[object Object]") return;
        if (!force && /<\/?(p|div|span|strong|em|br|ul|ol|li|h[1-6])\b/i.test(text) && text.indexOf("<") !== -1) return;
        window.dispatchEvent(new CustomEvent("__nemapi_clip_capture__", { detail: text }));
      } catch (_) {}
    }
    try {
      const clip = navigator.clipboard;
      if (!clip) return;
      if (typeof clip.writeText === "function") {
        const orig = clip.writeText.bind(clip);
        clip.writeText = async function (text) {
          emit(text, true);
          return orig(text);
        };
      }
      if (typeof clip.write === "function") {
        const origW = clip.write.bind(clip);
        clip.write = async function (items) {
          try {
            for (const item of items || []) {
              if (!item || !item.types) continue;
              const types = Array.from(item.types || []);
              if (types.includes("text/plain")) {
                const blob = await item.getType("text/plain");
                emit(await blob.text(), true);
              } else {
                for (const type of types) {
                  if (String(type).startsWith("text/")) {
                    const blob = await item.getType(type);
                    emit(await blob.text(), type === "text/plain");
                  }
                }
              }
            }
          } catch (_) {}
          return origW(items);
        };
      }
    } catch (_) {}
    document.addEventListener(
      "copy",
      function (e) {
        try {
          const t = (e.clipboardData && e.clipboardData.getData("text/plain")) || "";
          if (t) emit(t, true);
        } catch (_) {}
      },
      true
    );
  };
  // Installed up-front: a bridge injected after the page loaded must not wait
  // for the isolated world to ask for it.
  try {
    window.__nemapiInstallClipHook();
  } catch (_) {}

  // The isolated world cannot call this function directly (separate JS realm):
  // it asks for the hook through a DOM event (fired by base.js).
  document.addEventListener("nemapi:install-clip-hook", function () {
    try {
      window.__nemapiInstallClipHook();
    } catch (_) {}
  });

  document.addEventListener("nemapi:read-md", function () {
    const els = document.querySelectorAll("[data-nemapi-md-req]");
    els.forEach((el) => {
      const names = (el.getAttribute("data-nemapi-md-req") || "").split(",").filter(Boolean);
      let md = "";
      try {
        md = readMarkdown(el, names) || "";
      } catch (_) {
        md = "";
      }
      el.removeAttribute("data-nemapi-md-req");
      el.setAttribute("data-nemapi-md", md);
    });
  });
})();
