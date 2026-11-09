// ytdl-web — frontend sin frameworks. Los textos llegan en window.T (ver app/i18n.py).
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const T = window.T;
const L = T.js;
// "Descargando {kind}…" + {kind: "audio"} -> "Descargando audio…"
const fmt = (s, vars = {}) => String(s).replace(/\{(\w+)\}/g, (m, k) => (k in vars ? vars[k] : m));

// localStorage puede fallar (modo privado, bloqueado): todo va en try/catch
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* sin almacenamiento */ } },
};
// Descargas pedidas desde esta pestaña: solo en sessionStorage (se borra al cerrarla),
// así no se pierde el guardado automático si cambias de idioma a media descarga.
const session = {
  get(k) { try { return JSON.parse(sessionStorage.getItem(k) || "[]"); } catch { return []; } },
  set(k, v) { try { sessionStorage.setItem(k, JSON.stringify(v)); } catch { /* nada */ } },
};
const mine = new Set(session.get("ytdl.mine"));
const fetched = new Set(session.get("ytdl.fetched"));
const saveSession = () => { session.set("ytdl.mine", [...mine].slice(-200)); session.set("ytdl.fetched", [...fetched].slice(-200)); };
try { localStorage.removeItem("ytdl.mine"); localStorage.removeItem("ytdl.fetched"); } catch { /* restos viejos */ }

// ------------------------------------------------------------- idioma ---
document.querySelectorAll(".lang a").forEach((a) => a.addEventListener("click", (e) => {
  e.preventDefault();
  document.cookie = `lang=${a.dataset.lang}; path=/; max-age=31536000; samesite=lax`;
  location.reload();
}));

// ------------------------------------------------------------ formulario ---
const checked = (name, def) => document.querySelector(`input[name=${name}]:checked`)?.value ?? def;
const mode = () => checked("mode", "video");
const scope = () => checked("scope", "single");
const profile = () => (mode() === "video" ? $("#video-profile").value : $("#audio-profile").value);

function syncForm() {
  const video = mode() === "video";
  const sc = scope();
  $("#video-opts").classList.toggle("d-none", !video);
  $("#res-opts").classList.toggle("d-none", !video);
  $("#audio-opts").classList.toggle("d-none", video);
  $("#tab-opts").classList.toggle("d-none", sc !== "channel");
  $("#limit-opts").classList.toggle("d-none", sc === "single");
  let hint = T.profiles[mode()][profile()]?.hint || "";
  if (sc === "channel") hint += " " + L.channel_hint;
  $("#hint").textContent = hint.trim();
}
document.querySelectorAll("input[name=mode], input[name=scope]").forEach((r) => r.addEventListener("change", syncForm));
["#video-profile", "#audio-profile"].forEach((s) => $(s).addEventListener("change", syncForm));

const auto = $("#auto");
auto.checked = store.get("ytdl.auto", true);
auto.addEventListener("change", () => store.set("ytdl.auto", auto.checked));

function detectScope(u) {
  if (/youtube\.com\/(@|channel\/|c\/|user\/)/i.test(u)) return "channel";
  if (/youtube\.com\/playlist\?/i.test(u)) return "playlist";
  if (/[?&]list=/.test(u) && !/[?&]v=/.test(u)) return "playlist";
  return "single";
}

function onUrlChange() {
  const u = $("#url").value.trim();
  const note = $("#detect");
  note.classList.add("d-none");
  if (!u || !document.querySelector("input[name=scope]")) return;
  const sc = detectScope(u);
  $(`#scope-${sc}`).checked = true;
  if (sc === "channel") {
    note.innerHTML = `<i class="bi bi-person-video3"></i> ${esc(L.detect_channel)}`;
    note.classList.remove("d-none");
  } else if (sc === "single" && /[?&]list=/.test(u)) {
    note.innerHTML = `<i class="bi bi-collection-play"></i> ${esc(L.detect_playlist)}`;
    note.classList.remove("d-none");
  }
  syncForm();
}
$("#url").addEventListener("input", onUrlChange);

$("#paste").addEventListener("click", async () => {
  try {
    const text = await navigator.clipboard.readText();
    if (text) { $("#url").value = text.trim(); onUrlChange(); }
  } catch {
    toast(L.clip_fail, L.clip_hint, true);
  }
  $("#url").focus();
});

syncForm();

// --------------------------------------------------------------- utilidades ---
function fmtBytes(n) {
  if (n == null) return "";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
}

function fmtEta(s) {
  if (s == null) return "";
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
  return h ? `${h}h ${m}m` : m ? `${m}m ${ss}s` : `${ss}s`;
}

