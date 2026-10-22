/* NemApi 5 — shared web UI: shell (sidebar, top bar), API helpers, polling, toasts.
 * Every page: <body data-page="…" data-title="…"> + .app > aside.sidebar + .main > header.topbar + main.content
 * then <script src="/admin.js"></script> and its own script. */
"use strict";

const $ = (id) => document.getElementById(id);
const esc = (v) =>
  String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const fmtNum = (n) => (Number(n) || 0).toLocaleString("fr-FR");
const fmtChars = (n) => (n >= 1e6 ? (n / 1e6).toFixed(1) + " M" : n >= 1e3 ? (n / 1e3).toFixed(1) + " k" : String(n || 0));
const fmtUptime = (s) => {
  s = Math.max(0, Math.floor(s || 0));
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d) return `${d}j ${h}h`;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${s % 60}s`;
  return `${s}s`;
};
const fmtAgo = (s) => (s == null ? "jamais" : s < 5 ? "à l'instant" : `il y a ${fmtUptime(s)}`);
const fmtCost = (cost) => {
  const c = Number(cost) || 0;
  if (c === 0) return "$0.00";
  if (c < 0.01) return "$" + c.toFixed(5);
  if (c < 1) return "$" + c.toFixed(4);
  return "$" + c.toFixed(2);
};
const fmtDate = (iso) => {
  const d = new Date(iso + "T00:00:00");
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString("fr-FR", { day: "2-digit", month: "short" });
};

const PCOLORS = { deepseek: "#4F8CFF", qwen: "#22d3ee", claude: "#F59E0B", gemini: "#F87171", chatgpt: "#10A37F", kimi: "#A855F7", zai: "#6366F1" };
const PNAMES = { deepseek: "DeepSeek", qwen: "Qwen", claude: "Claude", gemini: "Gemini", chatgpt: "ChatGPT", kimi: "Kimi", zai: "Z.ai" };
const providerLabel = (id) => PNAMES[id] || id;
function providerBadge(id) {
  return `<span class="pbadge" style="--pcolor:${PCOLORS[id] || "var(--accent)"}">
    <img class="picon" src="/icons/providers/${esc(id)}.png" alt="" width="20" height="20" onerror="this.style.display='none'">
    <span class="pname-text">${esc(providerLabel(id))}</span></span>`;
}

/* ---------------------------------------------------------------- icons */
const ICONS = {
  dashboard: '<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/>',
  config: '<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/>',
  agents: '<rect x="4" y="7" width="16" height="13" rx="3"/><path d="M12 3v4M8.5 13h.01M15.5 13h.01M9 17h6M2 12v3M22 12v3"/>',
  keys: '<path d="M21 2l-2 2m-7.6 7.6a5.5 5.5 0 1 1-7.78 7.78 5.5 5.5 0 0 1 7.78-7.78zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4"/>',
  chat: '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
  analytics: '<path d="M18 20V10M12 20V4M6 20v-6"/>',
  menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  moon: '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>',
  info: '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/>',
  warn: '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0zM12 9v4M12 17h.01"/>',
  link: '<path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7"/><path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7"/>',
  tabs: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  book: '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>',
  sliders: '<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/>',
  log: '<path d="M4 4v16h16"/><path d="M4 20l6-6 4 4 6-8"/>',
  check: '<path d="M22 11.1V12a10 10 0 1 1-5.9-9.1"/><path d="M22 4 12 14l-3-3"/>',
  coins: '<path d="M12 2v20M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/>',
  clock: '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
  users: '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.9M16 3.1a4 4 0 0 1 0 7.8"/>',
  shield: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  copy: '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
  trash: '<path d="M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
  send: '<path d="M22 2 11 13M22 2l-7 20-4-9-9-4 20-7z"/>',
  paperclip: '<path d="M21.4 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/>',
  image: '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/>',
  refresh: '<path d="M23 4v6h-6M1 20v-6h6"/><path d="M3.5 9a9 9 0 0 1 14.9-3.4L23 10M1 14l4.6 4.4A9 9 0 0 0 20.5 15"/>',
  pulse: '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
  activity: '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
  layers: '<path d="M12 2 2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/>',
  terminal: '<path d="M4 17l6-6-6-6"/><path d="M12 19h8"/>',
  zap: '<path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z"/>',
  cpu: '<rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 1v3M15 1v3M9 20v3M15 20v3M20 9h3M20 14h3M1 9h3M1 14h3"/>',
  database: '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14c0 1.7 4 3 9 3s9-1.3 9-3V5"/><path d="M3 12c0 1.7 4 3 9 3s9-1.3 9-3"/>',
  gauge: '<path d="M12 14l4-4"/><path d="M3.5 18a9 9 0 1 1 17 0"/><circle cx="12" cy="18" r="1.5"/>',
  sparkles: '<path d="M12 3l1.9 4.6L18.5 9.5 13.9 11.4 12 16l-1.9-4.6L5.5 9.5l4.6-1.9L12 3z"/><path d="M19 15l.9 2.1L22 18l-2.1.9L19 21l-.9-2.1L16 18l2.1-.9L19 15z"/>',
  globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a15 15 0 0 1 0 18 15 15 0 0 1 0-18z"/>',
  arrowUp: '<path d="M12 19V5M5 12l7-7 7 7"/>',
  arrowDown: '<path d="M12 5v14M5 12l7 7 7-7"/>',
  target: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1.5"/>',
};
/* The "ico" class is always present: it carries the default size. A call
 * site may add its own class, never remove it. */
const icon = (name, cls = "") =>
  `<svg class="ico${cls ? " " + cls : ""}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name] || ""}</svg>`;

