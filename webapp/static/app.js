// ClipForge app. Plain JavaScript, no build step.
// The API key lives in sessionStorage for this tab only. Clip files are fetched with the key
// and shown through object URLs, because <video> tags can't send custom headers.

(() => {
  "use strict";

  const KEY = "cf_api_key";
  const POLL_MS = 4000;
  const RENDER_POLL_MS = 3000;
  const RENDER_POLL_MAX = 80;
  const ALLOWED = [".mp4", ".mov", ".mkv", ".webm"];
  const ASPECTS = [
    { id: "9:16", label: "Vertical", note: "9:16 for Shorts, Reels and TikTok" },
    { id: "1:1", label: "Square", note: "1:1 for feed posts", paid: true },
    { id: "16:9", label: "Landscape", note: "16:9 for YouTube and widescreen", paid: true },
  ];
  const STYLES = [["highlight", "Highlight"], ["plain", "Plain"], ["signal", "Signal red"]];
  const STATUS_LABEL = { queued: "Queued", running: "Working", done: "Ready", failed: "Failed", none: "Not rendered" };

  const state = {
    me: null,
    plans: null,
    jobs: [],
    schedules: [],
    job: null,
    clips: [],
    jobId: null,
    sel: null,
    aspect: "9:16",
    draft: null,      // unsaved edits for the selected clip
    words: null,      // caption words for the selected clip
    dirty: false,
    sheet: false,     // phones: the editor is open full screen
    scheduleOpen: false,
    showUpload: false,
    file: null,
    uploading: false,
    blobs: new Map(), // request path -> object URL
    timer: null,
    renderTimer: null,
    renderPolls: 0,
    key: null,
  };

  const $ = (sel, root = document) => root.querySelector(sel);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const pad2 = (n) => String(n).padStart(2, "0");

  // Seconds as m:ss.s (or h:mm:ss for long videos)
  function clock(t) {
    t = Math.max(0, Number(t) || 0);
    const h = Math.floor(t / 3600);
    const m = Math.floor((t % 3600) / 60);
    const s = t % 60;
    return h ? `${h}:${pad2(m)}:${pad2(Math.floor(s))}` : `${m}:${s.toFixed(1).padStart(4, "0")}`;
  }
  const shortId = (id) => id.slice(0, 8);
  const fmtDate = (iso) => (iso ? new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "");
  const fmtMb = (mb) => (mb >= 1024 ? `${(mb / 1024).toFixed(0)} GB` : `${mb} MB`);
  const busy = (status) => status === "queued" || status === "running";

  // ---------- storage (best effort) ----------

  const store = {
    get() { try { return sessionStorage.getItem(KEY); } catch { return null; } },
    set(v) { try { sessionStorage.setItem(KEY, v); } catch { /* private mode: keep in memory only */ } },
    clear() { try { sessionStorage.removeItem(KEY); } catch { /* ignore */ } },
  };

  // ---------- API ----------

  async function api(path, opts = {}) {
    const headers = { "X-API-Key": state.key || "", ...(opts.headers || {}) };
    if (opts.body && !(opts.body instanceof FormData)) headers["Content-Type"] = "application/json";
    const res = await fetch(path, { ...opts, headers, body: opts.body && !(opts.body instanceof FormData) ? JSON.stringify(opts.body) : opts.body });
    if (res.status === 401) {
      signOut("Your API key wasn't accepted. Check it and sign in again.");
      throw new Error("unauthorized");
    }
    if (res.status === 204) return null;
    const isJson = (res.headers.get("content-type") || "").includes("application/json");
    const body = isJson ? await res.json() : null;
    if (!res.ok) throw new Error((body && body.detail) || `Request failed (${res.status})`);
    return body;
  }

  async function fileUrl(path) {
    if (state.blobs.has(path)) return state.blobs.get(path);
    const res = await fetch(path, { headers: { "X-API-Key": state.key || "" } });
    if (!res.ok) {
      const body = (res.headers.get("content-type") || "").includes("json") ? await res.json() : {};
      throw new Error(body.detail || "Not available yet");
    }
    const url = URL.createObjectURL(await res.blob());
    state.blobs.set(path, url);
    return url;
  }

  async function downloadFile(path, filename) {
    const res = await fetch(path, { headers: { "X-API-Key": state.key || "" } });
    if (!res.ok) {
      const body = (res.headers.get("content-type") || "").includes("json") ? await res.json() : {};
      throw new Error(body.detail || "Not available yet");
    }
    const url = URL.createObjectURL(await res.blob());
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
  }

  function forgetClipFiles(jobId, n) {
    for (const [path, url] of state.blobs) {
      if (path.startsWith(`/v1/jobs/${jobId}/clips/${n}.mp4`)) {
        URL.revokeObjectURL(url);
        state.blobs.delete(path);
      }
    }
  }

  function uploadFile(file, { top, onProgress }) {
    return new Promise((resolve, reject) => {
      const fd = new FormData();
      fd.append("file", file, file.name);
      fd.append("rights_confirmed", "true");
      fd.append("top", String(top));
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/v1/jobs");
      xhr.setRequestHeader("X-API-Key", state.key || "");
      xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(e.loaded / e.total); };
      xhr.onload = () => {
        let body = null;
        try { body = JSON.parse(xhr.responseText); } catch { /* not JSON */ }
        if (xhr.status === 401) { signOut("Your API key wasn't accepted."); return reject(new Error("unauthorized")); }
        if (xhr.status >= 200 && xhr.status < 300) resolve(body);
        else reject(new Error((body && body.detail) || `Upload failed (${xhr.status})`));
      };
      xhr.onerror = () => reject(new Error("Network error during upload. Check your connection and try again."));
      xhr.send(fd);
    });
  }

  // ---------- toasts ----------

  function toast(message, tone = "") {
    const node = document.createElement("div");
    node.className = `toast ${tone === "bad" ? "error" : ""}`;
    node.textContent = message;
    $("#toasts").append(node);
    setTimeout(() => node.remove(), 5200);
  }

  // ---------- screens and routing ----------

  function showScreen(name) {
    $("#signIn").hidden = name !== "signin";
    $("#shell").hidden = name !== "app";
  }

  function signOut(message) {
    if (state.key) {
      // End this sign-in on the server too. Best effort: the tab signs out either way.
      fetch("/v1/auth/session", { method: "DELETE", headers: { "X-API-Key": state.key } }).catch(() => {});
    }
    state.key = null;
    store.clear();
    clearTimeout(state.timer);
    clearTimeout(state.renderTimer);
    state.blobs.forEach((u) => URL.revokeObjectURL(u));
    state.blobs.clear();
    state.me = null;
    state.sheet = false;
    state.dirty = false;
    showScreen("signin");
    if (message) $("#signInError").textContent = message;
  }

  function parseRoute() {
    const [section, id] = location.hash.replace(/^#\/?/, "").split("/");
    if (section === "schedule") return { name: "schedule" };
    if (section === "account") return { name: "account" };
    if (section === "clips" && id) return { name: "job", id: decodeURIComponent(id) };
    return { name: "clips" };
  }

  const TITLES = { clips: "Clips", job: "Stream", schedule: "Schedule", account: "Account" };

  function setNav(name) {
    document.querySelectorAll("[data-nav]").forEach((a) => {
      if (a.dataset.nav === name) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    });
  }

  function syncSheet() {
    const open = state.sheet && window.innerWidth < 1000;
    document.body.dataset.sheet = open ? "1" : "0";
    const review = document.getElementById("review");
    if (review) review.dataset.open = open ? "1" : "0";
  }

  function setTitle(text) {
    $("#pageTitle").textContent = text;
    document.title = `${text} · ClipForge`;
  }

  async function route() {
    if (!state.key) return;
    clearTimeout(state.timer);
    clearTimeout(state.renderTimer);
    state.renderPolls = 0;
    const r = parseRoute();
    setNav(r.name === "job" ? "clips" : r.name);
    $("#topAction").replaceChildren();
    if (r.name !== "job") { state.sheet = false; state.jobId = null; }
    try {
      if (r.name === "clips") await showClips();
      if (r.name === "job") await showJob(r.id);
      if (r.name === "schedule") await showSchedule();
      if (r.name === "account") await showAccount();
    } catch (err) {
      if (err.message !== "unauthorized") toast(err.message, "bad");
    }
  }

  // ---------- account summary (rail, uploads) ----------

  async function refreshMe() {
    state.me = await api("/v1/me");
    const me = state.me;
    $("#railPlan").textContent = `${me.plan_name} plan · ${Math.round(me.minutes_used)} of ${me.minutes_limit} min used`;
  }

  // ---------- library ----------

  async function showClips() {
    setTitle(TITLES.clips);
    const [jobs] = await Promise.all([api("/v1/jobs"), refreshMe()]);
    state.jobs = jobs;
    $("#topAction").innerHTML = `<button class="btn primary compact" type="button" data-action="toggle-upload">${state.showUpload ? "Close" : "New job"}</button>`;
    const me = state.me;
    $("#view").innerHTML = `
      <p class="lede">Upload a stream you own. ClipForge ranks the strongest moments and cuts them into captioned clips.</p>
      <div id="uploadSlot" class="${state.showUpload ? "" : "hidden"}">${uploadPanel(me)}</div>
      <div class="jobs" id="jobList" aria-live="polite"></div>`;
    renderJobList();
    wireUpload();
    schedulePollJobs();
  }

  function uploadPanel(me) {
    const max = me ? me.max_clips : 10;
    const start = Math.min(10, max);
    const title = state.file ? esc(state.file.name) : "Drop a stream here";
    const sub = state.file ? `${(state.file.size / 1048576).toFixed(1)} MB` : `${ALLOWED.map((e) => e.slice(1).toUpperCase()).join(", ")} · up to ${me ? fmtMb(me.max_upload_mb) : "2 GB"}`;
    return `
      <form class="upload" id="uploadForm" novalidate>
        <label class="dropzone" id="dropzone" tabindex="0">
          <input id="fileInput" type="file" accept=".mp4,.mov,.mkv,.webm,video/*" hidden>
          <strong id="dropTitle">${title}</strong>
          <span class="dust" id="dropSub">${sub}</span>
        </label>
        <p class="form-error" id="fileError" role="alert"></p>
        <div class="upload-row">
          <label class="field">
            <span>Clips to make: <strong id="clipCountLabel">${start}</strong></span>
            <input id="clipCount" type="range" min="1" max="${max}" value="${start}">
          </label>
          <button class="btn primary" id="submitBtn" type="submit" disabled>Create clips</button>
        </div>
        <label class="field" style="grid-template-columns:auto 1fr;align-items:start;gap:10px">
          <input id="rights" type="checkbox">
          <span class="dust small">I own this footage or have permission to clip it, and I accept the <a href="/terms" target="_blank" rel="noopener">terms of service</a>.</span>
        </label>
        <div class="progress" id="uploadProgress" hidden><span id="uploadBar"></span></div>
        <p class="dust small" id="uploadText" hidden></p>
      </form>`;
  }

  function jobCard(job) {
    const label = { queued: "Queued", running: "Working", done: "Done", failed: "Failed" }[job.status] || job.status;
    const meta = `${Math.round(job.source_minutes)} min source · ${job.status === "done" ? `${job.clip_count} clips` : "clips appear when it's done"} · ${fmtDate(job.created_at)}`;
    return `
      <a class="job-card" href="#/clips/${encodeURIComponent(job.id)}">
        <div class="row">
          <span class="title">Stream ${shortId(job.id)}</span>
          <span class="pill ${job.status}">${label}</span>
        </div>
        <div class="meta">${esc(meta)}</div>
        ${job.status === "failed" ? `<div class="form-error">${esc(job.error || "Something went wrong while processing this video.")}</div>` : ""}
        ${busy(job.status) ? `<div class="meta">Working on it. This list updates by itself.</div>` : ""}
      </a>`;
  }

  function renderJobList() {
    const list = $("#jobList");
    if (!list) return;
    if (state.jobs.length === 0) {
      list.innerHTML = `<div class="empty"><p>No streams yet. Upload one to get your first ranked clips.</p></div>`;
      return;
    }
    list.innerHTML = state.jobs.map(jobCard).join("");
  }

  function schedulePollJobs() {
    clearTimeout(state.timer);
    if (parseRoute().name === "clips" && state.jobs.some((j) => busy(j.status))) {
      state.timer = setTimeout(() => {
        if (parseRoute().name !== "clips") return;
        api("/v1/jobs").then((jobs) => { state.jobs = jobs; renderJobList(); schedulePollJobs(); }).catch(() => {});
      }, POLL_MS);
    }
  }

  function toggleUpload() {
    state.showUpload = !state.showUpload;
    const slot = $("#uploadSlot");
    if (slot) slot.classList.toggle("hidden", !state.showUpload);
    const btn = document.querySelector('[data-action="toggle-upload"]');
    if (btn) btn.textContent = state.showUpload ? "Close" : "New job";
  }

  function setFile(file) {
    const error = $("#fileError");
    error.textContent = "";
    state.file = null;
    if (file) {
      const ext = (file.name.match(/\.[^.]+$/) || [""])[0].toLowerCase();
      if (!ALLOWED.includes(ext)) error.textContent = `Unsupported file type. Use ${ALLOWED.join(", ")}.`;
      else if (state.me && file.size > state.me.max_upload_mb * 1048576) error.textContent = `This file is larger than your plan's ${fmtMb(state.me.max_upload_mb)} limit.`;
      else state.file = file;
    }
    $("#dropTitle").textContent = state.file ? state.file.name : "Drop a stream here";
    $("#dropSub").textContent = state.file ? `${(state.file.size / 1048576).toFixed(1)} MB` : "Choose a file to continue";
    updateSubmit();
  }

  function updateSubmit() {
    const btn = $("#submitBtn");
    if (btn) btn.disabled = !(state.file && $("#rights").checked) || state.uploading;
  }

  function setUploading(on, progress = 0) {
    state.uploading = on;
    $("#uploadProgress").hidden = !on;
    $("#uploadBar").style.width = `${Math.round(progress * 100)}%`;
    const text = $("#uploadText");
    text.hidden = !on;
    text.textContent = progress >= 1 ? "Upload complete. Queuing…" : `Uploading… ${Math.round(progress * 100)}%`;
    updateSubmit();
  }

  async function submitUpload(e) {
    e.preventDefault();
    if (!state.file || !$("#rights").checked || state.uploading) return;
    setUploading(true, 0);
    try {
      const job = await uploadFile(state.file, {
        top: Number($("#clipCount").value),
        onProgress: (p) => setUploading(true, p),
      });
      toast(`Stream ${shortId(job.id)} is queued.`, "good");
      state.file = null;
      state.showUpload = false;
      state.jobs = await api("/v1/jobs");
      await showClips();
    } catch (err) {
      if (err.message !== "unauthorized") toast(err.message, "bad");
      setUploading(false);
    }
  }

  function wireUpload() {
    const zone = $("#dropzone");
    if (!zone) return;
    const input = $("#fileInput");
    zone.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); } });
    input.addEventListener("change", () => setFile(input.files[0]));
    ["dragenter", "dragover"].forEach((t) => zone.addEventListener(t, (e) => { e.preventDefault(); zone.classList.add("over"); }));
    ["dragleave", "drop"].forEach((t) => zone.addEventListener(t, (e) => { e.preventDefault(); zone.classList.remove("over"); }));
    zone.addEventListener("drop", (e) => setFile(e.dataTransfer.files[0]));
    $("#rights").addEventListener("change", updateSubmit);
    $("#clipCount").addEventListener("input", (e) => { $("#clipCountLabel").textContent = e.target.value; });
    $("#uploadForm").addEventListener("submit", submitUpload);
    updateSubmit();
  }

  // ---------- one stream: timeline, clip ranking and the editor ----------

  async function showJob(id) {
    state.jobId = id;
    state.job = null;
    setTitle(TITLES.job);
    const [job] = await Promise.all([api(`/v1/jobs/${encodeURIComponent(id)}`), refreshMe()]);
    state.job = job;
    state.clips = job.status === "done" ? await api(`/v1/jobs/${encodeURIComponent(id)}/clips`) : [];
    setTitle(`Stream ${shortId(job.id)}`);
    $("#topAction").innerHTML = job.status === "done" && state.clips.length
      ? `<button class="btn compact" type="button" data-action="zip" aria-label="Download all ${state.aspect} clips as a zip">Zip</button>` : "";
    if (state.sel && state.sel > state.clips.length) state.sel = null;
    if (!state.sel && state.clips.length) state.sel = 1;
    renderJob();
    if (state.sel) await loadWords();
    renderEditor();
    syncSheet();
    if (busy(job.status)) scheduleJobPoll(id);
  }

  function scheduleJobPoll(id) {
    clearTimeout(state.timer);
    state.timer = setTimeout(async () => {
      if (parseRoute().name !== "job" || state.dirty) return;
      try { await showJob(id); } catch { /* network blip: try again next time */ }
    }, POLL_MS);
  }

  function renderJob() {
    const job = state.job;
    const clips = state.clips;
    const duration = Math.max(1, Math.round(job.source_minutes * 60));
    const body = busy(job.status)
      ? `<div class="empty"><p>${job.status === "queued" ? "Queued. Clips appear here when processing finishes." : "Finding the strongest moments. This page updates by itself."}</p></div>`
      : job.status === "failed"
        ? `<div class="empty"><p class="form-error">${esc(job.error || "This stream couldn't be processed.")}</p></div>`
        : clips.length === 0
          ? `<div class="empty"><p>No strong moments were found in this stream.</p></div>`
          : "";
    $("#view").innerHTML = `
      <div class="summary">
        <div class="summary-meta">
          <span class="pill ${job.status}">${{ queued: "Queued", running: "Working", done: "Ready", failed: "Failed" }[job.status]}</span>
          <span>${Math.round(job.source_minutes)} min source</span>
          <span>${clips.length} clips ranked</span>
          <span>${fmtDate(job.created_at)}</span>
        </div>
        <div class="timeline">
          <div class="tl-track" id="timeline" role="group" aria-label="Where each clip sits in the stream">
            ${clips.map((c) => timelineSegment(c, duration)).join("")}
          </div>
          <div class="tl-scale"><span>0:00</span><span>${clock(duration / 2)}</span><span>${clock(duration)}</span></div>
        </div>
      </div>
      ${body}
      ${clips.length ? `
      <div class="review" id="review" data-open="0">
        <div class="clip-list" role="list">${clips.map(clipRow).join("")}</div>
        <section class="editor" id="editor" aria-label="Clip editor"></section>
      </div>` : ""}`;
  }

  function segmentBounds(c, duration) {
    const isSel = c.n === state.sel && state.draft;
    const start = isSel ? state.draft.start : c.start;
    const end = isSel ? state.draft.end : c.end;
    return { left: (start / duration) * 100, width: Math.max(0.6, ((end - start) / duration) * 100) };
  }

  function timelineSegment(c, duration) {
    const b = segmentBounds(c, duration);
    const selected = c.n === state.sel;
    return `<button class="tl-seg" type="button" data-action="select" data-n="${c.n}" data-seg="${c.n}"
      style="left:${b.left.toFixed(3)}%;width:${b.width.toFixed(3)}%;opacity:${0.35 + (c.score / 100) * 0.65}"
      aria-pressed="${selected}" aria-label="Clip ${c.n}, score ${Math.round(c.score)}"></button>`;
  }

  function clipRow(c) {
    const selected = c.n === state.sel;
    return `
      <button class="clip-row" type="button" role="listitem" data-action="select" data-n="${c.n}" aria-pressed="${selected}">
        <span class="score-ring" style="--s:${Math.round(c.score)}"><span>${Math.round(c.score)}</span></span>
        <span>
          <span class="clip-title" style="display:block">${esc(c.title)}</span>
          <span class="clip-hook" style="display:block">“${esc(c.hook)}”</span>
          <span class="clip-meta">
            <span>${clock(c.start)}–${clock(c.end)}</span>
            <span>${Math.round(c.end - c.start)}s</span>
            ${c.edited ? '<span class="edited">edited</span>' : ""}
          </span>
        </span>
      </button>`;
  }

  async function selectClip(n) {
    if (state.dirty && n !== state.sel && !confirm("Discard your unsaved changes to this clip?")) return;
    const clip = state.clips.find((c) => c.n === n);
    if (!clip) return;
    state.sel = n;
    state.draft = { start: clip.start, end: clip.end, style: clip.caption_style, words: {} };
    state.dirty = false;
    state.scheduleOpen = false;
    state.sheet = true;
    state.words = null;
    if (window.innerWidth >= 1000) state.sheet = false;
    syncSheet();
    refreshListSelection();
    await loadWords();
    renderEditor();
    syncSheet();
    updateTimelineSegments();
  }

  function refreshListSelection() {
    document.querySelectorAll(".clip-row").forEach((b) => b.setAttribute("aria-pressed", String(Number(b.dataset.n) === state.sel)));
    document.querySelectorAll(".tl-seg").forEach((b) => b.setAttribute("aria-pressed", String(Number(b.dataset.n) === state.sel)));
  }

  function updateTimelineSegments() {
    if (!state.job) return;
    const duration = Math.max(1, Math.round(state.job.source_minutes * 60));
    state.clips.forEach((c) => {
      const seg = document.querySelector(`[data-seg="${c.n}"]`);
      if (!seg) return;
      const b = segmentBounds(c, duration);
      seg.style.left = `${b.left.toFixed(3)}%`;
      seg.style.width = `${b.width.toFixed(3)}%`;
    });
  }

  function selectedClip() {
    return state.clips.find((c) => c.n === state.sel) || null;
  }

  async function loadWords() {
    const c = selectedClip();
    state.words = null;
    if (!c) return;
    try {
      state.words = await api(`/v1/jobs/${encodeURIComponent(state.jobId)}/clips/${c.n}/words`);
    } catch {
      state.words = [];
    }
  }

  function variantStatus(c, aspect) {
    return (c.variants && c.variants[aspect] && c.variants[aspect].status) || "none";
  }

  function renderEditor() {
    const editor = $("#editor");
    if (!editor) return;
    const c = selectedClip();
    if (!c || !state.draft) {
      editor.innerHTML = `<div class="empty"><p>Pick a clip to edit it.</p></div>`;
      return;
    }
    const d = state.draft;
    const status = variantStatus(c, state.aspect);
    const me = state.me || {};
    const allowed = (me.aspects || ["9:16"]);
    const canRender = state.dirty || status === "none" || status === "failed";
    const words = state.words || [];
    const wordChips = words.length
      ? words.map((w) => {
          const shown = d.words[w.i] !== undefined ? d.words[w.i] : w.word;
          const edited = shown !== w.original;
          return `<button class="word ${edited ? "edited" : ""}" type="button" data-action="edit-word" data-i="${w.i}" aria-label="Edit word ${esc(w.original)}">${esc(shown)}</button>`;
        }).join("")
      : `<p class="muted-note">No caption words in this range. Widen the clip to add captions.</p>`;
    const dur = d.end - d.start;
    const durClass = dur < 3 || dur > 90 ? "form-error" : "dust";

    editor.innerHTML = `
      <div class="editor-head">
        <div>
          <h2>${esc(c.title)}</h2>
          <p class="hint-line">Clip ${c.n} · score ${Math.round(c.score)} · <span class="${durClass}">${dur.toFixed(1)}s</span></p>
        </div>
        <span class="pill ${status}">${STATUS_LABEL[status] || status} · ${state.aspect}</span>
      </div>

      <div class="stage">
        <div class="preview" id="preview" data-aspect="${state.aspect}">${previewPlaceholder(status)}</div>
      </div>

      <div>
        <p class="section-label">Length</p>
        <div class="trim">
          ${trimLine("start", "Start", d.start)}
          ${trimLine("end", "End", d.end)}
        </div>
        <p class="muted-note">Clips run 3 to 90 seconds. Nudges move the cut in small steps.</p>
      </div>

      <div>
        <p class="section-label">Caption style</p>
        <div class="seg" role="group" aria-label="Caption style">
          ${STYLES.map(([id, label]) => `<button type="button" data-action="style" data-style="${id}" aria-pressed="${d.style === id}">${label}</button>`).join("")}
        </div>
      </div>

      <div>
        <p class="section-label">Format</p>
        <div class="seg" role="group" aria-label="Aspect ratio">
          ${ASPECTS.map((a) => {
            const locked = !allowed.includes(a.id);
            return `<button type="button" data-action="aspect" data-aspect="${a.id}" aria-pressed="${state.aspect === a.id}" ${locked ? "disabled" : ""} title="${locked ? "Included on paid plans" : a.note}">${a.label}${locked ? '<small>paid</small>' : ""}</button>`;
          }).join("")}
        </div>
        <p class="muted-note" style="margin-top:8px">${ASPECTS.find((a) => a.id === state.aspect).note}</p>
      </div>

      <div>
        <p class="section-label">Captions. Tap a word to correct it.</p>
        <div class="words">${wordChips}</div>
      </div>

      <div class="copy-block">
        <p class="section-label">Why it was picked</p>
        <p class="reason">${esc(c.reason || "The model judged this stretch as a strong standalone moment.")}</p>
        <p class="section-label">Hook</p>
        <p>“${esc(c.hook)}”</p>
        <p class="section-label">Caption for the post</p>
        <p>${esc(c.caption)}</p>
        <div class="tags">${(c.hashtags || []).map((t) => `<span class="tag">${esc(t)}</span>`).join("")}</div>
      </div>

      ${state.scheduleOpen ? scheduleForm(c) : ""}

      <div class="actions">
        <span class="dirty" id="dirtyNote">${state.dirty ? "Unsaved changes" : ""}</span>
        <button class="btn primary" type="button" data-action="save" ${canRender ? "" : "disabled"}>${state.dirty ? "Save and render" : `Render ${state.aspect}`}</button>
        <button class="btn" type="button" data-action="download" ${status === "done" ? "" : "disabled"}>Download</button>
        <span class="spacer"></span>
        <button class="btn quiet" type="button" data-action="toggle-schedule">${state.scheduleOpen ? "Hide scheduling" : "Schedule post"}</button>
        <button class="btn quiet" type="button" data-action="copy" data-text="${esc(c.caption + " " + (c.hashtags || []).join(" "))}">Copy caption</button>
      </div>`;

    loadPreview(c, status);
    syncSheet();
  }

  function trimLine(edge, label, value) {
    return `
      <div class="trim-line">
        <span class="label">${label}</span>
        <span class="mono" id="${edge}Time">${clock(value)}</span>
        <div class="nudges" role="group" aria-label="Nudge ${label.toLowerCase()}">
          <button class="btn compact" type="button" data-action="nudge" data-edge="${edge}" data-d="-2">−2s</button>
          <button class="btn compact" type="button" data-action="nudge" data-edge="${edge}" data-d="-0.5">−0.5</button>
          <button class="btn compact" type="button" data-action="nudge" data-edge="${edge}" data-d="0.5">+0.5</button>
          <button class="btn compact" type="button" data-action="nudge" data-edge="${edge}" data-d="2">+2s</button>
        </div>
      </div>`;
  }

  function previewPlaceholder(status) {
    const text = {
      none: "Not rendered in this format yet. Render it to preview.",
      queued: "Queued for rendering…",
      running: "Rendering. This preview appears when it's ready.",
      failed: "This render failed. Change something and try again.",
    }[status];
    return text ? `<div class="placeholder">${esc(text)}</div>` : `<div class="placeholder">Loading preview…</div>`;
  }

  async function loadPreview(c, status) {
    if (status !== "done") return;
    const holder = $("#preview");
    if (!holder) return;
    const aspect = state.aspect;
    try {
      const url = await fileUrl(`/v1/jobs/${encodeURIComponent(state.jobId)}/clips/${c.n}.mp4?aspect=${encodeURIComponent(aspect)}`);
      if (state.sel !== c.n || state.aspect !== aspect) return;
      holder.innerHTML = `<video src="${url}" controls playsinline loop muted preload="metadata" aria-label="Preview of clip ${c.n}"></video>`;
    } catch (err) {
      holder.innerHTML = `<div class="placeholder">${esc(err.message)}</div>`;
    }
  }

  // draft edits
  function nudge(edge, delta) {
    const c = selectedClip();
    if (!c || !state.draft) return;
    const d = state.draft;
    if (edge === "start") d.start = Math.max(0, Math.round((d.start + delta) * 10) / 10);
    else d.end = Math.max(0, Math.round((d.end + delta) * 10) / 10);
    if (d.end - d.start > 90) { if (edge === "start") d.start = d.end - 90; else d.end = d.start + 90; }
    if (d.end - d.start < 3) { if (edge === "start") d.start = d.end - 3; else d.end = d.start + 3; }
    state.dirty = true;
    renderEditor();
    updateTimelineSegments();
  }

  function setStyle(style) {
    if (!state.draft) return;
    state.draft.style = style;
    state.dirty = true;
    renderEditor();
  }

  function setAspect(aspect) {
    state.aspect = aspect;
    renderEditor();
    const c = selectedClip();
    if (c && ["queued", "running"].includes(variantStatus(c, aspect))) pollRenders();
  }

  function editWord(i) {
    const btn = document.querySelector(`[data-action="edit-word"][data-i="${i}"]`);
    const word = (state.words || []).find((w) => w.i === i);
    if (!btn || !word || !state.draft) return;
    const current = state.draft.words[i] !== undefined ? state.draft.words[i] : word.word;
    const input = document.createElement("input");
    input.className = "word-input";
    input.type = "text";
    input.value = current;
    input.maxLength = 40;
    input.setAttribute("aria-label", `Replace ${word.original}`);
    let done = false;
    const commit = (keep) => {
      if (done) return;
      done = true;
      if (keep) {
        const value = input.value.trim();
        if (value && value !== current) {
          if (value === word.original) delete state.draft.words[i];
          else state.draft.words[i] = value;
          state.dirty = true;
        }
      }
      renderEditor();
    };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); commit(true); }
      if (e.key === "Escape") { e.preventDefault(); commit(false); }
    });
    input.addEventListener("blur", () => commit(true));
    btn.replaceWith(input);
    input.focus();
    input.select();
  }

  // ---------- rendering and saving ----------

  async function saveRender() {
    const c = selectedClip();
    if (!c || !state.draft) return;
    const btn = document.querySelector('[data-action="save"]');
    if (btn) btn.disabled = true;
    try {
      const d = state.draft;
      const updated = await api(`/v1/jobs/${encodeURIComponent(state.jobId)}/clips/${c.n}`, {
        method: "PATCH",
        body: {
          start: d.start,
          end: d.end,
          caption_style: d.style,
          word_edits: d.words,
          aspect: state.aspect,
        },
      });
      state.clips = state.clips.map((x) => (x.n === c.n ? updated : x));
      forgetClipFiles(state.jobId, c.n);
      state.draft = { start: updated.start, end: updated.end, style: updated.caption_style, words: {} };
      state.dirty = false;
      state.words = null;
      await loadWords();
      renderJob();
      renderEditor();
      updateTimelineSegments();
      refreshListSelection();
      syncSheet();
      toast(`Rendering ${state.aspect}. This preview updates when it's ready.`, "good");
      pollRenders();
    } catch (err) {
      if (err.message !== "unauthorized") toast(err.message, "bad");
      renderEditor();
    }
  }

  function pollRenders() {
    clearTimeout(state.renderTimer);
    state.renderPolls = 0;
    const tick = async () => {
      if (parseRoute().name !== "job" || !state.jobId) return;
      state.renderPolls += 1;
      try {
        const clips = await api(`/v1/jobs/${encodeURIComponent(state.jobId)}/clips`);
        const c = clips.find((x) => x.n === state.sel);
        const before = c ? variantStatus(c, state.aspect) : null;
        state.clips = clips;
        const now = c ? variantStatus(c, state.aspect) : null;
        if (!state.dirty) renderEditor();
        if (before && now !== before && now === "failed") toast(`Render failed: ${c.variants[state.aspect].error || "try again"}`, "bad");
        if (now === "queued" || now === "running") {
          if (state.renderPolls < RENDER_POLL_MAX) state.renderTimer = setTimeout(tick, RENDER_POLL_MS);
        }
      } catch { /* try again on the next tick */ }
    };
    state.renderTimer = setTimeout(tick, RENDER_POLL_MS);
  }

  async function downloadClip() {
    const c = selectedClip();
    if (!c) return;
    try {
      await downloadFile(`/v1/jobs/${encodeURIComponent(state.jobId)}/clips/${c.n}.mp4?aspect=${encodeURIComponent(state.aspect)}`,
        `clip_${shortId(state.jobId)}_${c.n}_${state.aspect.replace(":", "x")}.mp4`);
    } catch (err) {
      if (err.message !== "unauthorized") toast(err.message, "bad");
    }
  }

  async function downloadZip() {
    try {
      await downloadFile(`/v1/jobs/${encodeURIComponent(state.jobId)}/clips.zip?aspect=${encodeURIComponent(state.aspect)}`,
        `clips_${shortId(state.jobId)}_${state.aspect.replace(":", "x")}.zip`);
    } catch (err) {
      if (err.message !== "unauthorized") toast(err.message, "bad");
    }
  }

  function copyText(text) {
    if (!navigator.clipboard) { toast("Copying isn't available in this browser.", "bad"); return; }
    navigator.clipboard.writeText(text).then(() => toast("Caption copied."), () => toast("Couldn't copy. Select the text instead.", "bad"));
  }

  // ---------- scheduling ----------

  function toggleSchedule() {
    state.scheduleOpen = !state.scheduleOpen;
    renderEditor();
  }

  function scheduleForm(c) {
    const min = new Date(Date.now() + 3 * 60000);
    min.setSeconds(0, 0);
    const minValue = new Date(min - min.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
    const plans = state.me ? state.me.publishing : false;
    return `
      <form class="schedule-form" id="scheduleForm">
        <label class="field"><span>Platform</span>
          <select name="platform" required><option value="youtube">YouTube Shorts</option><option value="tiktok">TikTok</option></select>
        </label>
        <label class="field"><span>When</span>
          <input name="when" type="datetime-local" min="${minValue}" required>
        </label>
        <button class="btn primary" type="submit">Schedule</button>
        <p class="muted-note" style="grid-column:1/-1">${plans
          ? "Scheduled posts wait for platform approval. Nothing is posted until then."
          : "Scheduling is included on Pro and Business."}</p>
      </form>`;
  }

  async function submitSchedule(e) {
    e.preventDefault();
    const c = selectedClip();
    if (!c) return;
    const fd = new FormData(e.target);
    try {
      await api("/v1/schedules", {
        method: "POST",
        body: {
          job_id: state.jobId,
          clip_n: c.n,
          platform: fd.get("platform"),
          scheduled_at: new Date(fd.get("when")).toISOString(),
        },
      });
      toast("Scheduled. It's waiting for platform approval.", "good");
      state.scheduleOpen = false;
      renderEditor();
    } catch (err) {
      if (err.message !== "unauthorized") toast(err.message, "bad");
    }
  }

  const SCHEDULE_PILL = {
    scheduled: { label: "Scheduled", cls: "queued" },
    waiting_approval: { label: "Waiting for platform approval", cls: "queued" },
    posted: { label: "Posted", cls: "done" },
    failed: { label: "Didn't post", cls: "failed" },
  };

  async function showSchedule() {
    setTitle(TITLES.schedule);
    const [schedules] = await Promise.all([api("/v1/schedules"), refreshMe()]);
    state.schedules = schedules;
    const note = `<p class="lede">Posts you've queued for YouTube and TikTok. They go out once each platform approves ClipForge. Until then, nothing is posted.</p>`;
    if (!schedules.length) {
      $("#view").innerHTML = `${note}<div class="empty"><p>Nothing scheduled. Open a clip and choose Schedule post.</p></div>`;
      return;
    }
    $("#view").innerHTML = `${note}<div class="list">${schedules.map((s) => `
      <div class="list-row">
        <div>
          <strong>${s.platform === "youtube" ? "YouTube Shorts" : "TikTok"}</strong>
          <div class="dust small">Clip ${s.clip_n} from stream ${shortId(s.job_id)} · ${fmtDate(s.scheduled_at)}</div>
        </div>
        <span class="pill ${SCHEDULE_PILL[s.status]?.cls || "queued"}" title="${esc(s.error || "")}">${esc(SCHEDULE_PILL[s.status]?.label || s.status)}</span>
        ${s.error ? `<p class="dust small list-error">${esc(s.error)}</p>` : ""}
        ${s.status === "posted" ? "" : `<button class="btn compact" type="button" data-action="delete-schedule" data-id="${s.id}">Remove</button>`}
      </div>`).join("")}</div>`;
  }

  async function deleteSchedule(id) {
    try {
      await api(`/v1/schedules/${encodeURIComponent(id)}`, { method: "DELETE" });
      toast("Removed from your schedule.");
      await showSchedule();
    } catch (err) {
      if (err.message !== "unauthorized") toast(err.message, "bad");
    }
  }

  // ---------- account ----------

  async function showAccount() {
    setTitle(TITLES.account);
    await refreshMe();
    const me = state.me;
    const pct = Math.min(100, (me.minutes_used / me.minutes_limit) * 100);
    const left = Math.max(0, Math.round(me.minutes_limit - me.minutes_used));
    const items = [
      [true, `Up to ${me.max_clips} clips per stream`],
      [true, `Uploads up to ${fmtMb(me.max_upload_mb)}`],
      [!me.watermark, me.watermark ? "Logo on every clip (paid plans remove it)" : "No logo on clips"],
      [me.publishing, me.publishing ? "Publishing and scheduling (after platform approval)" : "Publishing and scheduling"],
      [me.aspects.length > 1, me.aspects.length > 1 ? "Square and landscape exports" : "Square and landscape exports"],
    ];
    $("#view").innerHTML = `
      <div class="account-grid">
        <section class="stat-card">
          <p class="dust">${esc(me.plan_name)} plan · this month</p>
          <p class="big">${left}</p>
          <p class="dust">source minutes left of ${me.minutes_limit}</p>
          <div class="meter" aria-hidden="true"><span style="width:${pct}%"></span></div>
          <p class="dust small">${Math.round(me.minutes_used)} minutes used · resets on the 1st</p>
          <a class="btn" href="/pricing" style="justify-self:start">Compare plans</a>
        </section>
        <section class="stat-card">
          <h2>What your plan includes</h2>
          <ul class="ticks">${items.map(([on, t]) => `<li class="${on ? "" : "off"}">${esc(t)}</li>`).join("")}</ul>
        </section>
      </div>
      <p style="margin-top:22px"><button class="btn quiet" type="button" data-action="sign-out">Sign out of this tab</button></p>`;
  }

  // ---------- sign in and start ----------

  async function startEmailSignIn(e) {
    e.preventDefault();
    const email = $("#email").value.trim();
    $("#signInError").textContent = "";
    const btn = e.target.querySelector("button");
    btn.disabled = true;
    try {
      const out = await api("/v1/auth/start", { method: "POST", body: { email } });
      $("#emailSentText").textContent = out.message;
      $("#emailForm").hidden = true;
      $("#emailSent").hidden = false;
    } catch (err) {
      if (err.message !== "unauthorized") $("#signInError").textContent = err.message;
    } finally {
      btn.disabled = false;
    }
  }

  // The sign-in link carries a one-time token in the fragment: exchange it, then remove it from the address bar.
  async function redeemFromLink() {
    const m = location.hash.match(/^#login=(.+)$/);
    if (!m) return false;
    const token = decodeURIComponent(m[1]);
    history.replaceState(null, "", location.pathname + "#/clips");
    try {
      const out = await fetch("/v1/auth/redeem", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token }),
      });
      const body = await out.json().catch(() => ({}));
      if (!out.ok) throw new Error(body.detail || "That sign-in link has expired or was already used. Request a new one.");
      state.key = body.api_key;
      store.set(body.api_key);
      return true;
    } catch (err) {
      $("#signInError").textContent = err.message;
      return false;
    }
  }

  async function signIn(e) {
    e.preventDefault();
    const key = $("#apiKey").value.trim();
    $("#signInError").textContent = "";
    state.key = key;
    try {
      await refreshMe();
      store.set(key);
      $("#apiKey").value = "";
      showScreen("app");
      await route();
    } catch (err) {
      state.key = null;
      if (err.message !== "unauthorized") $("#signInError").textContent = err.message;
    }
  }

  function showBillingResult() {
    try {
      const result = new URLSearchParams(location.search).get("billing");
      if (!result) return;
      toast(result === "success"
        ? "Thanks. Your plan updates as soon as Stripe confirms the payment."
        : "Checkout was cancelled. No charge was made.", result === "success" ? "good" : "");
      history.replaceState(null, "", location.pathname + location.hash);
    } catch { /* URL parsing unavailable: nothing to show */ }
  }

  function wire() {
    $("#signInForm").addEventListener("submit", signIn);
    $("#emailForm").addEventListener("submit", startEmailSignIn);
    $("#emailAgain").addEventListener("click", () => {
      $("#emailSent").hidden = true;
      $("#emailForm").hidden = false;
      $("#email").focus();
    });
    $("#signOut").addEventListener("click", () => signOut(""));
    window.addEventListener("hashchange", () => route());
    window.addEventListener("resize", syncSheet);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden && state.key && !state.dirty) route();
    });
    document.addEventListener("submit", (e) => {
      if (e.target.id === "scheduleForm") submitSchedule(e);
    });
    document.addEventListener("click", (e) => {
      const t = e.target.closest("[data-action]");
      if (!t || t.disabled) return;
      switch (t.dataset.action) {
        case "select": selectClip(Number(t.dataset.n)); break;
        case "nudge": nudge(t.dataset.edge, Number(t.dataset.d)); break;
        case "style": setStyle(t.dataset.style); break;
        case "aspect": setAspect(t.dataset.aspect); break;
        case "edit-word": editWord(Number(t.dataset.i)); break;
        case "save": saveRender(); break;
        case "download": downloadClip(); break;
        case "zip": downloadZip(); break;
        case "copy": copyText(t.dataset.text); break;
        case "toggle-upload": toggleUpload(); break;
        case "toggle-schedule": toggleSchedule(); break;
        case "delete-schedule": deleteSchedule(t.dataset.id); break;
        case "back":
          if (state.sheet && window.innerWidth < 1000) { state.sheet = false; syncSheet(); }
          else location.hash = "#/clips";
          break;
        case "sign-out": signOut(""); break;
        default: break;
      }
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && state.sheet && window.innerWidth < 1000) {
        state.sheet = false;
        syncSheet();
      }
    });
  }

  async function start() {
    wire();
    showBillingResult();
    if (location.hash.startsWith("#login=")) {
      showScreen("signin");
      if (await redeemFromLink()) {
        showScreen("app");
        await route();
        return;
      }
      return;
    }
    const saved = store.get();
    if (saved) {
      state.key = saved;
      try {
        await refreshMe();
        showScreen("app");
        await route();
        return;
      } catch {
        state.key = null;
        store.clear();
      }
    }
    showScreen("signin");
  }

  start();
})();
