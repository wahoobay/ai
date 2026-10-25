// services/dashboard/app/static/review-core.js
// Shared plumbing for the review page (spec 2026-09-24 §9): API calls, the
// species catalogue and its search box, the drag-to-fix box editor,
// keyboard shortcuts and view switching. The views live in
// review-single.js, review-frame.js and review-browse.js and register
// themselves on RV.views. Nothing here writes data straight into markup
// (no dynamic HTML-string assignment): every label comes from the
// database or the Species Log and is set as text.
"use strict";

const RV = {
  role: document.body.dataset.role || "reviewer",
  view: null,
  lane: "check",
  catalogue: [],
  byKey: new Map(),
  views: {},
  keyHandlers: {},
  held: null,          // the single box being answered: {itemId, savedFrameId}
  openFrameId: null,   // the frame whose boxes this volunteer holds, if any
  mine: { session_count: 0, recent: [] },
  HEADERS: { "Content-Type": "application/json", "X-Requested-With": "wahoobay" },
};

RV.el = function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2), value);
    } else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
};

const SVG_NS = "http://www.w3.org/2000/svg";
RV.svg = function svg(tag, attrs) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs || {})) node.setAttribute(key, String(value));
  return node;
};

// ---- server calls -----------------------------------------------------------

// Follow-up B3: a request that never settles would otherwise hold RV.next()'s
// queue -- and with it every lane click -- until the page is reloaded.
RV.API_TIMEOUT_MS = 20000;
RV.TIMED_OUT = "the server took too long; try again";

RV.api = async function api(method, url, body) {
  const controller = new AbortController();
  const options = { method, credentials: "same-origin", headers: RV.HEADERS,
    signal: controller.signal };
  if (body !== undefined) options.body = JSON.stringify(body);
  const timer = setTimeout(() => controller.abort(), RV.API_TIMEOUT_MS);
  try {
    let response;
    try {
      response = await fetch(url, options);
    } catch (err) {
      if (controller.signal.aborted) return RV.timedOut();
      RV.banner("Can't reach the server. Check your connection; nothing was saved.");
      return { ok: false, status: 0, data: { error: "network error" } };
    }
    if (response.status === 401) {
      window.location.href = "/login?next=" + encodeURIComponent("/review");
      return { ok: false, status: 401, data: { error: "signed out" } };
    }
    let data = {};
    try {
      data = await response.json();
    } catch (err) {
      if (controller.signal.aborted) return RV.timedOut();
      data = {};
    }
    if (response.status === 503) {
      RV.banner("The database is unavailable right now. Nothing was saved; try again in a minute.");
    } else if (response.ok) {
      RV.banner(null);
    }
    return { ok: response.ok, status: response.status, data: data || {} };
  } finally {
    clearTimeout(timer);
  }
};

RV.timedOut = function timedOut() {
  RV.banner("The server took too long to answer, so your last action may not have been saved. Try again.");
  return { ok: false, status: 0, data: { error: RV.TIMED_OUT } };
};
RV.get = (url) => RV.api("GET", url);
RV.post = (url, body) => RV.api("POST", url, body || {});
RV.patch = (url, body) => RV.api("PATCH", url, body || {});
RV.put = (url, body) => RV.api("PUT", url, body || {});
RV.del = (url) => RV.api("DELETE", url);

RV.banner = function banner(message) {
  const b = document.getElementById("rv-banner");
  b.textContent = message || "";
  b.hidden = !message;
};

RV.toast = function toast(message) {
  const t = document.getElementById("rv-toast");
  t.textContent = message;
  t.hidden = false;
  clearTimeout(RV.toastTimer);
  RV.toastTimer = setTimeout(() => { t.hidden = true; }, 4000);
};

RV.pct = (p) => (p === null || p === undefined ? "" : `${Math.round(p * 100)}%`);

// Every time on this page is shown in the site's own time zone -- the one the
// server puts on <html data-tz> from TZ -- with daylight saving applied from
// the browser's time-zone data and the zone's abbreviation shown, whatever
// zone the viewer's computer is set to.
RV.TIME_ZONE =
  (typeof document !== "undefined" && document.documentElement && document.documentElement.dataset.tz) ||
  "UTC";
