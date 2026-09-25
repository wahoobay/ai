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
// the view releases any single-box item the volunteer was still holding
// (RV.releaseHeld), since claiming the frame's boxes supersedes it; and
// "Looks right" asks for a life stage first when the catalogue lists the
// model's guess only under specific stages, exactly as review-single.js's
// acceptGuess() does, rather than round-tripping the server's 400 "choose
// a life stage for <species>".
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
    if (!id) { RV.show("single"); return; }
    if (busy) return;
    busy = true;
    try {
      // Claiming this frame's boxes supersedes whatever single-box item
      // the volunteer was still holding when they clicked "Whole frame".
      RV.releaseHeld();
      const r = await RV.post(`/api/review/frames/${id}/open`);
      if (!r.ok) { RV.toast(r.data.error || "Couldn't open that frame."); RV.show("single"); return; }
      state = r.data;
      drawing = false;
      selectFirst();
      render();
    } finally {
      busy = false;
    }
  }

  async function reload(keepSelection) {
    const r = await RV.get(`/api/review/frames/${state.saved_frame_id}`);
    if (!r.ok) { RV.toast(r.data.error || "Couldn't refresh the frame."); return; }
    state = r.data;
    if (!keepSelection) selectFirst();
    render();
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

  function editableSelection() {
    if (!selected || state.complete) return false;
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

  // "Looks right" sends no life_stage. If the catalogue lists the box's
  // guess only under specific life stages (no plain, stage-less entry),
  // the server rejects that with "choose a life stage for <species>";
  // ask first instead of round-tripping the 400 (same rule review-single
  // .js's acceptGuess() applies to the single-box view).
  function acceptGuess(b) {
    const name = b.model.name;
    const plain = RV.entry(name, null);
    if (plain && !plain.life_stage) { answerModel("accept", {}); return; }
    const stages = RV.catalogue.filter((e) => e.name === name);
    if (stages.length < 2) {
      answerModel("accept", { life_stage: stages.length ? stages[0].life_stage : null });
      return;
    }
    const who = b.model.common || name;
    document.getElementById("rv-frame-stage").replaceChildren(
      RV.el("span", { class: "rv-label", text: `Which life stage of ${who}?` }),
      RV.el("div", { class: "rv-chips" }, stages.map((s) => RV.el("button", {
        type: "button", class: "rv-chip", onclick: () => answerModel("accept", { life_stage: s.life_stage }),
      }, s.life_stage || who))));
  }

  function modelPanel(b) {
    const out = [RV.el("div", { class: "rv-label", text: `Box ${b.n} · drawn by the model` }),
      RV.el("div", { class: "rv-thumb" }, RV.el("img", { src: b.zoom.url, alt: `Box ${b.n}, zoomed` }))];
    if (b.state === "answered") {
      out.push(RV.el("p", { text: `Answered: ${answerText(b.outcome, b.label, b.common)}${b.mine ? " (by you)" : ""}.` }));
      if (b.mine) {
        out.push(RV.el("button", { type: "button", onclick: () => RV.show("single", { reopenEventId: b.event_id }) },
          "Change my answer"));
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
    if (state.complete || (a && !a.mine && RV.role !== "admin")) return out;
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
      : blocked ? `Mark frame complete (answer ${state.blocking.join(", ")} first)` : "Mark frame complete"));
    return out;
  }

  function renderPanel() {
    const panel = document.getElementById("rv-frame-panel");
    if (panel) panel.replaceChildren(...panelContent());
  }

  function toggleDraw() {
    if (state.complete) return;
    drawing = !drawing;
    if (drawing) selected = null;
    render();
  }

  function render() {
    root().replaceChildren(
      RV.el("div", { class: "rv-frame-top" },
        RV.el("button", { type: "button", class: "rv-link", onclick: () => RV.show("single") },
          "← Back to single boxes ", kbd("F")),
        RV.el("span", { class: "rv-muted", text: `${state.source_name} · ${new Date(state.ts).toLocaleString()}` }),
        RV.el("button", { type: "button", class: drawing ? "on" : "", disabled: Boolean(state.complete), onclick: toggleDraw },
          drawing ? "Drawing: drag on the frame (Esc to cancel) " : "Draw a box ", kbd("D"))),
      RV.el("div", { class: "rv-layout rv-layout-frame" },
        RV.el("div", {}, frameCanvas(), RV.el("p", { class: "rv-cap",
          text: "Click a box to answer it. Yellow dashed: needs an answer. Green: answered. Blue: drawn by a volunteer." })),
        RV.el("aside", { class: "rv-side", id: "rv-frame-panel" }, ...panelContent())));
  }

  // ---- saving ------------------------------------------------------------------

  async function answerModel(outcome, extra) {
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

  async function complete() {
    if (busy) return;
    busy = true;
    try {
      const r = await RV.post(`/api/review/frames/${state.saved_frame_id}/complete`);
      if (!r.ok) { RV.toast(r.data.error || "The frame isn't complete yet."); await reload(true); return; }
      RV.toast("Frame marked complete. Thank you!");
      RV.show("single");
    } finally {
      busy = false;
    }
  }

  RV.onKey("frame", (key) => {
    if (!state) return false;
    const k = key.length === 1 ? key.toLowerCase() : key;
    if (k === "f") { RV.show("single"); return true; }
    if (k === "d") { toggleDraw(); return true; }
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

  return { enter };
})();
