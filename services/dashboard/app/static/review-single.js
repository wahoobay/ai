// services/dashboard/app/static/review-single.js
// The single-box view (layout B): the zoomed box is the work area; the
// side panel holds the frame, the model's guess, the species search, the
// hints and the answer buttons. Also owns the recent-boxes strip.
//
// Adapted from the phase-1 brief to the server's real behaviour (Tasks
// 7-9's fix rounds): a pass-2 (spot-check) item is told apart by
// item.pass, never item.lane, which the server now always sends as null
// for a blind item; a reopened box that another volunteer has since
// touched arrives with editable=false and a reason, and is shown
// read-only with no way to submit; "Looks right" asks for a life stage
// first when the catalogue only lists the model's guess under specific
// stages (the server otherwise rejects it with "choose a life stage for
// <species>"); and a box that changed out from under the volunteer -- a
// 409, a 403 "this box was not served to you", or a 404 for a missing
// frame -- moves on to the next box instead of leaving them stuck.
"use strict";

RV.views.single = (() => {
  const root = () => document.getElementById("rv-single");
  let current = null;       // the payload being answered
  let editor = null;
  let picker = null;
  let reopened = false;
  let notSure = false;
  let notes = "";
  let hintName = null;      // set while choosing a life stage for a hint
  let busy = false;

  const kbd = (key) => RV.el("kbd", { text: key });

  async function enter(arg) {
    if (arg.reopenEventId) { await reopen(arg.reopenEventId); return; }
    if (arg.payload) { showPayload(arg.payload); return; }
    if (arg.result) { showResult(arg.result); return; }
    await loadNext();
  }

  function message(text) {
    root().replaceChildren(RV.el("p", { class: "rv-empty", text }));
  }

  function showPayload(payload) {
    reopened = false;
    current = payload;
    render();
  }

  function showResult(r) {
    current = null;
    if (!r.ok) { message(r.data.error || "Couldn't load a box."); return; }
    renderEmpty(r.data.pool);
  }

  // Everything that moves on goes through the shared router, which asks
  // the queue once and picks the view (spec amendment 2026-09-25).
  async function loadNext() {
    reopened = false;
    message("Loading the next box…");
    await RV.next();
  }

  async function reopen(eventId) {
    // Shares submit()/skip()'s busy guard (fix round 1): opening a recent
    // box while another mutation is in flight would otherwise let that
    // action's trailing loadNext() clobber the view this just opened.
    if (busy) return;
    busy = true;
    try {
      const r = await RV.get(`/api/review/boxes/${eventId}`);
      if (!r.ok) {
        RV.toast(r.data.error || "Couldn't reopen that box.");
        await loadNext();
        return;
      }
      RV.releaseHeld();
      reopened = true;
      current = r.data;
      render();
    } finally {
      busy = false;
    }
  }

  function renderEmpty(pool) {
    const when = pool && pool.finished_at ? new Date(pool.finished_at).toLocaleTimeString() : null;
    root().replaceChildren(RV.el("div", { class: "rv-empty" },
      RV.el("p", { text: "No boxes are ready in this lane right now." }),
      RV.el("p", { class: "rv-muted", text: when
        ? `The pool was last topped up at ${when}; it refills every few minutes.`
        : "The pool is filling; check back in a few minutes." }),
      RV.el("button", { type: "button", onclick: () => loadNext() }, "Try again")));
  }

  function frameThumb(c) {
    const svg = RV.svg("svg", { class: "rv-thumb-svg", viewBox: `0 0 ${c.frame.width} ${c.frame.height}`,
      preserveAspectRatio: "none" });
    for (const o of c.other_boxes) {
      svg.append(RV.svg("rect", { x: o.bbox[0], y: o.bbox[1], width: o.bbox[2] - o.bbox[0],
        height: o.bbox[3] - o.bbox[1], class: "rv-other" }));
    }
    const b = c.box.model_bbox;
    svg.append(RV.svg("rect", { x: b[0], y: b[1], width: b[2] - b[0], height: b[3] - b[1], class: "rv-box" }));
    const wrap = RV.el("div", { class: "rv-thumb" },
      RV.el("img", { src: `${c.frame.url}?size=640`, alt: "The whole frame" }));
    wrap.append(svg);
    return wrap;
  }

  function progressBar(c) {
    if (!c.counts_toward) return null;
    const t = c.counts_toward;
    const fill = Math.min(100, Math.round((100 * t.count) / t.target));
    return RV.el("div", { class: "rv-progress" },
      RV.el("span", { class: "rv-label", text: "Counts toward" }),
      RV.el("span", { text: t.common || t.name }),
      RV.el("span", { class: "rv-bar", "aria-hidden": "true" }, RV.el("i", { style: `width:${fill}%` })),
      RV.el("span", { class: "rv-muted", text: `${t.count} / ${t.target}` }));
  }

  // The life-stage check (ask first, rather than round-trip the server's
  // "choose a life stage for <species>" 400) is shared with review-frame.js
  // via RV.acceptGuessFlow (review-core.js) -- fix round 1, Important
  // finding: this used to reimplement it inline, identically to
  // review-frame.js's own copy.
  function acceptGuess() {
    if (!current || !current.model) return;
    RV.acceptGuessFlow(current.model, "rv-stage", (extra) => submit("accept", extra));
  }

  function guessCard(c) {
    if (!c.model) return null;
    const e = RV.entry(c.model.name, null);
    return RV.el("div", { class: "rv-guess" },
      e && e.photo_url ? RV.el("img", { src: e.photo_url, alt: "" }) : null,
      RV.el("div", { class: "rv-guess-text" },
        RV.el("div", { class: "rv-common", text: c.model.common || c.model.name }),
        RV.el("div", { class: "rv-latin", text: `${c.model.name} · model ${RV.pct(c.model.p)}` }),
        e && e.photo_credit ? RV.el("div", { class: "rv-credit", text: `Photo ${e.photo_credit}` }) : null),
      RV.el("button", { type: "button", class: "rv-primary", onclick: () => acceptGuess() },
        "✓ Looks right ", kbd("A")),
      RV.el("div", { class: "rv-stage", id: "rv-stage" }));
  }

  function chooseHint(h) {
    const entries = RV.catalogue.filter((e) => e.name === h.name);
    if (!entries.length) { RV.toast("That species isn't in the list any more."); return; }
    if (entries.length === 1) { pickSpecies(entries[0], "hint"); return; }
    // Several life stages: let the volunteer pick one, and remember the
    // choice came from a hint.
    hintName = h.name;
    picker.input.value = h.name;
    picker.update();
    picker.focus();
  }

  function hintChips(c) {
    if (!c.hints || !c.hints.length) return null;
    const title = c.model ? "The model's other guesses" : "The model's weak guesses (under 55%, often wrong)";
    return RV.el("div", { class: "rv-hints" },
      RV.el("div", { class: "rv-label", text: title }),
      RV.el("div", { class: "rv-chips" }, c.hints.map((h, i) => RV.el("button", {
        type: "button", class: "rv-chip", onclick: () => chooseHint(h),
      }, `${h.common || h.name} ${RV.pct(h.p)} `, kbd(String(i + 1))))));
  }

  // Shared by render() and renderLocked() (fix round 1: was duplicated
  // near-verbatim between the two): mounts a fresh BoxEditor over the
  // zoomed box and returns its container, ready to drop into the layout.
  function buildEditor(c, editable) {
    const work = RV.el("div", { class: "rv-work" });
    editor = new RV.BoxEditor(work, {
      imageUrl: c.zoom.url, region: c.zoom.region, box: c.box.bbox,
      frameW: c.frame.width, frameH: c.frame.height, others: c.other_boxes.map((o) => o.bbox),
      editable,
    });
    return work;
  }

  // Shared by render() and renderLocked(): the two-column layout (the
  // work area with its caption, plus the side panel) that replaces
  // #rv-single's contents.
  function wrapLayout(work, captionText, side) {
    root().replaceChildren(RV.el("div", { class: "rv-layout" },
      RV.el("div", {}, work, RV.el("p", { class: "rv-cap", text: captionText })),
      side));
  }

  // Reopened, but the server says it's no longer editable (Task 9): some
  // other volunteer answered or is answering it since. Show the box and
  // the reason, with no species picker, hints or answer buttons -- the
  // server would refuse a submit here anyway, and offering one just to
  // reject it wastes the volunteer's time.
  function renderLocked(c) {
    const work = buildEditor(c, false);
    const side = RV.el("aside", { class: "rv-side" },
      RV.el("p", { class: "rv-note", text: c.reason || "This box can no longer be changed." }),
      frameThumb(c),
      RV.el("button", { type: "button", onclick: () => loadNext() }, "Load the next box"));
    wrapLayout(work, "This box can't be changed any more.", side);
  }

  function render() {
    const c = current;
    notSure = false;
    notes = "";
    hintName = null;
    if (c.editable === false) { renderLocked(c); return; }
    const work = buildEditor(c, c.editable);
    const side = RV.el("aside", { class: "rv-side" });
    if (reopened) {
      side.append(RV.el("p", { class: "rv-note", text: "Changing your answer: pick again and it replaces your last one." }));
    }
    if (c.item.pass === 2) {
      side.append(RV.el("p", { class: "rv-note", text: "Second opinion: answer from the picture alone." }));
    }
    side.append(frameThumb(c));
    for (const part of [progressBar(c), guessCard(c)]) if (part) side.append(part);
    const pickerSlot = RV.el("div", { class: "rv-picker" });
    side.append(RV.el("div", { class: "rv-label", text: c.model ? "Or pick the species" : "Pick the species" }),
      pickerSlot);
    picker = new RV.SpeciesPicker(pickerSlot, (entry) => {
      const source = hintName && entry.name === hintName ? "hint" : "search";
      hintName = null;
      pickSpecies(entry, source);
    });
    const chips = hintChips(c);
    if (chips) side.append(chips);
    side.append(RV.el("div", { class: "rv-row" },
      RV.el("button", { type: "button", onclick: () => submit("unidentifiable", {}) }, "Can't identify ", kbd("C")),
      RV.el("button", { type: "button", onclick: () => submit("no_animal", {}) }, "No animal here ", kbd("N"))));
    const noteBox = RV.el("textarea", { class: "rv-notes", rows: "2", placeholder: "Note (optional)", hidden: true });
    noteBox.addEventListener("input", () => { notes = noteBox.value; });
    side.append(RV.el("div", { class: "rv-row rv-small" },
      RV.el("label", { class: "rv-check" },
        RV.el("input", { type: "checkbox", onchange: (ev) => { notSure = ev.target.checked; } }), " Not sure"),
      RV.el("button", { type: "button", class: "rv-link", onclick: () => {
        noteBox.hidden = !noteBox.hidden;
        if (!noteBox.hidden) noteBox.focus();
      } }, "+ note"),
      RV.el("button", { type: "button", class: "rv-link", onclick: () => skip() }, "Skip ", kbd("S")),
      frameAllowed(c)
        ? RV.el("button", { type: "button", class: "rv-link", onclick: () => openFrame() }, "Whole frame ", kbd("F"))
        : null),
    noteBox, RV.el("div", { class: "rv-celeb", id: "rv-celeb" }));
    wrapLayout(work, "Zoomed with room around the box. Drag an edge or corner to fix it; Esc puts it back.", side);
  }

  function pickSpecies(entry, source) {
    const base = { label: entry.name, life_stage: entry.life_stage, label_source: source };
    const celebs = entry.celebrities || [];
    if (!celebs.length) { submit("species", base); return; }
    const who = entry.common || entry.name;
    document.getElementById("rv-celeb").replaceChildren(
      RV.el("span", { class: "rv-label", text: `Is this a named ${who}?` }),
      ...celebs.map((c) => RV.el("button", { type: "button",
        onclick: () => submit("species", { ...base, celebrity_id: c.id }) }, c.nickname)),
      RV.el("button", { type: "button", onclick: () => submit("species", base) }, `No, just a ${who}`));
  }

  async function submit(outcome, extra) {
    if (!current || busy) return;
    busy = true;
    try {
      const body = { event_id: current.item.event_id, review_item_id: reopened ? null : current.item.id,
        outcome, ...extra };
      if (outcome === "accept" || outcome === "species") body.not_sure = notSure;
      if (editor && editor.moved && outcome !== "no_animal") body.bbox = editor.box;
      if (notes.trim()) body.notes = notes.trim();
      const r = await RV.post("/api/review/verdicts", body);
      // A box that changed out from under the volunteer -- someone else
      // just answered it (409), this claim was never really theirs (403
      // "this box was not served to you"), or its frame vanished from
      // disk between load and submit (404) -- can't be fixed by
      // resubmitting the same thing, so move on rather than leave them
      // stuck. Every other failure (a bad box, an unchosen life stage, a
      // stale species list) is the volunteer's to correct, so it stays.
      if (r.status === 409 || r.status === 404
          || (r.status === 403 && r.data.error === "this box was not served to you")) {
        RV.toast(r.data.error || "That box isn't available any more. Here's the next one.");
        RV.held = null;
        await loadNext();
        return;
      }
      if (!r.ok) {
        RV.toast(r.data.error || "That answer wasn't saved.");
        // Review Focus 5: the species list may have changed since the page loaded.
        if (r.data.error === "pick a species from the list") await RV.loadCatalogue();
        return;
      }
      RV.held = null;
      await RV.refreshMine();
      await loadNext();
    } finally {
      busy = false;
    }
  }

  async function skip() {
    // Shares submit()'s busy guard (fix round 1): without it, a held-down
    // S key (the shared dispatcher now ignores auto-repeat, but a fast
    // double-tap or a double click on the "Skip" link still reaches here
    // twice) fires two overlapping POSTs, orphans the held item, and can
    // race a concurrent submit() on the same box.
    if (!current || busy) return;
    busy = true;
    try {
      if (!reopened && current.item.id) {
        await RV.post(`/api/review/items/${current.item.id}/skip`);
        RV.held = null;
      }
      await loadNext();
    } finally {
      busy = false;
    }
  }

  // Whole-frame mode shows the other boxes' labels and model guesses, so a
  // spot-check (item.pass 2; its item.lane is null) never offers it: that
  // would contaminate the second opinion (final review I1). The server
  // refuses as well, with a 409, while the spot-check is held.
  function frameAllowed(c) {
    return Boolean(RV.views.frame && c && c.item.pass !== 2);
  }

  // Fix round 1, Minor A: if the frame then fails to open, fall back to
  // showing this same box rather than a bare error -- unless it's a
  // reopened ("change my answer") box, which was never a live queue claim
  // and has no payload of its own to fall back to. The mode is only
  // remembered as "frame" once the frame has actually opened, so a failed
  // attempt doesn't strand the volunteer in single mode with no box shown.
  async function openFrame(eventId) {
    if (frameAllowed(current)) {
      const savedFrameId = current.box.saved_frame_id;
      const arg = { savedFrameId, selectEventId: eventId || current.item.event_id };
      if (!reopened) arg.payload = current;
      await RV.show("frame", arg);
      if (RV.openFrameId === savedFrameId) RV.setMode("frame");
    }
  }

  function renderRecent() {
    const strip = document.getElementById("rv-recent");
    const items = (RV.mine.recent || []).slice(0, 12);
    if (!items.length) { strip.replaceChildren(); return; }
    const word = (r) => (r.outcome === "species" ? (r.common || r.label)
      : r.outcome === "no_animal" ? "No animal" : "Can't ID");
    strip.replaceChildren(
      RV.el("span", { class: "rv-muted", text: "Your recent boxes:" }),
      ...items.map((r) => RV.el("button", {
        type: "button", class: "rv-recent-item", title: "Change this answer",
        onclick: () => RV.show("single", { reopenEventId: r.event_id }),
      }, RV.el("img", { src: r.zoom_url, alt: "", loading: "lazy" }), RV.el("span", { text: word(r) }))),
      RV.el("span", { class: "rv-muted", text: "click one to change your answer" }));
  }

  RV.onKey("single", (key) => {
    if (!current || current.editable === false) return false;
    const k = key.length === 1 ? key.toLowerCase() : key;
    if (k === "a" && current.model) { acceptGuess(); return true; }
    if (k === "/") { picker.focus(); return true; }
    if (["1", "2", "3"].includes(k) && current.hints && current.hints[Number(k) - 1]) {
      chooseHint(current.hints[Number(k) - 1]);
      return true;
    }
    if (k === "c") { submit("unidentifiable", {}); return true; }
    if (k === "n") { submit("no_animal", {}); return true; }
    if (k === "s") { skip(); return true; }
    if (k === "f" && frameAllowed(current)) { openFrame(); return true; }
    if (k === "Escape" && editor) { editor.reset(); return true; }
    return false;
  });

  return { enter, loadNext, renderRecent };
})();