function toast(title, sub = "", bad = false) {
  const el = document.createElement("div");
  el.className = "toast-msg" + (bad ? " bad" : "");
  el.innerHTML = `<i class="bi ${bad ? "bi-exclamation-circle" : "bi-download"}"></i>
    <div><b>${esc(title)}</b><small>${esc(sub)}</small></div>`;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), 6000);
}

function saveUrl(j) {
  return j.files.length > 1 ? `api/jobs/${j.id}/zip` : `api/jobs/${j.id}/files/0`;
}

function triggerDownload(url) {
  const a = document.createElement("a");
  a.href = url;
  a.download = "";
  a.style.display = "none";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

// --------------------------------------------- textos de cada descarga ---
// el servidor manda claves ("dl:audio", "pp:Merger"…); aquí se traducen
function stageText(key) {
  if (!key) return "";
  const [kind, arg] = key.split(":");
  if (kind === "dl") return fmt(L.stage.dl, { kind: L.kind[arg] || arg });
  if (kind === "pp") return L.pp[arg] || fmt(L.stage.pp_other, { name: arg });
  return L.stage[key] || key;
}

function profileLabel(j) {
  const prof = T.profiles[j.mode]?.[j.profile];
  const parts = [`${L.mode[j.mode] || j.mode} · ${prof ? prof.label : j.profile}`];
  if (j.mode === "video" && j.resolution && j.resolution !== "max") parts.push(fmt(L.upto, { r: j.resolution }));
  if (j.scope === "channel") parts.push(fmt(L.scope_channel, { tab: T.tabs[j.tab] || j.tab }));
  else if (j.scope === "playlist") parts.push(L.scope_playlist);
  if (j.limit) parts.push(fmt(L.first_n, { n: j.limit }));
  return parts.join(" · ");
}

function errorText(j) {
  let msg = j.error_key ? (L.err_key[j.error_key] || j.error_key) : (j.error || "");
  if (j.error_hint && L.err_hint[j.error_hint]) msg += ` — ${L.err_hint[j.error_hint]}`;
  return msg;
}

// ------------------------------------------------------------- tarjetas ---
function progressBlock(j) {
  const queued = j.status === "queued";
  const pct = j.percent;
  let overall = "";
  const m = /^(\d+)\/(\d+)$/.exec(j.item || "");
  if (m) {
    const idx = +m[1], tot = +m[2];
    const o = Math.min(100, ((idx - 1 + (pct || 0) / 100) / tot) * 100);
    overall = `<div class="bar thin" title="${esc(L.overall)}"><div class="fill" style="width:${o.toFixed(1)}%"></div></div>`;
  }
  const stats = [
    pct != null && !queued ? `<span class="pct">${pct.toFixed(1)}%</span>` : "",
    j.total ? `<span>${esc(fmt(L.of, { a: fmtBytes(j.downloaded), b: fmtBytes(j.total) }))}</span>` : "",
    j.speed ? `<span>${fmtBytes(j.speed)}/s</span>` : "",
    j.eta != null ? `<span>${esc(fmt(L.left, { t: fmtEta(j.eta) }))}</span>` : "",
    j.item ? `<span>${esc(fmt(L.item, { i: j.item }))}</span>` : "",
  ].filter(Boolean).join("");
  return `
    <div class="stage">${esc(stageText(j.stage))}${j.current ? ` <span class="cur">· ${esc(j.current)}</span>` : ""}</div>
    <div class="bar ${pct == null || queued ? "indet" : ""}"><div class="fill" style="width:${pct || 0}%"></div></div>
    ${overall}
    ${stats ? `<div class="stats">${stats}</div>` : ""}`;
}

function filesBlock(j) {
  const files = j.files || [];
  if (!files.length) return "";
  const total = files.reduce((s, f) => s + f.size, 0);
  const rows = files.map((f, i) => `
    <li><span class="name">${esc(f.name)}</span>
      <span class="size">${fmtBytes(f.size)} · <a href="api/jobs/${j.id}/files/${i}">${esc(L.save)}</a></span></li>`).join("");
  return `<details>
      <summary>${files.length === 1 ? esc(files[0].name) : esc(fmt(L.files_n, { n: files.length }))} · ${fmtBytes(total)}</summary>
      <ul class="files">${rows}</ul>
    </details>`;
}

function cardBody(j) {
  const active = j.status === "queued" || j.status === "running";
  const files = j.files || [];
  // línea de specs estilo revista: "canal / audio · mp3 320 kbps / 1 con error"
  const meta = [
    j.uploader ? esc(j.uploader) : "",
    esc(profileLabel(j)),
    j.status === "done" && j.failed ? `<span class="warn">${esc(fmt(L.failed_n, { n: j.failed }))}</span>` : "",
  ].filter(Boolean).join('<span class="sep">/</span>');

  let actions = "";
  if (files.length && !active) {
    actions += `<a class="btn-save" href="${saveUrl(j)}"><i class="bi bi-arrow-down"></i>
      ${esc(files.length > 1 ? L.save_all : L.save)}</a>`;
  }
  actions += active
    ? `<button class="btn-line" data-act="cancel" data-id="${j.id}">${esc(L.cancel)}</button>`
    : `<button class="btn-line" data-act="delete" data-id="${j.id}">${esc(L.remove)}</button>`;

  const error = j.status === "error" ? `
    <div class="err">${esc(errorText(j))}</div>
    ${j.log && j.log.length ? `<details><summary>${esc(L.view_log)}</summary><pre class="log">${esc(j.log.join("\n"))}</pre></details>` : ""}` : "";

  return `
    <div class="job-top">
      <div>
        <div class="job-title">${esc(j.title || j.url)}</div>
        <div class="job-meta">${meta}</div>
      </div>
      <span class="stamp ${j.status}">${esc(L.status[j.status] || j.status)}</span>
    </div>
    ${active ? progressBlock(j) : ""}
    ${error}
    ${filesBlock(j)}
    <div class="job-actions">${actions}</div>`;
}

const cards = new Map(); // id -> {el, thumb, body, html, src}

function render(jobs) {
  const list = $("#jobs");
  const ids = new Set(jobs.map((j) => j.id));
  for (const [id, c] of cards) {
    if (!ids.has(id)) { c.el.remove(); cards.delete(id); }
  }
  jobs.forEach((j, i) => {
    let c = cards.get(j.id);
    if (!c) {
      const el = document.createElement("article");
      el.className = "job";
      el.innerHTML = `<div class="job-num"></div><div class="job-thumb"><i class="bi ${j.mode === "audio" ? "bi-music-note-beamed" : "bi-film"}"></i></div><div class="job-body"></div>`;
      c = { el, num: el.children[0], thumb: el.children[1], body: el.children[2], html: "", src: "" };
      cards.set(j.id, c);
    }
    if (list.children[i] !== c.el) list.insertBefore(c.el, list.children[i] || null);
    c.el.dataset.status = j.status;
    c.num.textContent = String(jobs.length - i).padStart(2, "0"); // la más vieja es la 01
    // la miniatura se maneja aparte para que no parpadee con cada actualización
    const src = j.thumbnail || "";
    if (src !== c.src) {
      c.thumb.querySelector("img")?.remove();
      if (src) {
        const img = new Image();
        img.alt = "";
        img.referrerPolicy = "no-referrer";
        img.onerror = () => img.remove();
        img.src = src;
        c.thumb.appendChild(img);
      }
      c.src = src;
    }
    const html = cardBody(j);
    if (html !== c.html) { c.body.innerHTML = html; c.html = html; }
  });
  $("#empty").classList.toggle("d-none", jobs.length > 0);
  $("#counter").textContent = jobs.length ? fmt(L.counter, { n: String(jobs.length).padStart(2, "0") }) : "";
}

// Al terminar una descarga pedida desde este navegador, se guarda sola.
function autoSave(jobs) {
  let changed = false;
  for (const j of jobs) {
    if (j.status !== "done" || !mine.has(j.id) || fetched.has(j.id)) continue;
    fetched.add(j.id);
    changed = true;
    if (!auto.checked || !(j.files || []).length) continue;
    triggerDownload(saveUrl(j));
    toast(j.files.length > 1 ? fmt(L.saving_zip, { n: j.files.length }) : L.saving,
      j.files.length > 1 ? (j.title || "") : j.files[0].name);
  }
  if (changed) saveSession();
}

// ------------------------------------------------------------- polling ---
let timer = null;

async function refresh() {
  clearTimeout(timer);
  let active = false;
  try {
    const r = await fetch("api/jobs", { cache: "no-store" });
    if (r.ok) {
      const jobs = await r.json();
      active = jobs.some((j) => j.status === "queued" || j.status === "running");
      render(jobs);
      autoSave(jobs);
    }
  } catch { /* reintenta en el siguiente ciclo */ }
  timer = setTimeout(refresh, active ? 1000 : 5000);
}

$("#form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("#go");
  btn.disabled = true;
  try {
    const r = await fetch("api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: $("#url").value.trim(),
        mode: mode(),
        profile: profile(),
        resolution: $("#res").value,
        scope: scope(),
        tab: $("#tab").value,
        limit: +$("#limit").value,
      }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || L.create_fail);
    mine.add(data.id);
    saveSession();
    $("#url").value = "";
    $("#detect").classList.add("d-none");
    refresh();
  } catch (err) {
    toast(L.start_fail, err.message, true);
  } finally {
    btn.disabled = false;
  }
});