RV.fmtTime = function fmtTime(iso, timeOnly) {
  const d = new Date(iso);
  const opts = { timeZone: RV.TIME_ZONE, timeZoneName: "short" };
  return timeOnly ? d.toLocaleTimeString("en-US", opts) : d.toLocaleString("en-US", opts);
};

// "Counts toward <species>" with its progress bar, shared by the single-box
// and whole-frame views (spec amendment 2026-09-25).
RV.progressBar = function progressBar(t) {
  if (!t) return null;
  const fill = Math.min(100, Math.round((100 * t.count) / t.target));
  return RV.el("div", { class: "rv-progress" },
    RV.el("span", { class: "rv-label", text: "Counts toward" }),
    RV.el("span", { text: t.common || t.name }),
    RV.el("span", { class: "rv-bar", "aria-hidden": "true" }, RV.el("i", { style: `width:${fill}%` })),
    RV.el("span", { class: "rv-muted", text: `${t.count} / ${t.target}` }));
};

// ---- the species catalogue ------------------------------------------------

RV.loadCatalogue = async function loadCatalogue() {
  const r = await RV.get("/api/review/species");
  if (!r.ok) return false;
  RV.catalogue = r.data.species || [];
  RV.byKey = new Map(RV.catalogue.map((e) => [e.key, e]));
  return true;
};

RV.entry = function entry(name, lifeStage) {
  return RV.byKey.get(`${name}|${lifeStage || ""}`)
    || RV.catalogue.find((e) => e.name === name) || null;
};

RV.search = function search(query, limit) {
  const q = query.trim().toLowerCase();
  if (q.length < 2) return [];
  const scored = [];
  for (const e of RV.catalogue) {
    const common = (e.common || "").toLowerCase();
    const name = e.name.toLowerCase();
    let score = -1;
    if (common.startsWith(q) || name.startsWith(q)) score = 0;
    else if (`${common} ${name}`.split(/\s+/).some((w) => w.startsWith(q))) score = 1;
    else if (common.includes(q) || name.includes(q)) score = 2;
    if (score >= 0) scored.push([score, e]);
  }
  scored.sort((a, b) => a[0] - b[0]
    || (a[1].common || a[1].name).localeCompare(b[1].common || b[1].name));
  return scored.slice(0, limit || 8).map((s) => s[1]);
};

RV.SpeciesPicker = class SpeciesPicker {
  constructor(container, onPick) {
    this.onPick = onPick;
    this.results = [];
    this.active = -1;
    this.input = RV.el("input", {
      type: "search", class: "rv-search", autocomplete: "off",
      placeholder: "Search common or Latin name…  ( / )", "aria-label": "Search species",
    });
    this.list = RV.el("ul", { class: "rv-results", role: "listbox" });
    this.input.addEventListener("input", () => this.update());
    this.input.addEventListener("keydown", (ev) => this.onKey(ev));
    container.append(this.input, this.list);
  }

  focus() { this.input.focus(); this.input.select(); }

  clear() { this.input.value = ""; this.update(); }

  update() {
    this.results = RV.search(this.input.value, 8);
    this.active = this.results.length ? 0 : -1;
    this.render();
  }

  render() {
    this.list.replaceChildren(...this.results.map((e, i) => RV.el("li", {
      class: "rv-result" + (i === this.active ? " active" : ""), role: "option",
      onmousedown: (ev) => { ev.preventDefault(); this.pick(e); },
    },
    e.photo_url ? RV.el("img", { src: e.photo_url, alt: "", loading: "lazy" })
      : RV.el("span", { class: "rv-nophoto", "aria-hidden": "true" }),
    RV.el("span", { class: "rv-result-text" },
      RV.el("span", { class: "rv-common", text: e.common || e.name }),
      RV.el("span", { class: "rv-latin", text: e.name + (e.life_stage ? ` · ${e.life_stage}` : "") })))));
  }

  onKey(ev) {
    if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
      ev.preventDefault();
      if (!this.results.length) return;
      const step = ev.key === "ArrowDown" ? 1 : -1;
      this.active = (this.active + step + this.results.length) % this.results.length;
      this.render();
    } else if (ev.key === "Enter") {
      ev.preventDefault();
      if (this.active >= 0) this.pick(this.results[this.active]);
    } else if (ev.key === "Escape") {
      this.clear();
      this.input.blur();
    }
  }

  pick(entry) { this.clear(); this.input.blur(); this.onPick(entry); }
};

