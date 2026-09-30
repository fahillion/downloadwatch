// DownloadWatch for Plex dashboard. All text goes in via textContent (titles and names come from Plex).
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const PAGE = 50;
  // Absolute URLs without any user:pass@ part (fetch rejects URLs that carry credentials).
  const API = location.origin + location.pathname.replace(/[^/]*$/, "") + "api/";
  let TZ = undefined, offset = 0, total = 0;
  const form = $("filters");

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }
  function fmt(iso, withDate = true) {
    if (!iso) return "";
    const d = new Date(iso);
    const opts = withDate ? { month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZone: TZ }
                          : { hour: "numeric", minute: "2-digit", timeZone: TZ };
    return d.toLocaleString(undefined, opts);
  }
  function sameDay(a, b) {
    const f = (x) => new Date(x).toLocaleDateString("en-CA", { timeZone: TZ });
    return a && b && f(a) === f(b);
  }
  function localDate(daysAgo) {
    const d = new Date(Date.now() - daysAgo * 864e5);
    return d.toLocaleDateString("en-CA", { timeZone: TZ });  // YYYY-MM-DD
  }
  function mins(a, b) {
    const m = Math.round((new Date(b) - new Date(a)) / 60000);
    return m < 1 ? "<1 min" : m < 60 ? m + " min" : Math.floor(m / 60) + " h " + (m % 60) + " min";
  }

  function params() {
    const f = new FormData(form), p = new URLSearchParams();
    for (const k of ["q", "user", "type", "status"]) if (f.get(k)) p.set(k, f.get(k));
    const r = f.get("range");
    if (r === "today") p.set("from", localDate(0));
    else if (r === "7") p.set("from", localDate(6));
    else if (r === "30") p.set("from", localDate(29));
    else if (r === "month") p.set("from", localDate(0).slice(0, 8) + "01");
    else if (r === "custom") { if (f.get("from")) p.set("from", f.get("from")); if (f.get("to")) p.set("to", f.get("to")); }
    return p;
  }

  function fillSelect(sel, values) {
    const cur = sel.value;
    while (sel.options.length > 1) sel.remove(1);
    for (const v of values) sel.add(new Option(v, v));
    sel.value = values.includes(cur) ? cur : "";
  }

  async function loadSummary() {
    let s;
    try {
      const r = await fetch(API + "summary", { cache: "no-store" });
      s = await r.json();
    } catch (e) {
      $("conn").textContent = "DownloadWatch unreachable"; $("conn").className = "badge bad"; return;
    }
    TZ = s.timezone || TZ;
    $("cToday").textContent = s.today; $("c7").textContent = s.d7; $("c30").textContent = s.d30;
    $("cActive").textContent = s.active.length; $("cGb").textContent = s.gb30 + " GB";
    $("cTop").textContent = s.top30 ? `${s.top30.user} (${s.top30.n})` : "–";
    const h = s.health, c = $("conn");
    $("relink").hidden = !(CFG && CFG.can_relink && (h.auth_error || !h.plex_connected));
    if (s.first) $("since").textContent = `Every download from your Plex server since ${fmt(s.first)}.`;
    if (CFG && CFG.demo) { c.textContent = "● demo"; c.className = "badge good"; }
    else if (h.auth_error) { c.textContent = "Plex rejected the token"; c.className = "badge bad"; }
    else if (h.plex_connected) { c.textContent = h.feed_connected ? "● live" : "● connected (polling)"; c.className = "badge good"; }
    else { c.textContent = "Plex unreachable — retrying"; c.className = "badge bad"; }
    c.title = h.last_poll ? "Last check " + fmt(h.last_poll) : "";

    const act = $("active");
    act.replaceChildren();
    $("activeWrap").hidden = !s.active.length;
    for (const a of s.active) {
      const card = el("div", "act");
      card.append(el("div", "who", a.plex_username || "unknown user"), el("div", "what", a.display_title));
      const bar = el("div", "bar"), fill = el("span");
      fill.style.width = Math.max(0, Math.min(100, a.last_progress || 0)) + "%";
      bar.append(fill);
      const meta = el("div", "meta");
      meta.append(el("span", "", (a.last_progress == null ? "starting" : Math.round(a.last_progress) + "%")),
                  el("span", "", "Started " + fmt(a.started_at, false) + (a.size_gb ? " · " + a.size_gb + " GB" : "")));
      card.append(bar, meta);
      card.addEventListener("click", () => showDetail(a.id));
      act.append(card);
    }

    fillSelect(form.user, s.filters.users);
    fillSelect(form.type, s.filters.types);
    const ub = $("users");
    ub.replaceChildren();
    if (!s.users.length) { const tr = el("tr"); const td = el("td", "muted", "No downloads yet"); td.colSpan = 4; tr.append(td); ub.append(tr); }
    for (const u of s.users) {
      const tr = el("tr", "click");
      tr.append(el("td", "", u.user || "unknown"), el("td", "num", u.n), el("td", "num", u.gb), el("td", "sub", fmt(u.last)));
      tr.addEventListener("click", () => { form.user.value = u.user || ""; offset = 0; loadRows(); });
      ub.append(tr);
    }
  }

  async function loadRows() {
    const p = params();
    p.set("limit", PAGE); p.set("offset", offset);
    let d;
    try { d = await (await fetch(API + "downloads?" + p, { cache: "no-store" })).json(); } catch (e) { return; }
    total = d.total;
    const tb = $("rows");
    tb.replaceChildren();
    if (!d.rows.length) { const tr = el("tr"); const td = el("td", "muted", "No downloads match."); td.colSpan = 7; tr.append(td); tb.append(tr); }
    for (const r of d.rows) {
      const tr = el("tr", "click");
      const title = el("td");
      title.append(el("div", "", r.display_title));
      if (r.library) title.append(el("div", "sub", r.library + (r.resolution ? " · " + r.resolution + (/^\d+$/.test(r.resolution) ? "p" : "") : "")));
      const st = el("td"); st.append(el("span", "st " + r.status, r.status === "Active" && r.last_progress != null ? Math.round(r.last_progress) + "%" : r.status));
      const fin = r.completed_at || r.ended_at;
      tr.append(el("td", "", fmt(r.started_at)), el("td", "", r.plex_username || "?"), title,
                el("td", "", r.media_type || ""), st,
                el("td", "sub", fin ? (sameDay(fin, r.started_at) ? fmt(fin, false) : fmt(fin)) + " · " + mins(r.started_at, fin) : ""),
                el("td", "num", r.size_gb != null ? r.size_gb + " GB" : ""));
      tr.addEventListener("click", () => showDetail(r.id));
      tb.append(tr);
    }
    $("count").textContent = total ? `${offset + 1}–${offset + d.rows.length} of ${total}` : "";
    $("prev").disabled = offset === 0;
    $("next").disabled = offset + PAGE >= total;
  }

  async function showDetail(id) {
    let r;
    try { r = await (await fetch(API + "downloads/" + id, { cache: "no-store" })).json(); } catch (e) { return; }
    $("dTitle").textContent = r.display_title;
    const dl = $("dList");
    dl.replaceChildren();
    const fin = r.completed_at || r.ended_at;
    const items = [
      ["User", r.plex_username], ["Status", r.status + (r.last_progress != null ? ` (${Math.round(r.last_progress)}%)` : "")],
      ["Type", r.media_type], ["Show", r.grandparent_title],
      ["Season / episode", r.season != null ? `S${String(r.season).padStart(2, "0")}E${String(r.episode).padStart(2, "0")}` : null],
      ["Year", r.year], ["Library", r.library], ["Started", fmt(r.started_at)], ["Last seen", fmt(r.last_seen_at)],
      ["Finished", fin ? fmt(fin) + " (" + mins(r.started_at, fin) + ")" : null],
      ["Source file", r.size_gb != null ? `${r.size_gb} GB · ${r.resolution || "?"} · ${r.container || "?"}` : null],
      ["Runtime", r.duration_min != null ? r.duration_min + " min" : null],
      ["Plex metadata ID", r.metadata_id], ["Plex user ID", r.plex_user_id], ["Activity UUID", r.activity_uuid],
    ];
    for (const [k, v] of items) {
      if (v === null || v === undefined || v === "") continue;
      dl.append(el("dt", "", k), el("dd", "", String(v)));
    }
    let raw = r.raw_activity || "";
    try { raw = JSON.stringify(JSON.parse(raw), null, 2); } catch (e) { /* keep as is */ }
    $("dRaw").textContent = raw;
    $("detail").hidden = false;
    $("dClose").focus();
  }

  $("dClose").addEventListener("click", () => { $("detail").hidden = true; });
  $("detail").addEventListener("click", (e) => { if (e.target.id === "detail") $("detail").hidden = true; });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("detail").hidden = true; });
  let t;
  form.addEventListener("input", (e) => {
    form.querySelector(".custom").hidden = form.range.value !== "custom";
    clearTimeout(t); offset = 0;
    t = setTimeout(loadRows, e.target.name === "q" ? 300 : 0);
  });
  form.addEventListener("submit", (e) => e.preventDefault());
  $("prev").addEventListener("click", () => { offset = Math.max(0, offset - PAGE); loadRows(); });
  $("next").addEventListener("click", () => { offset += PAGE; loadRows(); });
  $("export").addEventListener("click", () => { location.href = API + "export.csv?" + params(); });

  // ---------------- config, banners, navigation, first-run sign-in
  let CFG = null, timer = null;
  function banner(text, cls) { const b = el("div", "banner " + cls, text); $("banners").append(b); }

  async function post(path) {
    const r = await fetch(API + path, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}", cache: "no-store" });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
    return d;
  }

  function showSetup() {
    $("dashView").hidden = true; $("setupView").hidden = false;
    const missing = !CFG.plex_url_set;
    $("setupMissingUrl").hidden = !missing; $("setupStart").hidden = missing || !CFG.can_relink;
    if (!missing && !CFG.can_relink) { $("setupMsg").textContent = "Sign-in isn't available here (see the log)."; $("setupMsg").className = "err"; }
  }

  let pollSetup = null, expiresAt = 0;
  async function startSetup() {
    $("getCode").disabled = true; $("setupMsg").textContent = ""; $("setupMsg").className = "";
    try {
      const d = await post("setup/start");
      $("code").textContent = d.code; expiresAt = Date.now() + d.expires_in * 1000;
      $("setupStart").hidden = true; $("setupCode").hidden = false;
      clearInterval(pollSetup); pollSetup = setInterval(checkSetup, 2500);
    } catch (e) { $("setupMsg").textContent = e.message; $("setupMsg").className = "err"; $("getCode").disabled = false; }
  }
  async function checkSetup() {
    const left = Math.max(0, Math.round((expiresAt - Date.now()) / 1000));
    $("expires").textContent = `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
    let d;
    try { d = await (await fetch(API + "setup/status", { cache: "no-store" })).json(); } catch (e) { return; }
    if (d.state === "linked") {
      clearInterval(pollSetup);
      $("setupCode").hidden = true;
      $("setupMsg").textContent = "Connected" + (d.server ? ` to ${d.server}` : "") + ". Loading your dashboard…"; $("setupMsg").className = "ok";
      setTimeout(() => location.reload(), 1500);
    } else if (d.state === "error" || d.state === "expired" || d.state === "idle") {
      clearInterval(pollSetup);
      $("setupCode").hidden = true; $("setupStart").hidden = false; $("getCode").disabled = false;
      $("setupMsg").textContent = d.state === "error" ? d.error : "The code expired. Get a new one."; $("setupMsg").className = "err";
    }
  }
  $("getCode").addEventListener("click", startSetup);
  $("relink").addEventListener("click", () => { showSetup(); $("setupStart").hidden = false; });

  function tick() { loadSummary(); if (offset === 0) loadRows(); }

  async function boot() {
    try { CFG = await (await fetch(API + "config", { cache: "no-store" })).json(); }
    catch (e) { banner("Can't reach DownloadWatch.", "warn"); return; }
    document.title = CFG.title === "DownloadWatch" ? "DownloadWatch for Plex" : CFG.title;
    $("siteTitle").textContent = CFG.title;
    $("version").textContent = "v" + CFG.version;
    for (const n of CFG.nav) { const a = el("a", "", n.label); a.href = n.url; $("nav").append(a); }
    if (CFG.demo) banner("Demo mode: made-up sample data (public-domain films and fictional shows). No Plex server is connected.", "info");
    if (CFG.auth === "none") banner("No dashboard password is set: anyone who can reach this page can see who downloaded what. Set AUTH_PASSWORD.", "warn");
    if (!CFG.configured) return showSetup();
    $("dashView").hidden = false;
    tick();
    timer = setInterval(tick, 10000);
  }
  boot();
})();