/* ---------------------------------------------------------------- navigation (single source) */
const NAV = [
  { section: "Pilotage" },
  { id: "dashboard", href: "/", label: "Tableau de bord", icon: "dashboard" },
  { id: "analytics", href: "/analytics", label: "Analytics", icon: "analytics" },
  { section: "Agents" },
  { id: "integrations", href: "/integrations", label: "Intégration agents IA", icon: "agents", badge: "nav-badge-integrations" },
  { section: "Réglages" },
  { id: "config", href: "/config", label: "Configuration", icon: "config" },
  { id: "api-keys", href: "/api-keys-page", label: "Clés API", icon: "keys", badge: "nav-badge-keys" },
  { section: "Outils" },
  { id: "chat", href: "/chat", label: "Chat test", icon: "chat" },
];

const NemUI = {
  version: "",
  state: null,
  _stateListeners: [],
  onState(fn) {
    this._stateListeners.push(fn);
    if (this.state) fn(this.state);
  },
};

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem("nemapi-theme", theme); } catch (_) {}
  const btn = $("theme-btn");
  if (btn) btn.innerHTML = icon(theme === "light" ? "moon" : "sun", "");
}

function navHTML(page) {
  let html = "", open = false;
  for (const n of NAV) {
    if (n.section) {
      if (open) html += "</nav>";
      html += `<div class="nav-section">${esc(n.section)}</div><nav class="nav" aria-label="${esc(n.section)}">`;
      open = true;
      continue;
    }
    const active = n.id === page;
    html += `<a href="${n.href}" class="${active ? "active" : ""}" data-nav="${n.id}"${active ? ' aria-current="page"' : ""}>
      ${icon(n.icon)}<span class="label">${esc(n.label)}</span>${n.badge ? `<span class="nav-badge" id="${n.badge}" hidden></span>` : ""}</a>`;
  }
  return html + (open ? "</nav>" : "");
}

