// Shared helpers: talk to the FastAPI backend (a different origin) with the session cookie.
export const API = (import.meta.env.VITE_API_BASE_URL || "http://localhost:8000").replace(/\/+$/, "");

let csrf = null;
let onUnauthorized = () => {};
export const setCsrf = (t) => { csrf = t; };
export const setUnauthorizedHandler = (fn) => { onUnauthorized = fn; };

/** JSON request. Throws Error(message) with .status and .payload on non-2xx. */
export async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (typeof opts.body === "string") headers["Content-Type"] = "application/json";
  if (opts.method && opts.method !== "GET") headers["X-CSRF-Token"] = csrf || "";
  let r;
  try {
    r = await fetch(API + path, { credentials: "include", cache: "no-store", ...opts, headers });
  } catch (_) {
    throw new Error("Cannot reach the server. Check your connection / Wi-Fi and try again.");
  }
  let j = {};
  try { j = await r.json(); } catch (_) { /* empty body */ }
  if (r.status === 401) onUnauthorized(j);
  if (r.status === 403 && j && j.code === "RESTRICTED") showRestricted(j);
  if (!r.ok && !(j && j.status)) {
    const e = new Error(typeof j.detail === "string" ? j.detail : j.message || `Request failed (${r.status})`);
    e.status = r.status; e.payload = j;
    throw e;
  }
  return j;
}

/** Office-only policy: replace the page with a "restricted" notice. */
export function showRestricted(j) {
  const box = el("section", { class: "panel", style: "max-width:480px;margin:40px auto;text-align:center" },
    el("h2", {}, "Access restricted"),
    el("p", {}, j.detail || "This page can only be used from an office computer on the office network."));
  if (j.client_ip) box.append(el("p", { class: "muted" }, `Detected network address: ${j.client_ip}`));
  document.body.replaceChildren(box);
}

/** Like api() but never throws on HTTP errors: returns { r, j } (used where the error body matters). */
export async function rawApi(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (typeof opts.body === "string") headers["Content-Type"] = "application/json";
  if (opts.method && opts.method !== "GET") headers["X-CSRF-Token"] = csrf || "";
  const r = await fetch(API + path, { credentials: "include", cache: "no-store", ...opts, headers });
  let j = {};
  try { j = await r.json(); } catch (_) { /* empty body */ }
  if (r.status === 401) onUnauthorized(j);
  if (r.status === 403 && j && j.code === "RESTRICTED") showRestricted(j);
  return { r, j };
}

/** Download a file from an authenticated endpoint (cookie + CORS), keeping the server's file name. */
export async function download(path) {
  const r = await fetch(API + path, { credentials: "include", cache: "no-store" });
  if (!r.ok) {
    let msg = `Download failed (${r.status})`;
    try { const j = await r.json(); if (typeof j.detail === "string") msg = j.detail; } catch (_) {}
    const e = new Error(msg); e.status = r.status; throw e;
  }
  const cd = r.headers.get("Content-Disposition") || "";
  const m = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(cd);
  const blob = await r.blob();
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = m ? decodeURIComponent(m[1]) : "attendance.xlsx";
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 10_000);
}

/** Tiny DOM builder: text is always set via textContent (no HTML injection). */
export function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "onclick") e.addEventListener("click", v);
    else if (k === "class") e.className = v;
    else e.setAttribute(k, v);
  }
  for (const k of kids.flat()) e.append(k instanceof Node ? k : document.createTextNode(k ?? ""));
  return e;
}

export const loginUrl = (next) => `${API}/auth/login?next=${encodeURIComponent(next)}`;
