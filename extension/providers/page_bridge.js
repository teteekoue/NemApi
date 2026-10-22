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

  /**
   * Value React currently holds for a controlled field (the textarea).
   *
   * The isolated world can only see the DOM value, which is NOT the same
   * thing: React keeps its own state and re-writes the DOM on the next
   * render. A write that React ignored therefore leaves a textarea that
   * *looks* filled for a few hundred milliseconds and is then silently
   * emptied — which is exactly what happens when the text is typed while the
   * page re-renders (end of an image upload). Reading the controlled value
   * tells us for sure whether React accepted the write.
   *
   * Returns null when the fiber is unreachable (uncontrolled field, page
   * changed, bridge not loaded): the caller then falls back to the DOM value.
   */
  function readReactValue(el) {
    if (!el) return null;
    let fiber = fiberOf(el);
    for (let i = 0; fiber && i < 8; i++) {
      const props = fiber.memoizedProps || fiber.pendingProps;
      if (props && typeof props === "object" && Object.prototype.hasOwnProperty.call(props, "value")) {
        const v = props.value;
        return typeof v === "string" ? v : v == null ? "" : String(v);
      }
      fiber = fiber.return;
    }
    return null;
  }

  /**
   * The Quill instance that owns a Quill editor, if any.
   *
   * Quill v1 attaches itself to its container: `container.__quill = this`
   * (core/quill.js, constructor). The container is the `.ql-container`
   * element, the editable root is the `.ql-editor` inside it.
   *
   * This lookup only works in the PAGE world: an expando set on a DOM node
   * belongs to the JS realm that set it, and the isolated world has its own
   * realm — it cannot see `container.__quill`. That is why the write goes
   * through this bridge.
   */
  function findQuillInstance(el) {
    if (!el) return null;
    try {
      if (el.__quill) return el.__quill;
      const container = el.closest && el.closest(".ql-container");
      if (container && container.__quill) return container.__quill;
      let node = el;
      for (let i = 0; i < 4 && node; i++) {
        if (node.__quill) return node.__quill;
        node = node.parentElement;
      }
    } catch (_) {}
    return null;
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

  // Controlled value of a field (the composer textarea): lets the isolated
  // world know whether React really holds the text it wrote.
  document.addEventListener("nemapi:read-value", function () {
    const els = document.querySelectorAll("[data-nemapi-val-req]");
    els.forEach((el) => {
      let v = null;
      let ok = false;
      try {
        v = readReactValue(el);
        ok = v !== null;
      } catch (_) {
        v = null;
      }
      el.removeAttribute("data-nemapi-val-req");
      if (ok) {
        el.setAttribute("data-nemapi-val", v);
        el.setAttribute("data-nemapi-val-ok", "1");
      } else {
        el.removeAttribute("data-nemapi-val");
        el.removeAttribute("data-nemapi-val-ok");
      }
    });
  });
  // Write into a Quill editor through the page world.
  //
  // The app reads Quill's MODEL, and the instance only exists in the page
  // realm (`container.__quill` is invisible from the isolated world). The
  // isolated world therefore asks this bridge to do the write.
  //
  // "user" is the source of a real paste: an app that only reacts to
  // user-initiated changes ignores an "api" write. The fallback covers the
  // opposite case — a read-only editor refuses a "user" write (Quill's
  // `strict` mode) but accepts an "api" one.
  document.addEventListener("nemapi:set-quill", function (e) {
    const detail = (e && e.detail) || {};
    const text = detail.text == null ? "" : String(detail.text);
    // The element is identified by an attribute, not by the event payload:
    // a DOM node cannot be carried across the two JS realms, while an
    // attribute is part of the shared DOM.
    const targets = document.querySelectorAll("[data-nemapi-quill-target]");
    for (const el of targets) {
      let ok = false;
      try {
        const quill = findQuillInstance(el);
        if (quill) {
          quill.setText(text, "user");
          if (String(quill.getText() || "").trim() !== text.trim()) {
            quill.setText(text, "api");
          }
          ok = true;
        }
      } catch (_) {
        ok = false;
      }
      try {
        el.setAttribute("data-nemapi-quill-ok", ok ? "1" : "0");
        try {
          el.setAttribute("data-nemapi-quill-text", ok ? String(quill.getText() || "") : "");
        } catch (_) {}
      } catch (_) {}
    }
  });
})();