function renderShell() {
  const page = document.body.dataset.page || "";
  const title = document.body.dataset.title || document.title;
  const side = document.querySelector(".sidebar");
  if (side) {
    side.innerHTML = `
      <div class="brand">
        <div class="logo"><img src="/icons/logo-48.png" alt="NemApi"></div>
        <div><div class="name"><span>NemApi</span></div><div class="tag">Pont API local · navigateur</div></div>
      </div>
      ${navHTML(page)}
      <div class="side-foot">
        <div class="row"><i id="sdot" class="dot"></i><span id="sconn">Connexion…</span></div>
        <div class="ver" id="side-version">NemApi</div>
      </div>`;
  }
  const top = document.querySelector(".topbar");
  if (top) {
    top.innerHTML = `
      <button class="icon-btn menu-btn" type="button" aria-label="Ouvrir le menu">${icon("menu", "")}</button>
      <div class="crumb">NemApi / <b>${esc(title)}</b></div>
      <div class="spacer"></div>
      <div class="pill" title="État de l'extension navigateur"><i id="hdot" class="dot"></i><span id="hconn">Connexion…</span></div>
      <button class="icon-btn" id="theme-btn" type="button" aria-label="Changer de thème" title="Thème clair / sombre"></button>`;
  }
  const content = document.querySelector(".content");
  if (content) {
    if (!$("offline-banner")) {
      content.insertAdjacentHTML("afterbegin", `<div class="offline-banner" id="offline-banner">${icon("warn", "")}<span>Proxy NemApi injoignable — nouvelle tentative automatique…</span></div>`);
    }
    content.setAttribute("id", content.id || "main-content");
    content.setAttribute("tabindex", "-1");
  }
  if (!document.querySelector(".overlay")) document.body.insertAdjacentHTML("beforeend", '<div class="overlay"></div>');
  if (!$("toasts")) document.body.insertAdjacentHTML("beforeend", '<div id="toasts" aria-live="polite"></div>');
  if (!$("skip-link")) {
    document.body.insertAdjacentHTML("afterbegin", '<a class="skip-link" href="#main-content">Aller au contenu principal</a>');
  }

  let theme = "dark";
  try { theme = localStorage.getItem("nemapi-theme") || "dark"; } catch (_) {}
  applyTheme(theme);
  $("theme-btn")?.addEventListener("click", () => applyTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light"));

  const overlay = document.querySelector(".overlay");
  const close = () => { side?.classList.remove("open"); overlay?.classList.remove("show"); };
  document.querySelector(".menu-btn")?.addEventListener("click", () => {
    const open = !side.classList.contains("open");
    side.classList.toggle("open", open);
    overlay.classList.toggle("show", open);
  });
  overlay?.addEventListener("click", close);
  document.addEventListener("keydown", (e) => e.key === "Escape" && close());
}

/* ---------------------------------------------------------------- API */
async function api(path, options = {}) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), options.timeout || 10000);
  try {
    const res = await fetch(path, { ...options, signal: ctrl.signal });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.error?.message || `HTTP ${res.status}`);
    return body;
  } catch (e) {
    if (e.name === "AbortError") throw new Error("délai dépassé");
    throw e;
  } finally {
    clearTimeout(timer);
  }
}
const post = (path, body = {}) =>
  api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

/* Re-render only when the markup changes (no flicker, keeps focus / hover). */
function setHTML(el, html) {
  if (typeof el === "string") el = $(el);
  if (el && el.__html !== html) {
    el.innerHTML = html;
    el.__html = html;
  }
}
function setText(id, text) {
  const el = $(id);
  if (el && el.textContent !== String(text)) el.textContent = text;
}

/* Poll without overlapping requests, paused while the tab is hidden, with backoff. */
function poll(fn, every = 2000) {
  let timer = null, failures = 0, running = false;
  const run = async () => {
    clearTimeout(timer);
    if (running) return;
    if (document.hidden) { timer = setTimeout(run, every); return; }
    running = true;
    try { await fn(); failures = 0; } catch (e) { failures++; }
    running = false;
    timer = setTimeout(run, failures ? Math.min(every * 2 ** failures, 15000) : every);
  };
  document.addEventListener("visibilitychange", () => !document.hidden && run());
  run();
  return { now: run };
}