// ---- accepting a model's guess -----------------------------------------------

// "Looks right" sends no life_stage. If the catalogue lists `model`'s guess
// only under specific life stages (no plain, stage-less entry), the server
// rejects a bare accept with "choose a life stage for <species>"; ask first
// instead of round-tripping that 400. RV.entry(name, null) alone can't tell
// "no ambiguity" from "ambiguous, but here's *an* entry anyway" -- it
// deliberately falls back to any entry with that name (used elsewhere just
// to show a representative photo) -- so confirm the entry it returns really
// has no life_stage, and otherwise fall back to enumerating the catalogue's
// stages for this name.
//
// Shared by review-single.js and review-frame.js (fix round 1, Important
// finding: their acceptGuess() functions were a near-verbatim duplication
// of this exact check, risking the life-stage rule drifting between the
// two views). `onAnswer(extra)` is called with `{}` for an unambiguous
// accept, or `{life_stage}` once one is chosen (from a single remaining
// stage, or picked from the chips rendered into `stageContainerId`); each
// view passes its own submit function -- already wrapped in that view's own
// busy guard -- as `onAnswer`.
RV.acceptGuessFlow = function acceptGuessFlow(model, stageContainerId, onAnswer) {
  const name = model.name;
  const plain = RV.entry(name, null);
  if (plain && !plain.life_stage) { onAnswer({}); return; }
  const stages = RV.catalogue.filter((e) => e.name === name);
  if (stages.length < 2) {
    onAnswer({ life_stage: stages.length ? stages[0].life_stage : null });
    return;
  }
  const who = model.common || name;
  document.getElementById(stageContainerId).replaceChildren(
    RV.el("span", { class: "rv-label", text: `Which life stage of ${who}?` }),
    RV.el("div", { class: "rv-chips" }, stages.map((s) => RV.el("button", {
      type: "button", class: "rv-chip", onclick: () => onAnswer({ life_stage: s.life_stage }),
    }, s.life_stage || who))));
};

// ---- the box editor ---------------------------------------------------------

RV.clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi);

RV.handlesOf = function handlesOf(box) {
  const [x1, y1, x2, y2] = box;
  const mx = (x1 + x2) / 2;
  const my = (y1 + y2) / 2;
  return [[x1, y1, "nw"], [mx, y1, "n"], [x2, y1, "ne"], [x2, my, "e"],
    [x2, y2, "se"], [mx, y2, "s"], [x1, y2, "sw"], [x1, my, "w"]];
};

// Apply a drag of (dx, dy) in `mode` ("move" or a handle name such as
// "nw") to the box `start`, keeping it inside the frame and at least 4 px
// wide and tall. All values are frame pixels.
RV.dragBox = function dragBox(start, mode, dx, dy, frameW, frameH) {
  let [x1, y1, x2, y2] = start;
  if (mode === "move") {
    const w = x2 - x1;
    const h = y2 - y1;
    x1 = RV.clamp(x1 + dx, 0, frameW - w);
    y1 = RV.clamp(y1 + dy, 0, frameH - h);
    x2 = x1 + w;
    y2 = y1 + h;
  } else {
    if (mode.includes("w")) x1 = RV.clamp(x1 + dx, 0, x2 - 4);
    if (mode.includes("e")) x2 = RV.clamp(x2 + dx, x1 + 4, frameW);
    if (mode.includes("n")) y1 = RV.clamp(y1 + dy, 0, y2 - 4);
    if (mode.includes("s")) y2 = RV.clamp(y2 + dy, y1 + 4, frameH);
  }
  return [Math.round(x1), Math.round(y1), Math.round(x2), Math.round(y2)];
};

