// ClipForge web app. Plain JavaScript, no build step.
// The API key lives in sessionStorage for this tab only. Clip files are fetched with the
// key and shown through object URLs, because <video> tags can't send custom headers.

(() => {
  "use strict";

  const KEY = "cf_api_key";
  const POLL_MS = 4000;
  const ALLOWED = [".mp4", ".mov", ".mkv", ".webm"];
  const STEPS = ["Uploaded", "Processing", "Clips ready"];

  const state = {
    key: null,
    me: null,
    plans: null,
    jobs: [],
    clipUrls: new Map(), // "jobId/n" -> object URL
    pollTimer: null,
    file: null,
    uploading: false,
  };

  const $ = (sel, root = document) => root.querySelector(sel);
  const el = (tag, props = {}, children = []) => {
    const node = document.createElement(tag);
    Object.entries(props).forEach(([k, v]) => {
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v);
    });
    children.forEach((c) => node.append(c));
    return node;
  };

  // ---------- storage (best effort) ----------

  const store = {
    get() { try { return sessionStorage.getItem(KEY); } catch { return null; } },
    set(v) { try { sessionStorage.setItem(KEY, v); } catch { /* private mode: keep in memory only */ } },
    clear() { try { sessionStorage.removeItem(KEY); } catch { /* ignore */ } },
  };

  // ---------- API ----------

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      ...opts,
      headers: { ...(opts.headers || {}), "X-API-Key": state.key || "" },
    });
    if (res.status === 401) {
      signOut("Your API key wasn't accepted. Check it and sign in again.");
      throw new Error("unauthorized");
    }
    const isJson = (res.headers.get("content-type") || "").includes("application/json");
    const body = isJson ? await res.json() : null;
    if (!res.ok) throw new Error((body && body.detail) || `Request failed (${res.status})`);
    return body;
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

  async function clipUrl(jobId, n) {
    const key = `${jobId}/${n}`;
    if (state.clipUrls.has(key)) return state.clipUrls.get(key);
    const res = await fetch(`/v1/jobs/${jobId}/clips/${n}.mp4`, { headers: { "X-API-Key": state.key || "" } });
    if (!res.ok) throw new Error(`clip ${n} unavailable`);
    const url = URL.createObjectURL(await res.blob());
    state.clipUrls.set(key, url);
    return url;
  }

  // ---------- toasts ----------

  function toast(message, tone = "") {
    const node = el("div", { class: `toast ${tone}`, text: message });
    $("#toasts").append(node);
    setTimeout(() => node.remove(), 5200);
  }

  // ---------- formatting ----------

  const fmtDate = (iso) => {
    if (!iso) return "";
    const d = new Date(iso);
    return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  };
  const fmtMinutes = (m) => (m >= 100 ? Math.round(m) : Math.round(m * 10) / 10).toString();
  const shortId = (id) => id.slice(0, 8);

  // ---------- rendering: header and usage ----------

  function renderUsage() {
    const me = state.me;
    if (!me) return;
    $("#planName").textContent = me.plan_name;
    const pct = Math.min(100, (me.minutes_used / me.minutes_limit) * 100);
    const fill = $("#meterFill");
    fill.style.width = `${pct}%`;
    fill.className = pct >= 100 ? "full" : pct >= 80 ? "near" : "";
    $("#meterText").textContent = `${fmtMinutes(me.minutes_used)} / ${me.minutes_limit} min`;
    $("#watermarkBanner").hidden = !me.watermark;
    $("#limitsText").textContent = `up to ${me.max_clips} clips · ${me.max_upload_mb >= 1024 ? (me.max_upload_mb / 1024).toFixed(0) + " GB" : me.max_upload_mb + " MB"} per file`;
    const slider = $("#clipCount");
    slider.max = String(me.max_clips);
    if (Number(slider.value) > me.max_clips) slider.value = String(me.max_clips);
    $("#clipCountLabel").textContent = slider.value;
  }

  function renderPlans() {
    if (!state.plans) return;
    const grid = $("#planGrid");
    grid.replaceChildren();
    Object.entries(state.plans).forEach(([id, p]) => {
      const current = state.me && state.me.plan === id;
      const features = [
        `${p.source_minutes_month} source minutes a month`,
        `Up to ${p.max_clips} clips per upload`,
        p.watermark ? "Logo watermark on clips" : "No watermark",
        `${p.retention_days}-day clip storage`,
        p.publishing ? "Publish to YouTube and TikTok" : "Download clips",
        p.brand_kit ? "Brand kit" : null,
        p.seats > 1 ? `${p.seats} seats` : null,
      ].filter(Boolean);
      grid.append(el("article", { class: `plan ${current ? "current" : ""}` }, [
        current ? el("span", { class: "tag", text: "Your plan" }) : el("span", { class: "tag", text: "" }),
        el("h3", { text: p.name }),
        el("div", { class: "price" }, [
          document.createTextNode(p.price_usd === 0 ? "Free" : `$${p.price_usd}`),
          p.price_usd ? el("small", { text: " / mo" }) : document.createTextNode(""),
        ]),
        el("ul", {}, features.map((f) => el("li", { text: f }))),
        current
          ? el("button", { class: "ghost", type: "button", disabled: "", text: "Current plan" })
          : el("button", { class: "ghost", type: "button", disabled: "", title: "Billing isn't live yet", text: "Coming soon" }),
      ]));
    });
  }

  // ---------- rendering: jobs ----------

  function stepIndex(status) {
    if (status === "queued") return 0;
    if (status === "running") return 1;
    return 2; // done or failed
  }

  function renderJobs() {
    const list = $("#jobs");
    const empty = $("#jobsEmpty");
    $("#jobsCount").textContent = state.jobs.length ? `${state.jobs.length} total` : "";
    empty.hidden = state.jobs.length > 0;
    list.replaceChildren(...state.jobs.map(jobCard));
    state.jobs.forEach((job) => {
      if (job.status === "done") job.clips.forEach((n) => loadClip(job.id, n));
    });
  }

  function jobCard(job) {
    const idx = stepIndex(job.status);
    const steps = el("div", { class: "steps", "aria-label": `Progress: ${job.status}` },
      STEPS.map((label, i) => {
        const cls = job.status === "failed" && i === 2 ? "step on" : i < idx || (job.status === "done" && i === 2) ? "step done" : i === idx ? "step on" : "step";
        return el("div", { class: cls, text: label });
      }));
    const meta = `${fmtMinutes(job.source_minutes)} min source · started ${fmtDate(job.created_at)}`;
    const children = [
      el("div", { class: "job-head" }, [
        el("div", {}, [
          el("h3", { text: `Job ${shortId(job.id)}` }),
          el("div", { class: "job-meta", text: meta }),
        ]),
        el("span", { class: `pill ${job.status}`, text: job.status.charAt(0).toUpperCase() + job.status.slice(1) }),
      ]),
      steps,
    ];
    if (job.status === "failed") {
      children.push(el("div", { class: "error-box", role: "alert", text: job.error || "Something went wrong while processing this video." }));
    }
    if (job.status === "done") {
      if (job.clip_count === 0) {
        children.push(el("p", { class: "hint", style: "margin-top:12px", text: "No strong moments were found in this video." }));
      } else {
        children.push(el("div", { class: "clips" }, job.clips.map((n) => clipTile(job.id, n))));
      }
    }
    return el("article", { class: "job", "data-id": job.id }, children);
  }

  function clipTile(jobId, n) {
    const frame = el("div", { class: "clip-frame" }, [el("div", { class: "loading", text: "Loading…" })]);
    const link = el("a", { class: "download", href: "#", text: "Download", download: `clip_${shortId(jobId)}_${n}.mp4` });
    link.setAttribute("aria-disabled", "true");
    link.addEventListener("click", (e) => { if (link.getAttribute("aria-disabled") === "true") e.preventDefault(); });
    return el("figure", { class: "clip", "data-clip": `${jobId}/${n}` }, [
      frame,
      el("figcaption", {}, [el("span", { class: "muted", text: `Clip ${n}` }), link]),
    ]);
  }

  async function loadClip(jobId, n) {
    const figure = document.querySelector(`figure[data-clip="${jobId}/${n}"]`);
    if (!figure || figure.dataset.loaded === "1" || figure.dataset.loading === "1") return;
    figure.dataset.loading = "1";
    try {
      const url = await clipUrl(jobId, n);
      const frame = figure.querySelector(".clip-frame");
      const video = el("video", { src: url, muted: "", loop: "", playsinline: "", controls: "", preload: "metadata" });
      frame.replaceChildren(video);
      const link = figure.querySelector("a.download");
      link.href = url;
      link.setAttribute("aria-disabled", "false");
      figure.dataset.loaded = "1";
    } catch {
      const frame = figure.querySelector(".clip-frame");
      frame.replaceChildren(el("div", { class: "loading", text: "Couldn't load this clip" }));
    } finally {
      figure.dataset.loading = "0";
    }
  }

  // ---------- data loading and polling ----------

  async function refresh() {
    const [me, jobs] = await Promise.all([api("/v1/me"), api("/v1/jobs")]);
    state.me = me;
    state.jobs = jobs;
    renderUsage();
    renderJobs();
    renderPlans();
    schedulePoll();
  }

  function schedulePoll() {
    clearTimeout(state.pollTimer);
    const busy = state.jobs.some((j) => j.status === "queued" || j.status === "running");
    if (busy && state.key) state.pollTimer = setTimeout(() => refresh().catch(() => {}), POLL_MS);
  }

  // ---------- upload form ----------

  function setFile(file) {
    const error = $("#fileError");
    error.textContent = "";
    if (!file) { state.file = null; updateSubmit(); return; }
    const ext = (file.name.match(/\.[^.]+$/) || [""])[0].toLowerCase();
    if (!ALLOWED.includes(ext)) {
      error.textContent = `Unsupported file type. Use ${ALLOWED.join(", ")}.`;
      state.file = null;
    } else if (state.me && file.size > state.me.max_upload_mb * 1024 * 1024) {
      error.textContent = `This file is larger than your plan's ${state.me.max_upload_mb >= 1024 ? (state.me.max_upload_mb / 1024).toFixed(0) + " GB" : state.me.max_upload_mb + " MB"} limit.`;
      state.file = null;
    } else {
      state.file = file;
    }
    const zone = $("#dropzone");
    zone.classList.toggle("has-file", !!state.file);
    $("#dropTitle").textContent = state.file ? state.file.name : "Drop a video here, or browse";
    $("#dropSub").textContent = state.file
      ? `${(state.file.size / (1024 * 1024)).toFixed(1)} MB`
      : `${ALLOWED.map((e) => e.slice(1).toUpperCase()).join(", ")}`;
    updateSubmit();
  }

  function updateSubmit() {
    $("#submitBtn").disabled = !(state.file && $("#rights").checked) || state.uploading;
  }

  function setUploading(on, progress = 0) {
    state.uploading = on;
    $("#uploadProgress").hidden = !on;
    $("#uploadBar").style.width = `${Math.round(progress * 100)}%`;
    $("#uploadText").textContent = progress >= 1 ? "Upload complete. Queuing…" : `Uploading… ${Math.round(progress * 100)}%`;
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
      toast(`Job ${shortId(job.id)} is queued.`, "good");
      state.file = null;
      $("#fileInput").value = "";
      $("#rights").checked = false;
      setFile(null);
      await refresh();
    } catch (err) {
      if (err.message !== "unauthorized") toast(err.message, "bad");
    } finally {
      setUploading(false);
    }
  }

  function wireUpload() {
    const zone = $("#dropzone");
    const input = $("#fileInput");
    zone.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); } });
    input.addEventListener("change", () => setFile(input.files[0]));
    ["dragenter", "dragover"].forEach((t) => zone.addEventListener(t, (e) => { e.preventDefault(); zone.classList.add("drag"); }));
    ["dragleave", "drop"].forEach((t) => zone.addEventListener(t, (e) => { e.preventDefault(); zone.classList.remove("drag"); }));
    zone.addEventListener("drop", (e) => setFile(e.dataTransfer.files[0]));
    $("#rights").addEventListener("change", updateSubmit);
    $("#clipCount").addEventListener("input", (e) => { $("#clipCountLabel").textContent = e.target.value; });
    $("#uploadForm").addEventListener("submit", submitUpload);
  }

  // ---------- sign in and out ----------

  function showScreen(name) {
    $("#signIn").hidden = name !== "signin";
    $("#app").hidden = name !== "app";
    $("#topRight").hidden = name !== "app";
  }

  function signOut(message) {
    state.key = null;
    store.clear();
    clearTimeout(state.pollTimer);
    state.clipUrls.forEach((u) => URL.revokeObjectURL(u));
    state.clipUrls.clear();
    showScreen("signin");
    if (message) $("#signInError").textContent = message;
  }

  async function signIn(e) {
    e.preventDefault();
    const key = $("#apiKey").value.trim();
    $("#signInError").textContent = "";
    state.key = key;
    try {
      await refresh();
      store.set(key);
      showScreen("app");
      $("#apiKey").value = "";
    } catch (err) {
      state.key = null;
      if (err.message !== "unauthorized") $("#signInError").textContent = err.message;
    }
  }

  async function loadPlans() {
    try {
      const res = await fetch("/v1/plans");
      if (res.ok) state.plans = await res.json();
    } catch { /* plans are optional on the page */ }
  }

  function wireAccount() {
    $("#signInForm").addEventListener("submit", signIn);
    $("#signOut").addEventListener("click", () => signOut(""));
    $("#planChip").addEventListener("click", () => $("#plans").scrollIntoView({ behavior: "smooth" }));
  }

  // ---------- start ----------

  async function start() {
    wireAccount();
    wireUpload();
    await loadPlans();
    renderPlans();
    const saved = store.get();
    if (saved) {
      state.key = saved;
      try {
        await refresh();
        showScreen("app");
        return;
      } catch {
        state.key = null;
        store.clear();
      }
    }
    showScreen("signin");
  }

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && state.key) refresh().catch(() => {});
  });

  start();
})();