/* ---------------------------------------------------------------- feedback */
function toast(message, type = "info", ms = 3200) {
  const box = $("toasts");
  if (!box) return;
  const el = document.createElement("div");
  el.className = "toast " + type;
  el.innerHTML = `<div>${esc(message)}</div>`;
  box.appendChild(el);
  setTimeout(() => { el.classList.add("hide"); setTimeout(() => el.remove(), 300); }, ms);
}
function confirmDialog(title, message, { ok = "Confirmer", danger = false } = {}) {
  return new Promise((resolve) => {
    const back = document.createElement("div");
    back.className = "modal-back";
    back.innerHTML = `<div class="modal" role="dialog" aria-modal="true"><h4>${esc(title)}</h4><p>${esc(message)}</p>
      <div class="actions"><button class="btn" data-a="0" type="button">Annuler</button>
      <button class="btn ${danger ? "btn-danger" : "btn-primary"}" data-a="1" type="button">${esc(ok)}</button></div></div>`;
    const done = (v) => { back.remove(); document.removeEventListener("keydown", onKey); resolve(v); };
    const onKey = (e) => e.key === "Escape" && done(false);
    back.addEventListener("click", (e) => {
      if (e.target === back) done(false);
      const a = e.target.closest("[data-a]");
      if (a) done(a.dataset.a === "1");
    });
    document.addEventListener("keydown", onKey);
    document.body.appendChild(back);
    back.querySelector('[data-a="1"]').focus();
  });
}
async function copyText(text, label = "Copié") {
  try {
    await navigator.clipboard.writeText(text);
    toast(label, "ok", 1800);
  } catch (_) {
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); toast(label, "ok", 1800); } catch (__) { toast("Copie impossible", "error"); }
    ta.remove();
  }
}

/* Settings switch helper: <input id> bound to POST {key: checked}. */
function bindSwitch(id, save, { onDone } = {}) {
  const el = $(id);
  if (!el) return;
  el.addEventListener("change", async () => {
    el.disabled = true;
    try {
      await save(el.checked);
      onDone && onDone(el.checked);
    } catch (e) {
      el.checked = !el.checked;
      if (e.message !== "annulé") toast("Erreur : " + e.message, "error");
    } finally {
      el.disabled = false;
    }
  });
}

/* ---------------------------------------------------------------- charts (SVG, no dependency) */
/* An area chart of the daily history. The values are also written as a table
 * below the chart by the pages that need it: a chart alone is not readable
 * by a screen reader. */
