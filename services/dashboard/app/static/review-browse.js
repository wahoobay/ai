// services/dashboard/app/static/review-browse.js
// Browse: headline counts, filters, a grid of boxes with their status and
// species progress; for administrators, disputes, agreement, the date
// and pool status.
//
// Adapted from the phase-1 brief to the server's real behaviour (Tasks
// 9's review and this task's own reading of routes_review.py /
// review_browse.py): GET /api/review/progress can flag its numbers
// `stale` (the pool's own refresh timed out) rather than fail, so that
// is shown as a note, not an error; progress and browse can both answer
// 503 under load, so a failed progress load offers a retry instead of
// leaving the view dead; and the two mutating admin actions (settling a
// dispute) get the same busy guard,
// reset in `finally`, that submit()/skip()/reopen() and
// answerModel()/saveAddedBox()/removeAddedBox()/complete() use in the
// single-box and whole-frame views, so a double click can't fire two
// overlapping requests for the same box.
//
// 2026-11-21 (reviewer species suggestions): the administrator's panels
// start with "New species", where suggested species are approved or
// rejected; its Approve, Reject and per-box "Not this species" share that
// same busy guard.
"use strict";

RV.views.browse = (() => {
  const root = () => document.getElementById("rv-browse");
  const filters = { status: "any", species: "", camera: "", who: "anyone", lane: "any", date_from: "", date_to: "" };
  let page = 0;
  let progress = null;

  // awaiting_species: the box's answer names a species waiting for an admin's approval, so it
  // is not verified yet (final review I1).
  const BADGES = { single: ["✓ verified", "ok"], agreed: ["✓✓ agreed", "two"], adjudicated: ["settled", "two"],
    disputed: ["disputed", "disp"], in_queue: ["in queue", "open"],
    awaiting_second_opinion: ["awaiting 2nd opinion", "open"],
    awaiting_species: ["waiting for approval", "open"] };

  function answerText(side) {
    if (side.outcome === "no_animal") return "no animal";
    if (side.outcome === "unidentifiable") return "can't identify";
    return side.common || side.label || "?";
  }

  async function enter() {
    root().replaceChildren(RV.el("p", { class: "rv-empty", text: "Loading…" }));
    const r = await RV.get("/api/review/progress");
    if (!r.ok) {
      root().replaceChildren(RV.el("div", { class: "rv-empty" },
        RV.el("p", { text: r.data.error || "Couldn't load progress." }),
        RV.el("button", { type: "button", onclick: () => enter() }, "Try again")));
      return;
    }
    progress = r.data;
    page = 0;
    render();
    await loadGrid();
    if (RV.role === "admin") await loadAdmin();
  }

  const kpi = (value, label) => RV.el("div", { class: "rv-kpi" },
    RV.el("b", { text: String(value) }), RV.el("span", { class: "rv-muted", text: label }));

  function render() {
    const k = progress.kpis;
    // The nulls are left out: replaceChildren() would show each as the word "null"
    // (browser walk 2026-11-21).
    root().replaceChildren(...[
      RV.el("div", { class: "rv-kpis" },
        kpi(k.verified_boxes, "verified boxes"), kpi(k.species_with_verified, "species with verified boxes"),
        kpi(k.complete_frames, "complete frames"), kpi(k.session_verified, "answered this session"),
        kpi(k.open.check + k.open.identify, "open in the queue")),
      progress.stale
        ? RV.el("p", { class: "rv-muted rv-small", text: "These numbers may be a few minutes out of date." })
        : null,
      filterBar(),
      RV.el("div", { class: "rv-browse-body" },
        RV.el("div", {}, RV.el("div", { id: "rv-grid", class: "rv-grid" }), RV.el("div", { id: "rv-pager", class: "rv-row rv-small" })),
        speciesList()),
      RV.role === "admin" ? RV.el("section", { id: "rv-admin", class: "rv-admin" }) : null,
    ].filter(Boolean));
  }

  function select(name, options, value, onchange) {
    return RV.el("label", { class: "rv-filter" }, `${name} `,
      RV.el("select", { onchange: (ev) => onchange(ev.target.value) },
        options.map(([v, text]) => RV.el("option", { value: v, selected: v === value }, text))));
  }

  function filterBar() {
    const set = (key) => (value) => { filters[key] = value; page = 0; loadGrid(); };
    const species = RV.el("input", { type: "search", class: "rv-search rv-search-small", id: "rv-species-filter",
      placeholder: "Species (Latin name)", value: filters.species, list: "rv-species-names" });
    species.addEventListener("change", () => set("species")(species.value.trim()));
    // Listed species only: an admin's catalogue also holds every species waiting for approval.
    const names = RV.el("datalist", { id: "rv-species-names" },
      [...new Set(RV.catalogue.filter((e) => !e.pending).map((e) => e.name))]
        .map((n) => RV.el("option", { value: n })));
    const date = (key, label) => {
      const input = RV.el("input", { type: "date", value: filters[key] });
      input.addEventListener("change", () => set(key)(input.value));
      return RV.el("label", { class: "rv-filter" }, `${label} `, input);
    };
    return RV.el("div", { class: "rv-filters" },
      select("Status", [["any", "Any"], ["verified", "Verified"], ["agreed", "Agreed on spot-check"],
        ["adjudicated", "Settled by an admin"], ["disputed", "Disputed"], ["unidentifiable", "Can't identify"],
        ["no_animal", "No animal"], ["in_queue", "In the queue"]], filters.status, set("status")),
      species, names,
      select("Camera", [["", "Both cameras"], ["seahivecam", "SEAHIVECAM"], ["pier_cam", "Pier"]], filters.camera, set("camera")),
      select("Who", [["anyone", "Anyone"], ["me", "Me"], ["session", "This session"]], filters.who, set("who")),
      select("Lane", [["any", "Both lanes"], ["check", "Model's guesses"], ["identify", "New fish"]], filters.lane, set("lane")),
      date("date_from", "From"), date("date_to", "To"));
  }

  function cell(c) {
    let badge = BADGES[c.status] || [c.status, "open"];
    if (c.status !== "disputed" && c.outcome === "unidentifiable") badge = ["can't identify", "cant"];
    if (c.status !== "disputed" && c.outcome === "no_animal") badge = ["no animal", "cant"];
    const name = c.label ? (c.common || c.label) : c.status === "in_queue" ? "" : "—";
    const who = c.reviewers.length ? ` · ${c.reviewers.join(", ")}` : "";
    return RV.el("button", { type: "button", class: "rv-cell", title: `${c.camera} · ${RV.fmtTime(c.ts)}${who}`,
      onclick: () => open(c) },
    RV.el("img", { src: c.zoom_url, alt: "", loading: "lazy" }),
    RV.el("span", { class: "rv-cell-label" }, RV.el("span", { text: name }),
      RV.el("span", { class: `rv-badge rv-b-${badge[1]}`, text: badge[0] })));
  }

  function open(c) {
    // Follow-up B2: a single-box answer or skip, or a frame action, is still
    // saving. Opening a box now would supersede its trailing next and leave
    // "Loading the next box…" on screen.
    for (const v of ["single", "frame"]) if (RV.views[v] && RV.views[v].isBusy && RV.views[v].isBusy()) return;
    if (c.kind === "box" && c.mine) { RV.show("single", { reopenEventId: c.event_id }); return; }
    if (c.saved_frame_id && RV.views.frame) { RV.show("frame", { savedFrameId: c.saved_frame_id }); return; }
    RV.toast("Only your own answers can be changed.");
  }

  async function loadGrid() {
    const q = new URLSearchParams({ status: filters.status, who: filters.who, lane: filters.lane, page: String(page) });
    for (const key of ["species", "camera", "date_from", "date_to"]) if (filters[key]) q.set(key, filters[key]);
    const r = await RV.get(`/api/review/browse?${q}`);
    const grid = document.getElementById("rv-grid");
    const pager = document.getElementById("rv-pager");
    if (!grid) return;
    if (!r.ok) { grid.replaceChildren(RV.el("p", { text: r.data.error || "Couldn't load boxes." })); return; }
    const cells = r.data.cells || [];
    grid.replaceChildren(...(cells.length ? cells.map(cell)
      : [RV.el("p", { class: "rv-muted", text: "Nothing matches these filters." })]));
    pager.replaceChildren(
      RV.el("button", { type: "button", disabled: page === 0, onclick: () => { page -= 1; loadGrid(); } }, "← Newer"),
      RV.el("span", { class: "rv-muted", text: `Page ${page + 1}` }),
      RV.el("button", { type: "button", disabled: !r.data.has_more, onclick: () => { page += 1; loadGrid(); } }, "Older →"));
  }

  function speciesList() {
    const rows = progress.species.slice(0, 60).map((s) => {
      const base = Math.min(100, (100 * s.baseline) / s.target);
      const ver = Math.min(100 - base, (100 * s.verified) / s.target);
      return RV.el("button", { type: "button", class: "rv-sp", onclick: () => {
        filters.species = s.name;
        const input = document.getElementById("rv-species-filter");
        if (input) input.value = s.name;
        page = 0;
        loadGrid();
      } },
      RV.el("span", {}, RV.el("span", { text: s.common || s.name }), " ", RV.el("span", { class: "rv-latin", text: s.name })),
      RV.el("span", { class: "rv-bar", "aria-hidden": "true" },
        RV.el("i", { class: "rv-bar-base", style: `width:${base}%` }), RV.el("i", { class: "rv-bar-ver", style: `width:${ver}%` })),
      RV.el("span", { class: "rv-muted", text: `${s.baseline} + ${s.verified}` }));
    });
    return RV.el("aside", { class: "rv-species" },
      RV.el("div", { class: "rv-label", text: `Species progress · thinnest first · target ${progress.target}` }),
      ...rows,
      RV.el("p", { class: "rv-muted rv-small",
        text: "Grey: crops already in the training set. Green: verified here. Click a species to filter the grid." }));
  }

  // ---- administrator panels ----------------------------------------------------

  async function loadAdmin() {
    const section = document.getElementById("rv-admin");
    if (!section) return;
    const [pending, decided, disputes, agreement, status] = await Promise.all([
      RV.get("/api/review/species-suggestions?status=pending"),
      RV.get("/api/review/species-suggestions?status=decided"),
      RV.get("/api/review/disputes"), RV.get("/api/review/agreement"), RV.get("/api/review/status")]);
    if (pending.ok) RV.setSpeciesBadge(pending.data.pending);
    section.replaceChildren(
      RV.el("h3", { text: "Administrator" }),
      speciesPanel(pending.ok ? pending.data.suggestions : null, decided.ok ? decided.data.suggestions : []),
      disputesPanel(disputes.ok ? disputes.data.disputes : []),
      agreementPanel(agreement.ok ? agreement.data : null),
      statusPanel(status.ok ? status.data : null));
  }

  // Shared by disputesPanel()'s "Use answer" / "Relabel…" buttons and
  // (the browse view's mutating
  // admin actions (spec parity with submit()/skip()/reopen() in
  // review-single.js and answerModel()/saveAddedBox()/removeAddedBox()/
  // complete() in review-frame.js), so a double click can't fire two
  // overlapping POST/PUT requests. loadAdmin() itself stays unguarded --
  // like review-frame.js's reload() -- since it's a plain re-fetch these
  // actions call once they're done, not something a click fires directly.
  // Since 2026-11-21 speciesPanel()'s approveSuggestion(), rejectSuggestion()
  // and detachBox() share it too.
  let busy = false;

  async function adjudicate(eventId, body) {
    if (busy) return;
    busy = true;
    try {
      const r = await RV.post(`/api/review/disputes/${eventId}/adjudicate`, body);
      if (!r.ok) { RV.toast(r.data.error || "That wasn't saved."); return; }
      RV.toast("Settled.");
      await loadAdmin();
    } finally {
      busy = false;
    }
  }

  // ---- new species (spec 2026-11-21, "Admin panel") ----
  // Every suggested species waiting for a decision, with the boxes answered with it -- their
  // only check, since such answers get no spot-check -- each with "Not this species" (that
  // answer becomes can't-identify and leaves the suggestion). Approve adds it to the list
  // under the admin's names and stage; Reject says what it really is (a listed species, never
  // one still waiting) or that it can't be identified. Either rewrites its answers and
  // reloads the species list. Decided ones fold into a short history.

  const BOXES_PER_PAGE = 24;   // the server's page size
  const stageText = (stage) => (stage ? ` · ${stage}` : "");
  const answersText = (n) => `${n} answer${n === 1 ? "" : "s"}`;
  const formField = (label, control) => RV.el("label", { class: "rv-suggest-field" },
    RV.el("span", { class: "rv-label", text: label }), control);

  // A 409 from Approve, Reject or "Not this species": another admin decided the suggestion
  // first (or, on Approve, listed the same choice), so its card is stale. The lists are
  // reloaded to replace it, and the reason goes to a toast, since the card's own alert line
  // goes with it (final review).
  async function reloadStale(r) {
    RV.toast(r.data.error || "That suggestion was already decided.");
    await loadAdmin();
  }

  async function approveSuggestion(id, body, say) {
    if (busy) return;
    busy = true;
    try {
      const r = await RV.post(`/api/review/species-suggestions/${id}/approve`, body);
      if (r.status === 409) { await reloadStale(r); return; }
      if (!r.ok) { say(r.data.error || "That wasn't saved."); return; }
      RV.toast(`Added ${body.common_name} to the species list.`);
      await RV.loadCatalogue();
      await Promise.all([loadAdmin(), loadGrid()]);
    } finally {
      busy = false;
    }
  }

  async function rejectSuggestion(id, body, say) {
    if (busy) return;
    busy = true;
    try {
      const r = await RV.post(`/api/review/species-suggestions/${id}/reject`, body);
      if (r.status === 409) { await reloadStale(r); return; }
      if (!r.ok) { say(r.data.error || "That wasn't saved."); return; }
      RV.toast(`Not added; its answers now say ${body.unidentifiable
        ? "can't identify" : RV.listedName(body.as_label, body.as_life_stage)}.`);
      await RV.loadCatalogue();
      await Promise.all([loadAdmin(), loadGrid()]);
    } finally {
      busy = false;
    }
  }

  // "Not this species" on one box; `after(answers)` shows the suggestion without it.
  async function detachBox(id, eventId, after) {
    if (busy) return;
    busy = true;
    try {
      const r = await RV.post(`/api/review/species-suggestions/${id}/detach`, { event_id: eventId });
      if (r.status === 409) { await reloadStale(r); return; }
      if (!r.ok) { RV.toast(r.data.error || "That wasn't saved."); return; }
      RV.toast("That box's answer now says can't identify.");
      await Promise.all([after(r.data.answers), loadGrid()]);
    } finally {
      busy = false;
    }
  }

  function speciesPanel(pending, decided) {
    const panel = RV.el("div", { class: "rv-panel rv-species-panel" });
    if (!pending) {
      panel.append(RV.el("h4", { text: "New species" }), RV.el("p", { text: "Unavailable." }));
      return panel;
    }
    panel.append(RV.el("h4", { text: `New species (${pending.length} waiting)` }),
      ...(pending.length ? pending.map(suggestionCard)
        : [RV.el("p", { class: "rv-muted", text: "Nothing waiting." })]));
    if (decided.length) {
      panel.append(RV.el("details", { class: "rv-sg-history" },
        RV.el("summary", { text: `Decided (${decided.length})` }),
        RV.el("ul", {}, decided.map(decidedItem))));
    }
    return panel;
  }

  function suggestionCard(s) {
    const who = s.suggested_by_name || "the volunteer";
    const count = RV.el("span", { text: answersText(s.answers) });
    const error = RV.el("p", { class: "rv-suggest-error", role: "alert", hidden: true });
    const say = (message) => { error.textContent = message || ""; error.hidden = !message; };
    const value = (control) => (control.value || "").trim() || null;

    // The boxes answered with it, a page at a time; only the latest page asked for is shown.
    const boxes = RV.el("div", { class: "rv-sg-boxes" }, RV.el("p", { class: "rv-muted", text: "Loading…" }));
    const pager = RV.el("div", { class: "rv-row rv-small" });
    let page = 0;
    let asked = 0;
    async function showBoxes() {
      const mine = ++asked;
      const r = await RV.get(`/api/review/species-suggestions/${s.id}/boxes?page=${page}`);
      if (mine !== asked) return;
      if (!r.ok) {
        boxes.replaceChildren(RV.el("p", { text: r.data.error || "Couldn't load its boxes." }));
        pager.replaceChildren();
        return;
      }
      const { total, boxes: shown } = r.data;
      if (!shown.length && page > 0) { page -= 1; await showBoxes(); return; }
      boxes.replaceChildren(...(shown.length ? shown.map((b) => RV.el("figure", { class: "rv-sg-box" },
        RV.el("img", { src: b.zoom_url, alt: "", loading: "lazy" }),
        RV.el("figcaption", { class: "rv-muted", text: b.by }),
        RV.el("button", { type: "button", onclick: () => detachBox(s.id, b.event_id, afterDetach) },
          "Not this species")))
        : [RV.el("p", { class: "rv-muted", text: "No answers use it now." })]));
      const from = page * BOXES_PER_PAGE;
      pager.replaceChildren(...(total > BOXES_PER_PAGE ? [
        RV.el("button", { type: "button", disabled: page === 0, onclick: () => { page -= 1; showBoxes(); } },
          "← Previous"),
        RV.el("span", { class: "rv-muted", text: `boxes ${from + 1}–${from + shown.length} of ${total}` }),
        RV.el("button", { type: "button", disabled: from + BOXES_PER_PAGE >= total,
          onclick: () => { page += 1; showBoxes(); } }, "Next →"),
      ] : []));
    }
    async function afterDetach(answers) {
      s.answers -= answers;
      count.textContent = answersText(s.answers);
      await showBoxes();
    }

    // Approve: the names and stage as the admin corrects them, the scientific name required.
    const textField = (name, prefill, attrs) => {
      const input = RV.el("input", { type: "text", name, class: "rv-field", autocomplete: "off",
        maxlength: "120", ...attrs });
      input.value = prefill || "";
      return input;
    };
    const scientific = textField("scientific_name", s.scientific_name, { placeholder: "Genus species" });
    const common = textField("common_name", s.common_name);
    const stage = RV.el("select", { name: "life_stage", class: "rv-field" },
      RV.el("option", { value: "", text: "none" }),
      RV.STAGES.map((st) => RV.el("option", { value: st, text: st })));
    stage.value = s.life_stage || "";
    const note = RV.el("textarea", { name: "note", class: "rv-field", rows: "2", maxlength: "500",
      placeholder: `${who} sees it with your decision` });
    const approve = () => {
      const body = { scientific_name: value(scientific), common_name: value(common),
        life_stage: value(stage), note: value(note) };
      if (!body.scientific_name) { say("write the scientific name as Genus species"); scientific.focus(); return; }
      if (!body.common_name) { say("write the common name"); common.focus(); return; }
      approveSuggestion(s.id, body, say);
    };

    // Reject: "It's actually…" a listed species (the picker leaves out the ones still
    // waiting, which a rejection can't name), or "Can't identify".
    const radio = (value) => RV.el("input", { type: "radio", name: `reject-${s.id}`, value,
      onchange: () => say(null) });
    const asIt = radio("as");
    const cant = radio("cant");
    const chosen = RV.el("span", { class: "rv-sg-chosen" });
    const slot = RV.el("div", { class: "rv-picker" });
    let actually = null;
    new RV.SpeciesPicker(slot, (e) => {
      actually = e;
      asIt.checked = true;
      say(null);
      chosen.textContent = (e.common || e.name) + (e.life_stage ? ` (${e.life_stage})` : "");
    });
    const reject = () => {
      if (!asIt.checked && !cant.checked) { say("choose “It's actually…” or “Can't identify”"); return; }
      if (asIt.checked && !actually) { say("pick the species it actually is"); return; }
      rejectSuggestion(s.id, asIt.checked
        ? { as_label: actually.name, as_life_stage: actually.life_stage, unidentifiable: false, note: value(note) }
        : { as_label: null, as_life_stage: null, unidentifiable: true, note: value(note) }, say);
    };

    showBoxes();
    return RV.el("div", { class: "rv-sg" },
      RV.el("div", {},
        RV.el("span", { class: "rv-common", text: s.common_name }), " ",
        RV.el("span", { class: "rv-latin", text: (s.scientific_name || "no scientific name") + stageText(s.life_stage) })),
      s.note ? RV.el("p", { class: "rv-note", text: s.note }) : null,
      RV.el("p", { class: "rv-muted rv-small" }, `Suggested by ${who} · ${RV.fmtTime(s.created_at)} · `, count),
      boxes, pager,
      formField(`Note to ${who} (optional)`, note),
      RV.el("div", { class: "rv-sg-decide" },
        RV.el("div", { class: "rv-sg-fields" },
          RV.el("span", { class: "rv-label", text: "Approve as" }),
          formField("Scientific name", scientific), formField("Common name", common),
          formField("Life stage or phase", stage),
          RV.el("button", { type: "button", class: "rv-primary", onclick: approve }, "Approve")),
        RV.el("div", { class: "rv-sg-fields" },
          RV.el("span", { class: "rv-label", text: "Or reject" }),
          RV.el("label", { class: "rv-check" }, asIt, "It's actually…", chosen), slot,
          RV.el("label", { class: "rv-check" }, cant, "Can't identify"),
          RV.el("button", { type: "button", onclick: reject }, "Reject"))),
      error);
  }

  function decidedItem(s) {
    const suggested = s.life_stage ? `${s.common_name} (${s.life_stage})` : s.common_name;
    const what = s.status === "approved"
      ? `added as ${s.approved_common} (${s.approved_name}${stageText(s.approved_stage)})`
      : `not added; its answers now say ${s.rejected_to_name
        ? RV.listedName(s.rejected_to_name, s.rejected_to_stage) : "can't identify"}`;
    return RV.el("li", {},
      RV.el("span", { class: "rv-common", text: suggested }), ` · ${what}`,
      RV.el("div", { class: "rv-muted rv-small",
        text: `Suggested by ${s.suggested_by_name || "a volunteer"} · decided ${RV.fmtTime(s.decided_at)}` }),
      s.decision_note ? RV.el("div", { class: "rv-muted", text: s.decision_note }) : null);
  }

  function disputesPanel(list) {
    const body = list.length ? list.map((d) => {
      const use = (side) => adjudicate(d.event_id, { outcome: side.outcome, label: side.label, life_stage: side.life_stage });
      const slot = RV.el("div", { class: "rv-picker" });
      return RV.el("div", { class: "rv-dispute" },
        RV.el("img", { src: d.zoom_url, alt: "" }),
        RV.el("div", {},
          RV.el("button", { type: "button", onclick: () => use(d.first) }, `Use answer 1: ${answerText(d.first)} (${d.first.by})`),
          RV.el("button", { type: "button", onclick: () => use(d.second) }, `Use answer 2: ${answerText(d.second)} (${d.second.by})`),
          RV.el("button", { type: "button", onclick: () => {
            slot.replaceChildren();
            new RV.SpeciesPicker(slot, (e) => adjudicate(d.event_id,
              { outcome: "species", label: e.name, life_stage: e.life_stage })).focus();
          } }, "Relabel…"),
          slot));
    }) : [RV.el("p", { class: "rv-muted", text: "No disputes." })];
    return RV.el("div", { class: "rv-panel" }, RV.el("h4", { text: `Disputes (${list.length})` }), ...body);
  }

  function agreementPanel(a) {
    const panel = RV.el("div", { class: "rv-panel" }, RV.el("h4", { text: "Agreement on spot-checks" }));
    if (!a) { panel.append(RV.el("p", { text: "Unavailable." })); return panel; }
    const fmt = (b) => (b.pairs < a.min_pairs
      ? `not enough spot-checks yet (${b.pairs} of ${a.min_pairs})`
      : `${Math.round(100 * b.percent)}% agree · alpha ${b.alpha === null ? "n/a" : b.alpha.toFixed(2)} · ${b.pairs} pairs`);
    const how = { model_guess: "accepted the model's guess", hint: "clicked a hint", search: "searched", bulk: "marked with the rest", suggested: "added a new species", none: "gave no species" };
    panel.append(RV.el("p", { text: `Overall: ${fmt(a.overall)}` }),
      RV.el("ul", {}, Object.entries(a.by_label_source).map(([k, b]) =>
        RV.el("li", { text: `When the first volunteer ${how[k] || k}: ${fmt(b)}` }))));
    return panel;
  }

  function statusPanel(s) {
    const panel = RV.el("div", { class: "rv-panel" }, RV.el("h4", { text: "Pool status" }));
    if (!s) { panel.append(RV.el("p", { text: "Unavailable." })); return panel; }
    const run = s.last_run || {};
    const lines = [
      run.finished_at
        ? `Last top-up ${RV.fmtTime(run.finished_at)}: ${run.check_added} + ${run.identify_added} boxes added, `
          + `${run.probes} probes, ${run.duration_s}s, ${run.timeouts} timeouts, ${run.errors} errors`
        : "No top-up has finished yet.",
      `Boxes without hints: ${s.unscored}. Last hint written: ${s.last_hinted_at
        ? RV.fmtTime(s.last_hinted_at) : "never (the scorer arrives in phase 2)"}.`,
      ...s.items.map((i) => `${i.lane} · pass ${i.pass} · ${i.status}: ${i.count}`),
    ];
    panel.append(RV.el("ul", {}, lines.map((t) => RV.el("li", { text: t }))));
    return panel;
  }

  return { enter };
})();