$("#jobs").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  const { act, id } = btn.dataset;
  if (act === "cancel") {
    await fetch(`api/jobs/${id}/cancel`, { method: "POST" });
  } else if (act === "delete") {
    if (!confirm(L.confirm_remove)) return;
    const r = await fetch(`api/jobs/${id}`, { method: "DELETE" });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      toast(L.remove_fail, d.error || "", true);
    }
  }
  refresh();
});

refresh();

// ------------------------------------------------------------------ fondo ---
const B = L.bg;
const bgDialog = $("#bg-dialog");
const bgStatus = $("#bg-status");
const bgDrop = $("#bg-drop");
const MAX_MB = parseInt(bgDrop.dataset.maxMb, 10) || 20;

function bgSay(msg, bad = false) {
  bgStatus.textContent = msg;
  bgStatus.classList.toggle("bad", bad);
}

function applyFondo(f) {
  document.documentElement.style.setProperty("--field", f.field);
  $(".ad-bg").style.backgroundImage = `url("${f.url}")`;
  const prev = $("#bg-preview");
  prev.style.backgroundImage = `url("${f.url}")`;
  prev.style.backgroundColor = f.field;
  $("#bg-badge").textContent = f.custom ? B.yours : B.default;
  $("#bg-color").value = f.field;
  $("#bg-hex").textContent = f.field;
  $("#bg-auto").disabled = !f.override;
  $("#bg-reset").disabled = !f.custom && !f.override;
}