function areaChart(days, { value = "requests", width = 720, height = 200, label = "Requêtes" } = {}) {
  const rows = days || [];
  const data = rows.map((d) => Number(d[value]) || 0);
  if (!data.length || data.every((v) => v === 0)) {
    return `<div class="chart-empty">${icon("activity", "")}<span>Aucune donnée sur la période.</span></div>`;
  }
  const pad = { top: 16, right: 8, bottom: 26, left: 42 };
  const w = width - pad.left - pad.right;
  const h = height - pad.top - pad.bottom;
  const max = Math.max(...data, 1);
  const step = data.length > 1 ? w / (data.length - 1) : w;
  const x = (i) => pad.left + i * step;
  const y = (v) => pad.top + h - (v / max) * h;
  const points = data.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`);
  const line = points.join(" ");
  const area = `${pad.left},${pad.top + h} ${line} ${x(data.length - 1).toFixed(1)},${pad.top + h}`;

  const ticks = [0, Math.round(max / 2), max];
  const grid = ticks.map((t) =>
    `<line class="grid-line" x1="${pad.left}" x2="${pad.left + w}" y1="${y(t).toFixed(1)}" y2="${y(t).toFixed(1)}"/>` +
    `<text class="axis-text" x="${pad.left - 8}" y="${(y(t) + 4).toFixed(1)}" text-anchor="end">${fmtNum(t)}</text>`).join("");

  const every = Math.ceil(data.length / 7);
  const xlabels = rows.map((d, i) => (i % every === 0 || i === data.length - 1)
    ? `<text class="axis-text" x="${x(i).toFixed(1)}" y="${height - 6}" text-anchor="middle">${fmtDate(d.date)}</text>` : "").join("");

  const dots = rows.map((d, i) =>
    `<circle class="point-hit" cx="${x(i).toFixed(1)}" cy="${y(data[i]).toFixed(1)}" r="12" tabindex="0"
       aria-label="${esc(d.date)} : ${fmtNum(data[i])} ${esc(label)}"><title>${esc(d.date)} — ${fmtNum(data[i])} ${esc(label)}</title></circle>` +
    `<circle class="point" cx="${x(i).toFixed(1)}" cy="${y(data[i]).toFixed(1)}" r="3.4"/>`).join("");

  return `<svg class="chart" viewBox="0 0 ${width} ${height}" role="img"
      aria-label="${esc(label)} par jour sur ${data.length} jours" preserveAspectRatio="none">
    <defs><linearGradient id="nemapi-area" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="var(--accent)" stop-opacity="0.30"/>
      <stop offset="100%" stop-color="var(--accent)" stop-opacity="0"/>
    </linearGradient></defs>
    ${grid}
    <polygon class="series-area" points="${area}"/>
    <polyline class="series-line" points="${line}"/>
    ${dots}
    ${xlabels}
  </svg>`;
}

/* ---------------------------------------------------------------- shared status */
function setConn(connected, busy) {
  const cls = "dot " + (connected ? (busy ? "busy" : "on") : "off");
  const text = !connected ? "Extension non connectée" : busy ? "Extension · job en cours" : "Extension connectée";
  for (const id of ["sdot", "hdot"]) if ($(id) && $(id).className !== cls) $(id).className = cls;
  setText("sconn", text);
  setText("hconn", text);
}
function setOffline(off) {
  $("offline-banner")?.classList.toggle("show", !!off);
  if (off) {
    for (const id of ["sdot", "hdot"]) if ($(id)) $(id).className = "dot off";
    setText("sconn", "Proxy injoignable");
    setText("hconn", "Proxy injoignable");
  }
}

async function refreshShellState() {
  try {
    const state = await api("/extension/state");
    NemUI.state = state;
    setOffline(false);
    setConn(!!state.connected, !!state.busy);
    for (const fn of NemUI._stateListeners) {
      try { fn(state); } catch (e) { console.error(e); }
    }
  } catch (e) {
    setOffline(true);
    throw e;
  }
}

async function refreshBadges() {
  const [keys, integ, health] = await Promise.all([
    api("/api-keys/config").catch(() => null),
    api("/agent-integrations").catch(() => null),
    NemUI.version ? null : api("/health").catch(() => null),
  ]);
  const kb = $("nav-badge-keys");
  if (kb && keys) {
    kb.hidden = false;
    kb.className = "nav-badge " + (keys.require_auth ? "ok" : "warn");
    setText("nav-badge-keys", keys.require_auth ? String(keys.key_count || 0) : "OFF");
  }
  const ib = $("nav-badge-integrations");
  if (ib && integ) {
    const on = integ.agents.filter((a) => a.enabled).length;
    ib.hidden = !on;
    ib.className = "nav-badge ok";
    setText("nav-badge-integrations", String(on));
  }
  if (health && health.app_version) {
    NemUI.version = health.app_version;
    setText("side-version", `NemApi v${health.app_version} · 7 fournisseurs`);
  }
}

renderShell();
poll(refreshShellState, 2000);
NemUI.badges = poll(refreshBadges, 8000);