// A draggable box over an image showing `region` ([x0, y0, x1, y1] in
// frame pixels). The SVG's viewBox is the region itself, so the box is
// stored and drawn in frame pixels; only the pointer is converted.
RV.BoxEditor = class BoxEditor {
  constructor(container, { imageUrl, region, box, frameW, frameH, others, editable }) {
    this.region = region;
    this.original = box.slice();
    this.box = box.slice();
    this.frameW = frameW;
    this.frameH = frameH;
    this.others = others || [];
    this.editable = editable !== false;
    this.drag = null;
    const [x0, y0, x1, y1] = region;
    this.svgEl = RV.svg("svg", {
      class: "rv-editor-svg", viewBox: `${x0} ${y0} ${x1 - x0} ${y1 - y0}`,
      preserveAspectRatio: "none",
    });
    const wrap = RV.el("div", { class: "rv-editor" },
      RV.el("img", { src: imageUrl, alt: "The detected animal, zoomed", draggable: "false" }));
    wrap.append(this.svgEl);
    container.replaceChildren(wrap);
    this.svgEl.addEventListener("pointerdown", (ev) => this.onDown(ev));
    this.svgEl.addEventListener("pointermove", (ev) => this.onMove(ev));
    this.svgEl.addEventListener("pointerup", (ev) => this.onUp(ev));
    this.svgEl.addEventListener("pointercancel", (ev) => this.onUp(ev));
    this.draw();
  }

  get moved() { return this.box.some((v, i) => v !== this.original[i]); }

  reset() { this.box = this.original.slice(); this.draw(); }

  handleSize() { return (this.region[2] - this.region[0]) / 40; }

  toFrame(ev) {
    const r = this.svgEl.getBoundingClientRect();
    const [x0, y0, x1, y1] = this.region;
    return [x0 + ((ev.clientX - r.left) / r.width) * (x1 - x0),
      y0 + ((ev.clientY - r.top) / r.height) * (y1 - y0)];
  }

  draw() {
    const [x1, y1, x2, y2] = this.box;
    const nodes = this.others.map((o) => RV.svg("rect", {
      x: o[0], y: o[1], width: o[2] - o[0], height: o[3] - o[1], class: "rv-other" }));
    nodes.push(RV.svg("rect", { x: x1, y: y1, width: x2 - x1, height: y2 - y1, class: "rv-box" }));
    if (this.editable) {
      const s = this.handleSize();
      for (const [hx, hy] of RV.handlesOf(this.box)) {
        nodes.push(RV.svg("rect", { x: hx - s / 2, y: hy - s / 2, width: s, height: s, class: "rv-handle" }));
      }
    }
    this.svgEl.replaceChildren(...nodes);
  }

  onDown(ev) {
    if (!this.editable) return;
    const [px, py] = this.toFrame(ev);
    const s = this.handleSize();
    let mode = null;
    for (const [hx, hy, name] of RV.handlesOf(this.box)) {
      if (Math.abs(px - hx) <= s && Math.abs(py - hy) <= s) { mode = name; break; }
    }
    const [x1, y1, x2, y2] = this.box;
    if (!mode && px > x1 && px < x2 && py > y1 && py < y2) mode = "move";
    if (!mode) return;
    ev.preventDefault();
    this.svgEl.setPointerCapture(ev.pointerId);
    this.drag = { mode, start: [px, py], box: this.box.slice() };
  }

  onMove(ev) {
    if (!this.drag) return;
    const [px, py] = this.toFrame(ev);
    this.box = RV.dragBox(this.drag.box, this.drag.mode, px - this.drag.start[0],
      py - this.drag.start[1], this.frameW, this.frameH);
    this.draw();
  }

  onUp(ev) {
    if (!this.drag) return;
    this.drag = null;
    try { this.svgEl.releasePointerCapture(ev.pointerId); } catch (err) { /* already released */ }
  }
};

// ---- keys, views, the session ---------------------------------------------

RV.onKey = function onKey(view, handler) { RV.keyHandlers[view] = handler; };

document.addEventListener("keydown", (ev) => {
  const t = ev.target;
  const typing = t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA"
    || t.tagName === "SELECT" || t.isContentEditable);
  // ev.repeat: OS key-repeat on a held-down key must not re-fire an action
  // every few milliseconds (fix round 1). Every view's handler goes
  // through this one dispatcher, so the guard belongs here once rather
  // than in each view's own handler.
  if (typing || ev.repeat || ev.ctrlKey || ev.metaKey || ev.altKey) return;
  const handler = RV.keyHandlers[RV.view];
  if (handler && handler(ev.key, ev) === true) ev.preventDefault();
});

