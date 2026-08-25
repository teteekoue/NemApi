/* NemApi – shared admin helpers */
const $ = (id) => document.getElementById(id);
const esc = (v) =>
  String(v ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
const fmtNum = (n) => (n || 0).toLocaleString("fr-FR");
const fmtUptime = (s) => {
  s = s || 0;
  const h = Math.floor(s / 3600),
    m = Math.floor((s % 3600) / 60),
    sec = s % 60;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${sec}s`;
  return `${sec}s`;
};
const PCOLORS = {
  deepseek: "#60a5fa",
  qwen: "#22d3ee",
  gemini: "#f87171",
  claude: "#fbbf24",
};

async function api(path, options) {
  const res = await fetch(path, options);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error?.message || `HTTP ${res.status}`);
  return body;
}

function setConn(connected, busy) {
  const on = connected ? "on" : "";
  const sdot = $("sdot"),
    hdot = $("hdot");
  if (sdot) sdot.className = "dot " + on;
  if (hdot) hdot.className = "dot " + on;
  const text = !connected
    ? "Extension non connectée"
    : busy
      ? "Connectée · job en cours"
      : "Extension connectée";
  if ($("sconn")) $("sconn").textContent = text;
  if ($("hconn")) $("hconn").textContent = text;
}

function renderLogs(el, logs) {
  if (!el) return;
  el.innerHTML =
    (logs || [])
      .slice()
      .reverse()
      .map(
        (l) =>
          `<div class="log"><span class="t">${esc(l.t)}</span><span class="msg log-${esc(l.level)}">${esc(l.msg)}</span></div>`
      )
      .join("") ||
    '<span style="color:var(--faint)">Aucun événement pour le moment</span>';
}

/* Mobile sidebar */
function initMobileNav() {
  const btn = document.querySelector(".menu-btn");
  const side = document.querySelector(".sidebar");
  const overlay = document.querySelector(".sidebar-overlay");
  if (!btn || !side) return;

  const close = () => {
    side.classList.remove("open");
    if (overlay) overlay.classList.remove("show");
  };
  const open = () => {
    side.classList.add("open");
    if (overlay) overlay.classList.add("show");
  };

  btn.addEventListener("click", () => {
    if (side.classList.contains("open")) close();
    else open();
  });
  if (overlay) overlay.addEventListener("click", close);
  side.querySelectorAll(".nav a").forEach((a) => a.addEventListener("click", close));
}

function copyText(text) {
  navigator.clipboard?.writeText(text).catch(() => {});
}

document.addEventListener("DOMContentLoaded", initMobileNav);
