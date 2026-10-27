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
"use strict";

RV.views.browse = (() => {
  const root = () => document.getElementById("rv-browse");
  const filters = { status: "any", species: "", camera: "", who: "anyone", lane: "any", date_from: "", date_to: "" };
  let page = 0;
  let progress = null;

  const BADGES = { single: ["✓ verified", "ok"], agreed: ["✓✓ agreed", "two"], adjudicated: ["settled", "two"],
    disputed: ["disputed", "disp"], in_queue: ["in queue", "open"],
    awaiting_second_opinion: ["awaiting 2nd opinion", "open"] };

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
    root().replaceChildren(
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
      RV.role === "admin" ? RV.el("section", { id: "rv-admin", class: "rv-admin" }) : null);
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
    const names = RV.el("datalist", { id: "rv-species-names" },
      [...new Set(RV.catalogue.map((e) => e.name))].map((n) => RV.el("option", { value: n })));
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
    const [disputes, agreement, status] = await Promise.all([
      RV.get("/api/review/disputes"), RV.get("/api/review/agreement"), RV.get("/api/review/status")]);
    section.replaceChildren(
      RV.el("h3", { text: "Administrator" }),
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
    const how = { model_guess: "accepted the model's guess", hint: "clicked a hint", search: "searched", none: "gave no species" };
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
