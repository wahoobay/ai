// services/dashboard/app/static/review-frame.js
// Whole-frame mode: the frame is the work area. Every model box is
// numbered and outlined by state; the volunteer answers each one, draws
// boxes around animals the model missed (press D, then drag), and marks
// the frame complete once nothing is left to answer.
//
// Adapted from the phase-1 brief to Tasks 7-10's committed behaviour: a
// busy guard (shared by every action here, the same pattern
// review-single.js's submit()/skip()/reopen() use) keeps a held-down key
// or a fast double click from firing two overlapping requests; entering
// the view keeps a held box from this same frame (the one the queue just
// served, or the single box the volunteer was on) and releases one held
// from any other frame (spec amendment 2026-09-25); and "Looks right"
// asks for a life stage first when the catalogue lists the model's guess
// only under specific stages, exactly as review-single.js's acceptGuess()
// does, rather than round-tripping the server's 400 "choose a life stage
// for <species>".
"use strict";

RV.views.frame = (() => {
  const root = () => document.getElementById("rv-frame");
  let state = null;
  let selected = null;       // {kind: "model"|"added"|"new", id, bbox, original}
  let drawing = false;
  let drag = null;
  let svgEl = null;
  let picker = null;
  let hintName = null;
  let notSure = false;
  let busy = false;

  const kbd = (key) => RV.el("kbd", { text: key });
  const inside = (bb, x, y) => x >= bb[0] && x <= bb[2] && y >= bb[1] && y <= bb[3];
  const modelBox = (id) => state.boxes.find((b) => b.event_id === id);
  const addedBox = (id) => state.added.find((a) => a.id === id);
  const STATE_CLASS = { needs_answer: "rv-fb-pending", answered: "rv-fb-done",
    answered_hidden: "rv-fb-done", locked: "rv-fb-locked" };

  function answerText(outcome, label, common) {
    if (outcome === "no_animal") return "no animal";
    if (outcome === "unidentifiable") return "can't identify";
    return common || label || "?";
  }

  async function enter(arg) {
    const id = arg.savedFrameId || (state && state.saved_frame_id);
    if (!id) { await RV.next(); return; }
    if (busy) return;
    busy = true;
    const nav = RV.navToken;
    try {
      // Keep the claim on a box held from this same frame (the one the
      // queue just served, or the single box the volunteer was on); a box
      // held from any other frame is released as before (spec amendment
      // 2026-09-25).
      RV.releaseHeld(id);
      if (RV.openFrameId && RV.openFrameId !== id) RV.releaseFrame();
      // Follow-up B4: a /next the volunteer superseded may still be out, and
      // gives its box back only once it lands. Wait for that before claiming
      // this frame, or the late release could take back a box the frame has
      // just claimed and cost an answer redone. Then give up if they have
      // navigated again meanwhile.
      await RV.nextTurn;
      if (nav !== RV.navToken) return;
      const r = await RV.post(`/api/review/frames/${id}/open`);
      if (!r.ok) {
        RV.toast(r.data.error || "Couldn't open that frame.");
        // Never ask the queue again from here: a frame that can't be
        // opened would be served straight back. Show the served box on
        // its own instead, or the error.
        RV.show("single", arg.payload ? { payload: arg.payload } : { result: r });
        return;
      }
      state = r.data;
      RV.held = null;            // the frame's claims now include it
      RV.openFrameId = id;
      drawing = false;
      if (!(arg.selectEventId && selectBox(arg.selectEventId))) selectFirst();
      render();
    } finally {
      busy = false;
    }
  }

  function selectBox(eventId) {
    const b = modelBox(eventId);
    if (!b) return false;
    selected = { kind: "model", id: b.event_id, bbox: b.bbox.slice(), original: b.bbox.slice() };
    hintName = null;
    notSure = false;
    return true;
  }

  // Re-opening (not a plain GET) refreshes this volunteer's claims on the
  // frame each time an answer reloads it, so they don't lapse while
  // someone works through a busy frame (spec amendment 2026-09-25). Fix
  // round 1, Important 1: this now claims, so a reload that finishes after
  // the volunteer has already left this frame (F, a lane button, Browse,
  // "Change my answer") must not re-claim its boxes with nothing left to
  // release them -- checked both before sending the POST and after it
  // lands, in case the leave happened while it was in flight.
  async function reload(keepSelection) {
    const id = state.saved_frame_id;
    if (RV.openFrameId !== id) return;
    const r = await RV.post(`/api/review/frames/${id}/open`);
    if (!r.ok) { RV.toast(r.data.error || "Couldn't refresh the frame."); return; }
    if (RV.openFrameId !== id) { RV.post(`/api/review/frames/${id}/release`); return; }
    state = r.data;
    if (!keepSelection) selectFirst();
    render();
  }

  // F / "Single boxes": switch modes for good (remembered in this browser),
  // give this frame's boxes back without skipping them, and let the queue
  // pick the next single box. Fix round 1, Important 2: this sends a
  // mutating request, so it needs the same busy guard as every other
  // action here -- it is also the most direct way into Important 1.
  async function toSingle() {
    // Fix round 2, Important 1: refuse once this frame has already been
    // left (RV.openFrameId no longer names it) -- before the busy check,
    // so a second F landing after the first's release, or any other stale
    // action, can't run at all. See the identical guard on answerModel,
    // saveAddedBox, removeAddedBox and complete below.
    if (!state || RV.openFrameId !== state.saved_frame_id) return;
    if (busy) return;
    busy = true;
    try {
      RV.setMode("single");
      await RV.releaseFrame();
    } finally {
      busy = false;
    }
    await RV.next();
  }

  function selectFirst() {
    const box = state.boxes.find((b) => b.state === "needs_answer");
    selected = box ? { kind: "model", id: box.event_id, bbox: box.bbox.slice(), original: box.bbox.slice() } : null;
    // Cleared here too, not just in select(): otherwise a "not sure"
    // left checked (or a hint mid-pick) on the box just answered would
    // silently carry over onto whichever box reload()'s selectFirst()
    // lands on next.
    hintName = null;
    notSure = false;
  }

  function select(target) {
    selected = target;
    hintName = null;
    notSure = false;
    render();
  }

  // Follow-up B6: admins can edit a completed frame (spec §8/§10); volunteers can't.
  function editableSelection() {
    if (!selected || (state.complete && RV.role !== "admin")) return false;
    if (selected.kind === "new") return true;
    if (selected.kind === "model") {
      const b = modelBox(selected.id);
      return Boolean(b && b.state === "needs_answer");
    }
    const a = addedBox(selected.id);
    return Boolean(a && (a.mine || RV.role === "admin"));
  }

  // ---- the frame canvas ------------------------------------------------------

  function drawBoxes() {
    const W = state.frame.width;
    const fontSize = Math.max(14, W / 70);
    const nodes = [];
    const label = (x, y, text) => {
      const t = RV.svg("text", { x: x + 2, y: Math.max(fontSize, y - 4), class: "rv-fb-label", "font-size": fontSize });
      t.textContent = text;
      return t;
    };
    const rect = (bb, cls) => RV.svg("rect", { x: bb[0], y: bb[1], width: bb[2] - bb[0], height: bb[3] - bb[1], class: cls });
    for (const b of state.boxes) {
      const isSel = selected && selected.kind === "model" && selected.id === b.event_id;
      const bb = isSel ? selected.bbox : b.bbox;
      nodes.push(rect(bb, "rv-fb " + (isSel ? "rv-fb-selected" : STATE_CLASS[b.state])));
      const mark = b.state === "locked" ? " · busy" : b.state.startsWith("answered") ? " ✓" : "";
      nodes.push(label(bb[0], bb[1], `${b.n}${mark}`));
    }
    for (const a of state.added) {
      const isSel = selected && selected.kind === "added" && selected.id === a.id;
      const bb = isSel ? selected.bbox : a.bbox;
      nodes.push(rect(bb, "rv-fb rv-fb-added" + (isSel ? " rv-fb-selected" : "")));
      nodes.push(label(bb[0], bb[1], "+ " + answerText(a.outcome, a.label, a.common)));
    }
    if (selected && selected.kind === "new") nodes.push(rect(selected.bbox, "rv-fb rv-fb-added rv-fb-selected"));
    if (editableSelection()) {
      const s = W / 150;
      for (const [hx, hy] of RV.handlesOf(selected.bbox)) {
        nodes.push(RV.svg("rect", { x: hx - s / 2, y: hy - s / 2, width: s, height: s, class: "rv-handle" }));
      }
    }
    svgEl.replaceChildren(...nodes);
  }

  function frameCanvas() {
    svgEl = RV.svg("svg", { class: "rv-editor-svg" + (drawing ? " drawing" : ""),
      viewBox: `0 0 ${state.frame.width} ${state.frame.height}`, preserveAspectRatio: "none" });
    svgEl.addEventListener("pointerdown", onDown);
    svgEl.addEventListener("pointermove", onMove);
    svgEl.addEventListener("pointerup", onUp);
    svgEl.addEventListener("pointercancel", onUp);
    drawBoxes();
    const wrap = RV.el("div", { class: "rv-editor rv-frame-canvas" },
      RV.el("img", { src: `${state.frame.url}?size=1600`, alt: "The whole frame", draggable: "false" }));
    wrap.append(svgEl);
    return wrap;
  }

  function toFrame(ev) {
    const r = svgEl.getBoundingClientRect();
    return [((ev.clientX - r.left) / r.width) * state.frame.width,
      ((ev.clientY - r.top) / r.height) * state.frame.height];
  }

  function hit(px, py) {
    for (const a of [...state.added].reverse()) {
      if (inside(a.bbox, px, py)) return { kind: "added", id: a.id, bbox: a.bbox.slice(), original: a.bbox.slice() };
    }
    for (const b of [...state.boxes].reverse()) {
      if (inside(b.bbox, px, py)) return { kind: "model", id: b.event_id, bbox: b.bbox.slice(), original: b.bbox.slice() };
    }
    return null;
  }

  function startDrag(ev, dragState) {
    ev.preventDefault();
    svgEl.setPointerCapture(ev.pointerId);
    drag = dragState;
  }

  function onDown(ev) {
    const [px, py] = toFrame(ev);
    if (drawing) {
      startDrag(ev, { mode: "draw", start: [px, py] });
      selected = { kind: "new", id: null, bbox: [Math.round(px), Math.round(py), Math.round(px) + 4, Math.round(py) + 4] };
      drawBoxes();
      return;
    }
    if (editableSelection()) {
      const s = state.frame.width / 150;
      for (const [hx, hy, name] of RV.handlesOf(selected.bbox)) {
        if (Math.abs(px - hx) <= s && Math.abs(py - hy) <= s) {
          startDrag(ev, { mode: name, start: [px, py], box: selected.bbox.slice() });
          return;
        }
      }
      if (inside(selected.bbox, px, py)) {
        startDrag(ev, { mode: "move", start: [px, py], box: selected.bbox.slice() });
        return;
      }
    }
    const target = hit(px, py);
    if (target) select(target);
  }

  function onMove(ev) {
    if (!drag) return;
    const [px, py] = toFrame(ev);
    const W = state.frame.width;
    const H = state.frame.height;
    if (drag.mode === "draw") {
      const [sx, sy] = drag.start;
      selected.bbox = [Math.round(RV.clamp(Math.min(sx, px), 0, W)), Math.round(RV.clamp(Math.min(sy, py), 0, H)),
        Math.round(RV.clamp(Math.max(sx, px), 0, W)), Math.round(RV.clamp(Math.max(sy, py), 0, H))];
    } else {
      selected.bbox = RV.dragBox(drag.box, drag.mode, px - drag.start[0], py - drag.start[1], W, H);
    }
    drawBoxes();
  }

  function onUp(ev) {
    if (!drag) return;
    const mode = drag.mode;
    drag = null;
    try { svgEl.releasePointerCapture(ev.pointerId); } catch (err) { /* already released */ }
    if (mode === "draw") {
      drawing = false;
      const [x1, y1, x2, y2] = selected.bbox;
      if (x2 - x1 < 8 || y2 - y1 < 8) {
        selected = null;
        RV.toast("That box was too small; drag a bigger one.");
      }
      render();
    } else if (selected.kind === "added") {
      saveAddedBox(null);          // moving your own drawn box saves it at once
    } else {
      renderPanel();
    }
  }

  // ---- the side panel ----------------------------------------------------------

  function pickerAndButtons(hints, onSpecies, onCant, onNone) {
    const out = [];
    const slot = RV.el("div", { class: "rv-picker" });
    picker = new RV.SpeciesPicker(slot, (entry) => {
      const source = hintName && entry.name === hintName ? "hint" : "search";
      hintName = null;
      onSpecies(entry, source);
    });
    out.push(RV.el("div", { class: "rv-label", text: "Pick the species" }), slot);
    if (hints && hints.length) {
      out.push(RV.el("div", { class: "rv-chips" }, hints.map((h) => RV.el("button", {
        type: "button", class: "rv-chip", onclick: () => {
          const entries = RV.catalogue.filter((e) => e.name === h.name);
          if (entries.length === 1) { onSpecies(entries[0], "hint"); return; }
          hintName = h.name;
          picker.input.value = h.name;
          picker.update();
          picker.focus();
        },
      }, `${h.common || h.name} ${RV.pct(h.p)}`))));
    }
    const row = [RV.el("button", { type: "button", onclick: onCant }, "Can't identify ", kbd("C"))];
    if (onNone) row.push(RV.el("button", { type: "button", onclick: onNone }, "No animal here ", kbd("N")));
    out.push(RV.el("div", { class: "rv-row" }, row));
    out.push(RV.el("label", { class: "rv-check" },
      RV.el("input", { type: "checkbox", checked: notSure, onchange: (ev) => { notSure = ev.target.checked; } }),
      " Not sure"));
    return out;
  }

  // The life-stage check (ask first, rather than round-trip the server's
  // "choose a life stage for <species>" 400) is shared with
  // review-single.js via RV.acceptGuessFlow (review-core.js) -- fix round
  // 1, Important finding: this used to reimplement it inline, identically
  // to review-single.js's own copy.
  function acceptGuess(b) {
    RV.acceptGuessFlow(b.model, "rv-frame-stage", (extra) => answerModel("accept", extra));
  }

  function modelPanel(b) {
    const out = [RV.el("div", { class: "rv-label", text: `Box ${b.n} · drawn by the model` }),
      RV.el("div", { class: "rv-thumb" }, RV.el("img", { src: b.zoom.url, alt: `Box ${b.n}, zoomed` }))];
    if (b.state === "answered") {
      out.push(RV.el("p", { text: `Answered: ${answerText(b.outcome, b.label, b.common)}${b.mine ? " (by you)" : ""}.` }));
      if (b.mine) {
        out.push(RV.el("button", { type: "button", onclick: () => {
          // Controller ruling 2 (Task 4): the one way out of a frame with no
          // guard on it -- pin it like every other request-sending action.
          if (busy || !state || RV.openFrameId !== state.saved_frame_id) return;
          if (RV.views.single && RV.views.single.isBusy && RV.views.single.isBusy()) return;   // Task 6 review: a reopen refused mid-save would strand the answered box
          RV.show("single", { reopenEventId: b.event_id });
        } }, "Change my answer"));
      }
      return out;
    }
    if (b.state === "answered_hidden") {
      out.push(RV.el("p", { text: "Answered. It is waiting for a second opinion, so the answer is hidden." }));
      return out;
    }
    if (b.state === "locked") {
      out.push(RV.el("p", { text: "Another volunteer is answering this box right now." }));
      return out;
    }
    const progress = RV.progressBar(b.counts_toward);
    if (progress) out.push(progress);
    if (b.model) {
      out.push(RV.el("div", { class: "rv-guess" },
        RV.el("div", { class: "rv-guess-text" },
          RV.el("div", { class: "rv-common", text: b.model.common || b.model.name }),
          RV.el("div", { class: "rv-latin", text: `${b.model.name} · model ${RV.pct(b.model.p)}` })),
        RV.el("button", { type: "button", class: "rv-primary", onclick: () => acceptGuess(b) },
          "✓ Looks right ", kbd("A")),
        RV.el("div", { class: "rv-stage", id: "rv-frame-stage" })));
    }
    out.push(...pickerAndButtons(b.hints,
      (entry, source) => answerModel("species", { label: entry.name, life_stage: entry.life_stage, label_source: source }),
      () => answerModel("unidentifiable", {}), () => answerModel("no_animal", {})));
    return out;
  }

  function addedPanel(a) {
    const out = [RV.el("div", { class: "rv-label", text: a ? `A box ${a.mine ? "you" : a.by} drew` : "The box you just drew" })];
    if (a) {
      out.push(RV.el("div", { class: "rv-thumb" }, RV.el("img", { src: a.zoom_url, alt: "The drawn box, zoomed" })),
        RV.el("p", { text: `Answer: ${answerText(a.outcome, a.label, a.common)}${a.not_sure ? " (not sure)" : ""}` }));
    }
    if ((state.complete && RV.role !== "admin") || (a && !a.mine && RV.role !== "admin")) return out;
    out.push(...pickerAndButtons(null,
      (entry) => saveAddedBox({ outcome: "species", label: entry.name, life_stage: entry.life_stage }),
      () => saveAddedBox({ outcome: "unidentifiable" }), null));
    out.push(a
      ? RV.el("button", { type: "button", class: "rv-link", onclick: removeAddedBox }, "Remove this box")
      : RV.el("button", { type: "button", class: "rv-link", onclick: () => { selected = null; render(); } }, "Discard"));
    return out;
  }

  function checklist() {
    const words = { needs_answer: "needs an answer", answered_hidden: "answered (second opinion pending)",
      locked: "another volunteer is on it" };
    const rows = state.boxes.map((b) => RV.el("li", {
      class: "rv-cl " + STATE_CLASS[b.state],
      onclick: () => select({ kind: "model", id: b.event_id, bbox: b.bbox.slice(), original: b.bbox.slice() }),
    }, `${b.n} · ${words[b.state] || answerText(b.outcome, b.label, b.common)}`));
    for (const a of state.added) {
      rows.push(RV.el("li", { class: "rv-cl rv-fb-added",
        onclick: () => select({ kind: "added", id: a.id, bbox: a.bbox.slice(), original: a.bbox.slice() }) },
      `+ drawn by ${a.mine ? "you" : a.by} · ${answerText(a.outcome, a.label, a.common)}`));
    }
    const done = state.boxes.length - state.blocking.length;
    return RV.el("div", {},
      RV.el("div", { class: "rv-label", text: `This frame · ${done} of ${state.boxes.length} model boxes answered` }),
      RV.el("ul", { class: "rv-checklist" }, rows));
  }

  function panelContent() {
    const out = [];
    if (state.complete) out.push(RV.el("p", { class: "rv-note", text: `Marked complete by ${state.complete.by}.` }));
    if (state.complete && RV.role === "admin") {
      out.push(RV.el("button", { type: "button", onclick: reopenFrame }, "Reopen frame for edits"));
    }
    if (!selected) {
      out.push(RV.el("p", { class: "rv-muted",
        text: "Click a box to answer it, or press D and drag to draw a box around an animal the model missed." }));
    } else if (selected.kind === "model") {
      out.push(...modelPanel(modelBox(selected.id)));
    } else {
      out.push(...addedPanel(selected.kind === "added" ? addedBox(selected.id) : null));
    }
    out.push(checklist());
    const blocked = state.blocking.length > 0;
    out.push(RV.el("button", { type: "button", class: "rv-primary", disabled: Boolean(state.complete) || blocked,
      onclick: complete },
    state.complete ? "Frame complete"
      : blocked ? `Mark frame complete (answer box ${state.blocking.join(", ")} first)` : "Mark frame complete"));
    return out;
  }

  function renderPanel() {
    const panel = document.getElementById("rv-frame-panel");
    if (panel) panel.replaceChildren(...panelContent());
  }

  function toggleDraw() {
    if (state.complete && RV.role !== "admin") return;
    drawing = !drawing;
    if (drawing) selected = null;
    render();
  }

  // The instructions under the frame: a call-out whose swatches use the same
  // colours and line styles as the boxes drawn on the frame (review.css).
  function legend() {
    const swatch = (state, label) => RV.el("span", { class: "rv-legend-item" },
      RV.el("span", { class: `rv-swatch rv-swatch-${state}`, "aria-hidden": "true" }), label);
    return RV.el("div", { class: "rv-callout", role: "note" },
      RV.el("strong", { text: "Click a box to answer it." }),
      swatch("pending", "needs an answer"),
      swatch("done", "answered"),
      swatch("added", "drawn by a volunteer"),
      swatch("locked", "another volunteer has it"),
      RV.el("span", { class: "rv-legend-keys" },
        kbd("S"), " next frame · ", kbd("D"), " draw a box · ", kbd("F"), " single boxes"));
  }

  function render() {
    root().replaceChildren(
      RV.el("div", { class: "rv-frame-top" },
        RV.el("button", { type: "button", class: "rv-link", onclick: toSingle },
          "Single boxes ", kbd("F")),
        RV.el("button", { type: "button", onclick: nextFrame }, "Next frame ", kbd("S")),
        RV.el("span", { class: "rv-muted", text: `${state.source_name} · ${RV.fmtTime(state.ts)}` }),
        RV.el("button", { type: "button", class: drawing ? "on" : "",
          disabled: Boolean(state.complete) && RV.role !== "admin", onclick: toggleDraw },
          drawing ? "Drawing: drag on the frame (Esc to cancel) " : "Draw a box ", kbd("D"))),
      RV.el("div", { class: "rv-layout rv-layout-frame" },
        RV.el("div", {}, frameCanvas(), legend()),
        RV.el("aside", { class: "rv-side", id: "rv-frame-panel" }, ...panelContent())));
  }

  // ---- saving ------------------------------------------------------------------

  async function answerModel(outcome, extra) {
    // Fix round 2, Important 1: a lane/Browse click while the frame view
    // is idle nulls RV.openFrameId synchronously, but the stale frame's
    // DOM stays up and keeps taking keys until the queue's response
    // switches the view. Without this, an answer started in that window
    // would set busy and block the new frame's enter() from rendering the
    // box it just claimed -- held, but never shown.
    if (!state || RV.openFrameId !== state.saved_frame_id) return;
    if (busy) return;
    busy = true;
    try {
      const b = modelBox(selected.id);
      const body = { event_id: b.event_id, review_item_id: b.review_item_id, outcome, ...extra };
      if (outcome === "accept" || outcome === "species") body.not_sure = notSure;
      if (outcome !== "no_animal" && selected.bbox.some((v, i) => v !== b.bbox[i])) body.bbox = selected.bbox;
      const r = await RV.post("/api/review/verdicts", body);
      if (!r.ok) {
        RV.toast(r.data.error || "That answer wasn't saved.");
        if (r.data.error === "pick a species from the list") await RV.loadCatalogue();
        if (r.status === 409 || r.status === 403) await reload(false);
        return;
      }
      await RV.refreshMine();
      await reload(false);
    } finally {
      busy = false;
    }
  }

  async function saveAddedBox(answer) {
    // Fix round 2, Important 1: see answerModel's comment above.
    if (!state || RV.openFrameId !== state.saved_frame_id) return;
    if (busy) return;
    busy = true;
    try {
      const a = selected.kind === "added" ? addedBox(selected.id) : null;
      const payload = { bbox: selected.bbox };
      if (a) {
        Object.assign(payload, { outcome: a.outcome, label: a.label, life_stage: a.life_stage,
          not_sure: a.not_sure, celebrity_id: a.celebrity_id });
      }
      if (answer) Object.assign(payload, answer, { not_sure: notSure && answer.outcome === "species" });
      if (!payload.outcome) { renderPanel(); return; }         // a new box waits for its answer
      if (payload.outcome !== "species") Object.assign(payload, { label: null, life_stage: null, not_sure: false });
      const url = `/api/review/frames/${state.saved_frame_id}/boxes`;
      const r = a ? await RV.patch(`${url}/${a.id}`, payload) : await RV.post(url, payload);
      if (!r.ok) { RV.toast(r.data.error || "That box wasn't saved."); await reload(true); return; }
      selected = null;
      await reload(false);
    } finally {
      busy = false;
    }
  }

  async function removeAddedBox() {
    // Fix round 2, Important 1: see answerModel's comment above.
    if (!state || RV.openFrameId !== state.saved_frame_id) return;
    if (busy) return;
    busy = true;
    try {
      const r = await RV.del(`/api/review/frames/${state.saved_frame_id}/boxes/${selected.id}`);
      if (!r.ok) { RV.toast(r.data.error || "Couldn't remove that box."); return; }
      selected = null;
      await reload(false);
    } finally {
      busy = false;
    }
  }

  // "Next frame" (spec amendment 2026-09-25, flow step 3): every box here
  // this volunteer hasn't answered is skipped for them -- the queue won't
  // bring them back -- and freed for everyone else at once.
  async function nextFrame() {
    // Controller ruling 1 (Task 4): the same stale-frame leave guard as
    // answerModel/saveAddedBox/removeAddedBox/complete/toSingle above --
    // without it, pressing S during a lane switch could re-claim a frame
    // already released, recreating the claim leak fix round 2 closed.
    if (!state || RV.openFrameId !== state.saved_frame_id) return;
    if (busy) return;
    busy = true;
    let left = false;
    try {
      const r = await RV.post(`/api/review/frames/${state.saved_frame_id}/skip`);
      if (!r.ok) { RV.toast(r.data.error || "Couldn't leave this frame."); return; }
      RV.openFrameId = null;     // the skip already released this frame's boxes
      left = true;
    } finally {
      busy = false;
    }
    // Outside the busy guard: RV.next() re-enters this view.
    if (left) await RV.next();
  }

  async function complete() {
    // Fix round 2, Important 1: see answerModel's comment above.
    if (!state || RV.openFrameId !== state.saved_frame_id) return;
    if (busy) return;
    busy = true;
    let done = false;
    try {
      const r = await RV.post(`/api/review/frames/${state.saved_frame_id}/complete`);
      if (!r.ok) { RV.toast(r.data.error || "The frame isn't complete yet."); await reload(true); return; }
      RV.toast("Frame marked complete. Thank you!");
      // Final review M2: release, don't just forget. This is a no-op
      // server-side once every box already has an answer -- true whether
      // it came from this page or, since follow-up A1, the older
      // /api/corrections form, which also closes the pool item it
      // answers -- but it's still correct if any claim remains. Awaited,
      // like toSingle()'s, so the queue call below can't have /next hand
      // that claim straight back as "held" (Review Focus 3).
      // releaseFrame() nulls RV.openFrameId before it sends anything, so
      // a second click on "Mark frame complete" is still refused by the
      // guard above.
      await RV.releaseFrame();
      done = true;
    } finally {
      busy = false;
    }
    // Outside the busy guard: RV.next() may re-enter this view, and
    // enter() refuses while busy.
    if (done) await RV.next();
  }

  // Follow-up B6: an admin takes the frame's completion back, so volunteers
  // can change it and mark it complete again.
  async function reopenFrame() {
    if (!state || RV.openFrameId !== state.saved_frame_id) return;
    if (busy) return;
    busy = true;
    try {
      const r = await RV.del(`/api/review/frames/${state.saved_frame_id}/complete`);
      if (!r.ok) { RV.toast(r.data.error || "Couldn't reopen the frame."); return; }
      RV.toast("Frame reopened for edits.");
      await reload(true);
    } finally {
      busy = false;
    }
  }

  RV.onKey("frame", (key) => {
    if (!state) return false;
    const k = key.length === 1 ? key.toLowerCase() : key;
    if (k === "f") { toSingle(); return true; }
    if (k === "d") { toggleDraw(); return true; }
    if (k === "s") { nextFrame(); return true; }
    if (k === "Escape") {
      if (drawing) { drawing = false; render(); } else if (selected && selected.original) {
        selected.bbox = selected.original.slice();
        render();
      }
      return true;
    }
    if (k === "/" && picker) { picker.focus(); return true; }
    if (!selected) return false;
    if (selected.kind === "model") {
      const b = modelBox(selected.id);
      if (!b || b.state !== "needs_answer") return false;
      if (k === "a" && b.model) { acceptGuess(b); return true; }
      if (k === "c") { answerModel("unidentifiable", {}); return true; }
      if (k === "n") { answerModel("no_animal", {}); return true; }
    } else if (k === "c") {
      saveAddedBox({ outcome: "unidentifiable" });
      return true;
    }
    return false;
  });

  // Fix round 1, Important 1(b): so the nav click handler (review-core.js)
  // can refuse to leave the frame view while an answer is still in flight,
  // rather than race a late reload() into re-claiming the frame it left.
  return { enter, isBusy: () => busy };
})();