// ---- claims: the single box held, and the frame held --------------------------
// Spec amendment 2026-09-25. A box the queue served is recorded with its
// frame, so opening that same frame keeps the claim instead of releasing it
// and re-claiming it a moment later -- a gap in which someone else could
// take it. Leaving a frame any way but "Next frame" gives its boxes back
// without skipping them.

RV.releaseHeld = function releaseHeld(keepFrameId) {
  const held = RV.held;
  if (!held || (keepFrameId && held.savedFrameId === keepFrameId)) return undefined;
  RV.held = null;
  return RV.post(`/api/review/items/${held.itemId}/release`);
};

RV.releaseFrame = function releaseFrame() {
  const id = RV.openFrameId;
  RV.openFrameId = null;
  return id ? RV.post(`/api/review/frames/${id}/release`) : undefined;
};

RV.releaseAll = async function releaseAll() {
  await Promise.all([RV.releaseHeld(), RV.releaseFrame()]);
};

// ---- the mode: whole frame (the default) or single boxes ------------------------
// Remembered in this browser. Storage can be blocked (private windows,
// strict settings), so every access is guarded and any failure means the
// default.

RV.MODE_KEY = "rv.mode";
// Fix round 1, Minor B: kept in memory too, or a blocked write is silently
// dropped and mode() falls back all the way to "frame" -- so choosing
// "single boxes" in a private window or under strict settings could never
// stick, and F in the frame view would just release and reopen a frame.
RV.modeValue = null;

RV.mode = function mode() {
  // Fix round 2, Minor (carried): RV.modeValue is only ever set by setMode
  // during this page's lifetime, so it must win over whatever storage
  // holds -- otherwise a readable-but-unwritable store (a stale "frame"
  // read succeeds while a "single" write silently fails, e.g. quota full)
  // would override the in-memory choice instead of the other way around.
  if (RV.modeValue) return RV.modeValue;
  try {
    const stored = window.localStorage.getItem(RV.MODE_KEY);
    if (stored === "single" || stored === "frame") return stored;
  } catch (err) {
    /* storage blocked: fall through to the default */
  }
  return "frame";
};

RV.setMode = function setMode(value) {
  RV.modeValue = value;
  try {
    window.localStorage.setItem(RV.MODE_KEY, value);
  } catch (err) {
    /* storage blocked: the in-memory value still applies this session */
  }
};

// ---- the next piece of work ------------------------------------------------------
// The one place /api/review/next is called, so a box is never claimed
// twice. A spot-check (item.pass 2; its item.lane is null) always goes to
// the blind single-box view; any other box opens its whole frame with the
// box selected, unless the volunteer chose single boxes.
//
// Final review I1: the latest navigation wins. Every view switch (RV.show)
// and every call here takes a new RV.navToken, so a /next that comes back
// after the volunteer has moved on -- Browse, another lane, a reopened box --
// neither switches the view nor keeps its box: it gives the box back. And
// only one /next is out at a time: each call waits for the one before it to
// hand over its turn, which a superseded call does only once its release has
// landed. Otherwise the server's held path could hand the same box to both
// calls (the same lane, or a spot-check from any lane) and the stale release
// would take it away from the call that shows it. A call made on behalf of
// an older action passes that action's token and gives way (follow-up B1).

RV.navToken = 0;
RV.nextTurn = Promise.resolve();

