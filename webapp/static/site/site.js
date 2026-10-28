/* ClipForge marketing pages: plan cards on /pricing and the payment step on /checkout.
   Plan data mirrors PLANS in webapp/service.py. The server stays the source of truth
   for limits; this file only describes the plans to visitors. */
(() => {
  "use strict";

  const KEY = "cf_api_key";
  const YEARLY_SAVE = 0.2;

  const PLANS = [
    {
      id: "free", name: "Free", monthly: 0, tagline: "Try the workflow on your own footage.",
      features: [
        { t: "60 source minutes a month", on: true },
        { t: "Uploads up to 2 GB", on: true },
        { t: "Up to 5 clips per upload", on: true },
        { t: "ClipForge logo on every clip", on: true },
        { t: "Clips kept for 7 days", on: true },
        { t: "Vertical 9:16 only", on: true },
        { t: "Square and landscape exports", on: false },
        { t: "Post to YouTube and TikTok", on: false },
      ],
    },
    {
      id: "creator", name: "Creator", monthly: 15, tagline: "For regular streamers posting every week.",
      features: [
        { t: "300 source minutes a month", on: true },
        { t: "Uploads up to 4 GB", on: true },
        { t: "Up to 15 clips per upload", on: true },
        { t: "No logo on clips", on: true },
        { t: "Clips kept for 30 days", on: true },
        { t: "Square and landscape exports", on: true },
        { t: "Post to YouTube and TikTok", on: false },
      ],
    },
    {
      id: "pro", name: "Pro", monthly: 29, featured: true, badge: "Most popular",
      tagline: "For channels that publish a lot and want it automated.",
      features: [
        { t: "600 source minutes a month", on: true },
        { t: "Uploads up to 8 GB", on: true },
        { t: "Up to 20 clips per upload", on: true },
        { t: "No logo on clips", on: true },
        { t: "Clips kept for 90 days", on: true },
        { t: "Square and landscape exports", on: true },
        { t: "Post to YouTube and TikTok, once platform approval is in", on: true },
        { t: "Brand kit", on: true },
      ],
    },
    {
      id: "business", name: "Business", monthly: 79, tagline: "For teams running several channels.",
      features: [
        { t: "1,500 source minutes a month", on: true },
        { t: "Uploads up to 8 GB", on: true },
        { t: "Up to 30 clips per upload", on: true },
        { t: "No logo on clips", on: true },
        { t: "Clips kept for 90 days", on: true },
        { t: "Square and landscape exports", on: true },
        { t: "Post to YouTube and TikTok, once platform approval is in", on: true },
        { t: "Brand kit", on: true },
      ],
    },
  ];

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const money = (n) => "$" + n;

  const monthlyFor = (p, interval) => (interval === "yearly" ? Math.round(p.monthly * (1 - YEARLY_SAVE)) : p.monthly);
  const yearlyTotal = (p) => Math.round(p.monthly * (1 - YEARLY_SAVE)) * 12;

  function savedKey() {
    try { return sessionStorage.getItem(KEY) || ""; } catch (e) { return ""; }
  }

  async function billingEnabled() {
    try {
      const r = await fetch("/v1/billing/status", { headers: { Accept: "application/json" } });
      if (!r.ok) return false;
      const data = await r.json();
      return data.enabled === true;
    } catch (e) {
      return false;
    }
  }

  function planCard(p, interval) {
    const isFree = p.id === "free";
    const price = monthlyFor(p, interval);
    const sub = isFree
      ? "Free forever"
      : interval === "yearly" ? `billed ${money(yearlyTotal(p))} a year` : "billed monthly";
    const href = isFree ? "/app" : `/checkout?plan=${p.id}&interval=${interval}`;
    const cta = isFree ? "Start free" : `Choose ${p.name}`;
    const list = p.features.map((f) => `<li class="${f.on ? "" : "off"}">${esc(f.t)}</li>`).join("");
    return `
      <article class="plan${p.featured ? " featured" : ""}" aria-labelledby="plan-${p.id}">
        ${p.badge ? `<span class="badge">${esc(p.badge)}</span>` : ""}
        <h3 id="plan-${p.id}">${esc(p.name)}</h3>
        <p class="tagline">${esc(p.tagline)}</p>
        <div class="price">${money(price)}<small>${isFree ? "" : " / month"}</small></div>
        <p class="muted small">${esc(sub)}</p>
        <ul>${list}</ul>
        <a class="btn btn-block ${p.featured ? "btn-primary" : ""}" href="${href}">${esc(cta)}</a>
      </article>`;
  }

  function initPricing() {
    const grid = $("#planCards");
    if (!grid) return;
    let interval = "monthly";
    const render = () => {
      grid.innerHTML = PLANS.map((p) => planCard(p, interval)).join("");
      $$("[data-interval]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.interval === interval)));
    };
    $$("[data-interval]").forEach((b) => {
      b.addEventListener("click", () => { interval = b.dataset.interval; render(); });
    });
    render();
  }

  async function initCheckout() {
    const root = $("#planPanel");
    if (!root) return;
    const params = new URLSearchParams(location.search);
    const plan = PLANS.find((p) => p.id === params.get("plan"));
    const interval = params.get("interval") === "yearly" ? "yearly" : "monthly";
    const summary = $("#summary");
    const pay = $("#payBtn");
    const err = $("#checkoutError");
    const notice = $("#noticeText");

    if (!plan || plan.id === "free") {
      root.innerHTML = `
        <h2>Pick a paid plan</h2>
        <p class="muted" style="margin-top:10px;">The free plan doesn't need payment. <a href="/pricing">Compare the paid plans</a> when you're ready.</p>`;
      summary.innerHTML = "";
      pay.hidden = true;
      return;
    }

    const price = monthlyFor(plan, interval);
    const total = interval === "yearly" ? yearlyTotal(plan) : price;
    root.innerHTML = `
      <h2>${esc(plan.name)} plan</h2>
      <p class="muted" style="margin-top:8px;">${esc(plan.tagline)}</p>
      <ul>${plan.features.filter((f) => f.on).map((f) => `<li>${esc(f.t)}</li>`).join("")}</ul>
      <p class="small muted" style="margin-top:18px;"><a href="/pricing">Change plan</a></p>`;

    summary.innerHTML = `
      <div class="summary-row"><span>Plan</span><strong>${esc(plan.name)}</strong></div>
      <div class="summary-row"><span>Billing</span><span>${interval === "yearly" ? "Yearly" : "Monthly"}</span></div>
      <div class="summary-row"><span>Price</span><span>${money(price)} / month</span></div>
      <div class="summary-row"><span>Due today</span><span class="total">${money(total)}</span></div>`;

    const enabled = await billingEnabled();
    const key = savedKey();

    if (!enabled) {
      pay.disabled = true;
      pay.textContent = "Payments open soon";
      notice.hidden = false;
      notice.textContent = "Payments aren't open yet. Your plan will be ready to pay for as soon as they are. Until then, your ClipForge team sets up your account.";
      return;
    }

    if (!key) {
      pay.disabled = false;
      pay.textContent = "Sign in to continue";
      pay.addEventListener("click", () => { location.href = "/app"; });
      notice.hidden = false;
      notice.textContent = "Sign in to your account first. We'll bring you back here to pay.";
      return;
    }

    pay.disabled = false;
    pay.textContent = `Continue to secure payment · ${money(total)}`;
    pay.addEventListener("click", async () => {
      const label = pay.textContent;
      pay.disabled = true;
      pay.textContent = "Opening secure checkout…";
      err.textContent = "";
      try {
        const r = await fetch("/v1/billing/checkout", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-API-Key": key },
          body: JSON.stringify({ plan: plan.id, interval }),
        });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Checkout couldn't start. Please try again.");
        location.href = data.url;
      } catch (e) {
        err.textContent = e.message;
        pay.disabled = false;
        pay.textContent = label;
      }
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    $$("[data-year]").forEach((el) => { el.textContent = String(new Date().getFullYear()); });
    initPricing();
    initCheckout();
  });
})();