async function bgRequest(url, opts) {
  const r = await fetch(url, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || fmt(L.http, { s: r.status }));
  return data;
}

async function uploadFondo(file) {
  if (!file) return;
  if (file.type && !/^image\/(jpeg|png|webp|gif)$/.test(file.type)) {
    bgSay(B.bad_type, true);
    return;
  }
  if (file.size > MAX_MB * 1024 * 1024) {
    bgSay(fmt(B.too_big, { size: fmtBytes(file.size), max: MAX_MB }), true);
    return;
  }
  const fd = new FormData();
  fd.append("imagen", file);
  bgDrop.classList.add("busy");
  bgSay(B.uploading);
  try {
    applyFondo(await bgRequest("api/fondo", { method: "POST", body: fd }));
    bgSay(B.uploaded);
  } catch (err) {
    bgSay(err.message, true);
  } finally {
    bgDrop.classList.remove("busy");
    $("#bg-file").value = "";
  }
}

$("#bg-open").addEventListener("click", async () => {
  bgSay("");
  bgDialog.showModal();
  try { applyFondo(await bgRequest("api/fondo")); } catch { /* se queda lo que ya se ve */ }
});
bgDialog.addEventListener("click", (e) => { if (e.target === bgDialog) bgDialog.close(); }); // clic fuera = cerrar

$("#bg-file").addEventListener("change", (e) => uploadFondo(e.target.files[0]));
["dragenter", "dragover"].forEach((ev) => bgDrop.addEventListener(ev, (e) => { e.preventDefault(); bgDrop.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) => bgDrop.addEventListener(ev, (e) => { e.preventDefault(); bgDrop.classList.remove("over"); }));
bgDrop.addEventListener("drop", (e) => uploadFondo(e.dataTransfer.files[0]));

// color: se ve en vivo mientras lo mueves y se guarda al soltarlo
$("#bg-color").addEventListener("input", (e) => {
  document.documentElement.style.setProperty("--field", e.target.value);
  $("#bg-hex").textContent = e.target.value;
  $("#bg-preview").style.backgroundColor = e.target.value;
});
$("#bg-color").addEventListener("change", async (e) => {
  try {
    applyFondo(await bgRequest("api/fondo/color", {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ field: e.target.value }),
    }));
    bgSay(B.color_saved);
  } catch (err) { bgSay(err.message, true); }
});
$("#bg-auto").addEventListener("click", async () => {
  try {
    applyFondo(await bgRequest("api/fondo/color", {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ field: null }),
    }));
    bgSay(B.color_auto);
  } catch (err) { bgSay(err.message, true); }
});
$("#bg-reset").addEventListener("click", async () => {
  if (!confirm(B.confirm_reset)) return;
  try {
    applyFondo(await bgRequest("api/fondo", { method: "DELETE" }));
    bgSay(B.reset_done);
  } catch (err) { bgSay(err.message, true); }
});