RV.next = async function next(startToken) {
  // Follow-up B1: an answer, skip or page load that started before the
  // volunteer moved on (Browse, a lane, the recent strip) passes the token it
  // started with. If they have navigated since, their newer choice stands.
  if (startToken !== undefined && startToken !== RV.navToken) return;
  const token = ++RV.navToken;
  const turn = RV.nextTurn;
  let handOff;
  RV.nextTurn = new Promise((resolve) => { handOff = resolve; });
  let r;
  try {
    await turn;
    if (token !== RV.navToken) return;       // superseded before it asked
    r = await RV.get(`/api/review/next?lane=${encodeURIComponent(RV.lane)}`);
    if (token !== RV.navToken) {             // superseded while it asked
      if (r.ok && r.data.item) await RV.post(`/api/review/items/${r.data.item.id}/release`);
      return;
    }
  } finally {
    // Before any view switch: a view's enter() may itself call this again,
    // and must find its turn already free.
    handOff();
  }
  if (!r.ok || !r.data.item) {
    RV.held = null;
    await RV.show("single", { result: r });
    return;
  }
  const payload = r.data;
  RV.held = { itemId: payload.item.id, savedFrameId: payload.box.saved_frame_id };
  if (payload.item.pass !== 2 && RV.views.frame && RV.mode() === "frame") {
    await RV.show("frame", { savedFrameId: payload.box.saved_frame_id,
      selectEventId: payload.item.event_id, payload });
  } else {
    await RV.show("single", { payload });
  }
};

RV.show = async function show(view, arg) {
  if (!RV.views[view]) return;
  RV.navToken += 1;      // supersedes any RV.next() still waiting on /next
  if (view !== "frame") RV.releaseFrame();
  for (const id of ["single", "frame", "browse"]) {
    const section = document.getElementById("rv-" + id);
    if (section) section.hidden = id !== view;
  }
  RV.view = view;
  for (const b of document.querySelectorAll("#rv-nav button")) {
    const on = b.dataset.view ? b.dataset.view === view
      : view !== "browse" && b.dataset.lane === RV.lane;
    b.classList.toggle("on", on);
    b.setAttribute("aria-pressed", on ? "true" : "false");
  }
  document.getElementById("rv-recent").hidden = view !== "single";
  await RV.views[view].enter(arg || {});
};

RV.refreshMine = async function refreshMine() {
  const r = await RV.get("/api/review/mine/recent");
  if (!r.ok) return;
  RV.mine = r.data;
  document.getElementById("rv-session-count").textContent = String(r.data.session_count || 0);
  if (RV.views.single && RV.views.single.renderRecent) RV.views.single.renderRecent();
};

window.addEventListener("pagehide", () => {
  const urls = [];
  if (RV.held) urls.push(`/api/review/items/${RV.held.itemId}/release`);
  if (RV.openFrameId) urls.push(`/api/review/frames/${RV.openFrameId}/release`);
  for (const url of urls) {
    fetch(url, { method: "POST", keepalive: true, credentials: "same-origin",
      headers: RV.HEADERS, body: "{}" });
  }
});

// Final review M1: pagehide also fires when the page goes into the
// back/forward cache, so by the time Back restores it the releases above have
// already given its claims back -- while RV.held and RV.openFrameId still name
// them, and the first answer would be refused as "not served to you". Reload
// instead, so the fresh page asks the queue again.
window.addEventListener("pageshow", (ev) => {
  if (ev.persisted) window.location.reload();
});

document.addEventListener("DOMContentLoaded", async () => {
  const landing = RV.navToken;   // follow-up B1: a click made while loading wins
  for (const b of document.querySelectorAll("#rv-nav button")) {
    const view = b.dataset.view || "single";
    b.hidden = !RV.views[view];
    b.addEventListener("click", async () => {
      // Fix round 1, Important 1(b): ignore the click outright while the
      // frame view has its own answer in flight, or /next could claim a
      // fresh box for a view that then refuses to render it (frame.enter()
      // is itself busy-guarded), and a late reload() would re-claim the
      // frame this leaves behind. Fix round 2: dropped the RV.view ===
      // "frame" condition -- a stale frame can still be busy (an answer
      // started after RV.view already moved on, e.g. via "Change my
      // answer") and must still be caught, regardless of which view is
      // currently on screen.
      if (RV.views.frame && RV.views.frame.isBusy && RV.views.frame.isBusy()) return;
      // Released -- and awaited -- before the queue is asked again, or
      // /next could hand back a box from the frame just left as "held".
      await RV.releaseAll();
      if (b.dataset.lane) {
        RV.lane = b.dataset.lane;
        await RV.next();
      } else {
        await RV.show(view);
      }
    });
  }
  if (!(await RV.loadCatalogue())) {
    RV.banner("Couldn't load the species list. Reload the page to try again.");
    return;
  }
  await RV.refreshMine();
  await RV.next(landing);
});
