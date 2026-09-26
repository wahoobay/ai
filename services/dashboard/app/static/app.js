const fmtMs = (x) => (x == null ? "—" : `${x.toFixed(0)} ms`);
const fmtPct = (x) => (x == null ? "—" : `${(x * 100).toFixed(1)}%`);
const short = (s) => (s ? s.slice(0, 40) : "");

async function poll(url) {
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  return r.json();
}

// ---------- localStorage stale-while-revalidate cache ----------
//
// Returning visitors see the panels populated immediately from their
// last successful response; the live fetch then replaces it. Discards
// snapshots older than this TTL so a long-absent visitor doesn't get
// misled by stale data — they fall back to the (now matview-backed,
// sub-100 ms) live fetch.
const SWR_TTL_MS = 60 * 60 * 1000;       // 1 hour
const SWR_PREFIX = "wb_swr_";

// WMO weather codes (Open-Meteo). See https://open-meteo.com/en/docs.
// Used when an NWS METAR text description isn't available — the codes are a
// coarser categorical signal than NWS's plain-English "Clear" / "Partly
// Cloudy" / "Light Rain" labels, but they cover the same conceptual space.
const WMO_WX = {
  0:  ["Clear",                 "☀️"],
  1:  ["Mainly clear",          "🌤️"],
  2:  ["Partly cloudy",         "⛅"],
  3:  ["Overcast",              "☁️"],
  45: ["Fog",                   "🌫️"],
  48: ["Depositing rime fog",   "🌫️"],
  51: ["Light drizzle",         "🌦️"],
  53: ["Moderate drizzle",      "🌦️"],
  55: ["Dense drizzle",         "🌧️"],
  56: ["Light freezing drizzle","🌧️"],
  57: ["Dense freezing drizzle","🌧️"],
  61: ["Light rain",            "🌧️"],
  63: ["Moderate rain",         "🌧️"],
  65: ["Heavy rain",            "🌧️"],
  66: ["Light freezing rain",   "🌧️"],
  67: ["Heavy freezing rain",   "🌧️"],
  71: ["Light snow",            "🌨️"],
  73: ["Moderate snow",         "🌨️"],
  75: ["Heavy snow",            "🌨️"],
  77: ["Snow grains",           "🌨️"],
  80: ["Light rain showers",    "🌦️"],
  81: ["Moderate rain showers", "🌧️"],
  82: ["Violent rain showers",  "⛈️"],
  85: ["Light snow showers",    "🌨️"],
  86: ["Heavy snow showers",    "🌨️"],
  95: ["Thunderstorm",          "⛈️"],
  96: ["Thunderstorm w/ light hail",  "⛈️"],
  99: ["Thunderstorm w/ heavy hail",  "⛈️"],
};

// Prefer the NWS METAR text description when available — it's a real-world
// observation from KPMP (Pompano Beach Air Park, ~2 mi from Wahoo Bay).
// Fall back to the WMO weather code from Open-Meteo, which is a forecast-
// model categorical, when no METAR description has landed yet.
function conditionsText(row) {
  if (row?.weather_description) return row.weather_description;
  const code = row?.weather_code;
  const entry = code != null ? WMO_WX[code] : null;
  return entry ? `${entry[1]} ${entry[0]}` : "—";
}

function readCached(url) {
  try {
    const raw = localStorage.getItem(SWR_PREFIX + url);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed.ts !== "number") return null;
    if (Date.now() - parsed.ts > SWR_TTL_MS) return null;
    return parsed; // { data, ts }
  } catch { return null; }
}

function writeCached(url, data) {
  try {
    localStorage.setItem(SWR_PREFIX + url, JSON.stringify({ data, ts: Date.now() }));
  } catch {
    // localStorage full / disabled — ignore, cache is best-effort
  }
}

async function pollWithCache(url) {
  const fresh = await poll(url);
  writeCached(url, fresh);
  return fresh;
}

function renderCurrent(live) {
  const el = document.getElementById("current-list");
  const countEl = document.getElementById("current-count");
  const dets = live?.detections || [];
  if (countEl) countEl.textContent = dets.length ? `(${dets.length} on-screen)` : "";
  if (!dets.length) {
    el.innerHTML = `<li class="empty">no fish in current frame</li>`;
    return;
  }
  el.innerHTML = dets
    .slice()
    .sort((a, b) => (b.best_accuracy ?? 0) - (a.best_accuracy ?? 0))
    .map((d) => {
      const latin = d.best_name;
      const common = d.best_common;
      // Canonical display = "Common Name (G. species)" with italic
      // parenthetical and the genus abbreviated to its initial. Falls
      // back to italic-abbrev alone when there's no curated common name.
      const display = latin ? speciesDisplayHtml(latin, common) : "unknown";
      const acc = d.best_accuracy;
      const topk = (d.topk || [])
        .slice(1)
        .map((p) => `<span class="meta">${speciesDisplayHtml(p.name, p.common)} ${fmtPct(p.accuracy)}</span>`)
        .join(" · ");
      return `
        <li>
          <span class="species-name">${display}</span>
          <span class="acc">${fmtPct(acc)}</span>
          <span class="detconf">det ${fmtPct(d.det_conf)}</span>
          <div class="meta">bbox ${d.bbox.join(",")}</div>
          <div>${topk}</div>
        </li>`;
    })
    .join("");
}

// Rolling-window FPS: derived from a monotonically increasing counter
// sampled at every tick(). Window slides over ~the last 5 s, so a
// sustained-rate change shows up within a couple of seconds without
// flickering frame-to-frame. Keyed so we can track grab and inference
// rates independently.
const _FPS_WINDOW_MS = 5000;
const _fpsSamples = new Map(); // key → [{count, ts_ms}, ...]

function computeFps(key, count) {
  if (count == null) return null;
  const now = performance.now();
  let samples = _fpsSamples.get(key);
  if (!samples) {
    samples = [];
    _fpsSamples.set(key, samples);
  }
  // Worker restart → counter jumps backward; drop the old window.
  if (samples.length && count < samples[samples.length - 1].count) {
    samples.length = 0;
  }
  samples.push({ count, ts_ms: now });
  while (samples.length > 1 && (now - samples[0].ts_ms) > _FPS_WINDOW_MS) {
    samples.shift();
  }
  if (samples.length < 2) return null;
  const oldest = samples[0];
  const dt = (now - oldest.ts_ms) / 1000;
  if (dt < 0.4) return null; // not enough span to be meaningful
  return (count - oldest.count) / dt;
}

function renderStats(live, stats) {
  document.getElementById("stat-frames").textContent = `frames: ${stats.frames_seen ?? "—"}`;
  const grabFps = computeFps("grab", stats.frames_seen);
  document.getElementById("stat-fps").textContent = `fps: ${grabFps == null ? "—" : grabFps.toFixed(1)}`;
  document.getElementById("stat-fish").textContent = `with fish: ${stats.frames_with_fish ?? "—"}`;
  document.getElementById("stat-infer").textContent = `inference: ${fmtMs(live.infer_ms)}`;
  const inferFps = computeFps("infer", stats.frames_inferred);
  document.getElementById("stat-infer-fps").textContent = `infer fps: ${inferFps == null ? "—" : inferFps.toFixed(1)}`;

  // Toggle fallback / "demo mode" banner based on autoswitch state.
  const banner = document.getElementById("fallback-banner");
  if (banner) {
    const isDark = !!(stats.autoswitch && stats.autoswitch.is_dark);
    banner.classList.toggle("hidden", !isDark);
  }
}

function renderSpecies(payload) {
  const el = document.getElementById("species-list");
  // Endpoint now returns {mode, items}; tolerate the old flat-array shape too.
  const rows = Array.isArray(payload) ? payload : (payload?.items || []);
  if (!rows.length) {
    el.innerHTML = `<li class="empty">no species recorded yet</li>`;
    return;
  }
  const isSightings = !Array.isArray(payload) && payload?.mode === "sightings";
  el.innerHTML = rows
    .map(
      (r) => `
      <li>
        <span class="species-name">${
          r.name ? speciesDisplayHtml(r.name, r.common) : escHtml(r.species_id || "unknown")
        }</span>
        <span class="acc">×${r.n}${isSightings ? " fish" : ""}</span>
        <span class="meta">${
          isSightings
            ? `mean acc ${fmtPct(r.mean_acc)} · ${r.total_frames} frames total`
            : `mean ${fmtPct(r.mean_acc)}`
        } · last ${new Date(r.last_seen).toLocaleTimeString()}</span>
      </li>`
    )
    .join("");
}

// EVENT_CACHE keeps the latest /api/events payload keyed by event_id so the
// per-event correction form can look up the row (topk, bbox, image availability)
// without re-querying the API.
const EVENT_CACHE = new Map();

function renderEvents(rows) {
  const el = document.getElementById("events-list");
  if (!rows || !rows.length) {
    el.innerHTML = `<li class="empty">no events yet</li>`;
    return;
  }
  // Don't .clear() — if the reviewer has the modal open on an event that's
  // since scrolled off this poll's top-N, wiping the cache nukes its topk +
  // best_name and the modal re-renders with no "Looks right" button, no
  // top-K chips, and the bbox editor bails out (destroying its own SVG
  // children on the way). Cache eviction is owned by postCorrection.
  for (const r of rows) EVENT_CACHE.set(r.id, r);

  el.innerHTML = rows.map(renderEventCard).join("");
}

function renderEventCard(r) {
  const acc = fmtPct(r.best_accuracy);
  const hasImage = !!r.image_path;
  const thumb = hasImage
    ? `<img class="event-thumb" src="/api/detections/${r.id}/roi.jpg?size=160" alt="" loading="lazy" onclick="openRoiModal(${r.id})" title="click to enlarge" onerror="this.style.display='none'">`
    : `<div class="event-thumb event-thumb-empty" title="no saved frame on disk for this detection (worker saves frames selectively)">no frame</div>`;
  const meta = `${new Date(r.ts).toLocaleTimeString()} · ${short(r.source_name)} · frame ${r.frame_id}`;
  return `
    <li class="event-item" data-event-id="${r.id}">
      <div class="event-row">
        ${thumb}
        <div class="event-info">
          <div class="event-line1">
            <span class="species-name">${speciesDisplayHtml(r.best_name, r.best_common)}</span>
            <span class="acc">${acc}</span>
          </div>
          <div class="meta">${meta}</div>
          ${eventActionsHtml(r.id)}
        </div>
      </div>
      <div class="correction-slot"></div>
    </li>`;
}

// Shared action-button HTML — rendered both in the list card and inside the
// ROI lightbox so a reviewer can accept / correct / reject from whichever
// view they're focused on. EVENT_CACHE drives the per-event eligibility
// checks (e.g. only show "Looks right" when the model produced a best_name).
function eventActionsHtml(eventId) {
  const r = EVENT_CACHE.get(eventId);
  const canEndorse = !!r?.best_name;
  const endorse = canEndorse
    ? `<button class="ev-btn ev-btn-ok"   onclick="endorseEvent(${eventId})" title="confirm the model got it right">✓ Looks right</button>`
    : "";
  const correct = `<button class="ev-btn ev-btn-edit" onclick="toggleCorrection(${eventId})" title="correct or reject this prediction">✏️ Correct</button>`;
  return `<div class="event-actions">${endorse}${correct}</div>`;
}

function correctionFormHtml(eventId) {
  const r = EVENT_CACHE.get(eventId);
  const original = r?.best_name || "";
  const topk = Array.isArray(r?.topk) ? r.topk : [];
  // De-dup: best_name is already the #1 of topk; show the OTHER top-K as
  // one-click alternatives. Most reviewer "corrections" should be one click.
  const altChips = topk
    .filter((t) => t?.name && t.name !== original)
    .slice(0, 4)
    .map((t) => `
      <button type="button" class="topk-chip" onclick="pickTopK(${eventId}, '${escAttr(t.name)}', '${escAttr(t.species_id || "")}')">
        ${speciesDisplayHtml(t.name, t.common)} <span class="topk-conf">${fmtPct(t.accuracy)}</span>
      </button>`)
    .join("");
  const altsBlock = altChips
    ? `<div class="cf-section">
         <div class="cf-label">Or pick a top-K alternative:</div>
         <div class="cf-chips">${altChips}</div>
       </div>`
    : "";
  return `
    <div class="correction-form">
      <div class="cf-section">
        <div class="cf-label">Type the correct species name:</div>
        <input type="text"
               id="cf-name-${eventId}"
               class="cf-name"
               placeholder="start typing — e.g. ${escAttr(original) || 'Sergeant Major'}"
               autocomplete="off"
               oninput="onSpeciesInput(${eventId}, event)">
        <ul class="cf-suggest" id="cf-suggest-${eventId}"></ul>
        <input type="hidden" id="cf-species-id-${eventId}" value="">
      </div>
      ${altsBlock}
      ${getCelebrityOptionsHtml(eventId, r?.best_species_id)}
      <div class="cf-section cf-quick">
        <button type="button" class="cf-bad" onclick="submitNotAFish(${eventId})">
          ✗ Not a fish
        </button>
        <select id="cf-conf-${eventId}" class="cf-conf" title="how sure are you?">
          <option value="certain">certain</option>
          <option value="probable" selected>probable</option>
          <option value="uncertain">uncertain</option>
        </select>
      </div>
      <div class="cf-section">
        <input type="text" id="cf-notes-${eventId}" class="cf-notes" placeholder="notes (optional)">
      </div>
      <div class="cf-actions">
        <button type="button" class="secondary" onclick="closeCorrection(${eventId})">cancel</button>
        <button type="button" onclick="submitCorrection(${eventId})">save correction</button>
      </div>
    </div>
  `;
}

function escAttr(s) { return String(s ?? "").replace(/'/g, "&#39;").replace(/"/g, "&quot;"); }
function escHtml(s) { return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }

// Canonical species display: "Common Name (G. species)" with italic
// parenthetical. The genus is abbreviated to its initial + period — matches
// the format wired into assets/species_manifest.json's display_label_html
// field. Returns "" / "unknown" sentinels if the binomial is missing.
function abbreviateBinomial(latin) {
  if (!latin) return "";
  const parts = String(latin).trim().split(/\s+/);
  if (parts.length < 2 || !parts[0]) return latin;
  return `${parts[0][0]}. ${parts.slice(1).join(" ")}`;
}
function speciesDisplayHtml(latin, common) {
  if (!latin) return common ? escHtml(common) : "unknown";
  const abbrev = abbreviateBinomial(latin);
  const paren = `<em>(${escHtml(abbrev)})</em>`;
  return common ? `${escHtml(common)} ${paren}` : paren;
}
function speciesDisplayText(latin, common) {
  if (!latin) return common || "unknown";
  const abbrev = abbreviateBinomial(latin);
  return common ? `${common} (${abbrev})` : `(${abbrev})`;
}

// Find the *active* container for an event's correction UI. When the ROI
// modal is open on this event, the modal wins (it's the focused surface);
// otherwise we fall back to the inline list card. Centralises this lookup
// so toggleCorrection / postCorrection / pickTopK all agree on where the
// form lives — and the cf-*-${eventId} IDs stay unique in the DOM.
function _activeEventContainer(eventId) {
  const modal = document.getElementById("roi-modal");
  const modalActions = document.getElementById("roi-modal-actions");
  if (
    modal && !modal.classList.contains("hidden")
    && modalActions?.dataset.eventId === String(eventId)
  ) return modalActions;
  return document.querySelector(`li[data-event-id="${eventId}"]`);
}

function toggleCorrection(eventId) {
  const el = _activeEventContainer(eventId);
  if (!el) return;
  const slot = el.querySelector(".correction-slot");
  if (slot.innerHTML.trim()) {
    slot.innerHTML = "";
  } else {
    slot.innerHTML = correctionFormHtml(eventId);
    const nameInput = document.getElementById(`cf-name-${eventId}`);
    if (nameInput) nameInput.focus();
  }
}

function closeCorrection(eventId) {
  const el = _activeEventContainer(eventId);
  if (!el) return;
  const slot = el.querySelector(".correction-slot");
  if (slot) slot.innerHTML = "";
}

function pickTopK(eventId, name, speciesId) {
  const nameEl = document.getElementById(`cf-name-${eventId}`);
  const idEl = document.getElementById(`cf-species-id-${eventId}`);
  if (nameEl) nameEl.value = name;
  if (idEl) idEl.value = speciesId || "";
  // Auto-submit on a top-K chip click — it's a one-click correction.
  submitCorrection(eventId);
}

// Species autocomplete. /api/species/list returns up to ~hundreds of items;
// we load once on first use and filter client-side.
let SPECIES_CACHE = null;
async function loadSpeciesIfNeeded() {
  if (SPECIES_CACHE !== null) return SPECIES_CACHE;
  try {
    const r = await fetch("/api/species/list?hours=720&min_frames=1");
    if (!r.ok) throw new Error(`${r.status}`);
    const j = await r.json();
    SPECIES_CACHE = Array.isArray(j?.species) ? j.species : [];
  } catch {
    SPECIES_CACHE = [];
  }
  return SPECIES_CACHE;
}

function onSpeciesInput(eventId, ev) {
  const q = (ev?.target?.value || "").trim().toLowerCase();
  const list = document.getElementById(`cf-suggest-${eventId}`);
  if (!list) return;
  if (q.length < 2) { list.innerHTML = ""; return; }
  loadSpeciesIfNeeded().then((species) => {
    const matches = species
      .filter((s) => (s.latin || "").toLowerCase().includes(q) || (s.common || "").toLowerCase().includes(q))
      .slice(0, 8);
    list.innerHTML = matches.map((s) => `
      <li onclick="pickSpecies(${eventId}, '${escAttr(s.latin)}', '${escAttr(s.species_id || "")}')">
        <span class="sg-latin">${escHtml(s.latin)}</span>
        ${s.common ? `<span class="sg-common">${escHtml(s.common)}</span>` : ""}
      </li>`).join("");
  });
}

function pickSpecies(eventId, latin, speciesId) {
  document.getElementById(`cf-name-${eventId}`).value = latin;
  document.getElementById(`cf-species-id-${eventId}`).value = speciesId || "";
  document.getElementById(`cf-suggest-${eventId}`).innerHTML = "";
}

// "Looks right" one-click endorsement: posts a correction with
// corrected_name = best_name and is_endorsement=true. Used to register
// positive signal on good predictions (fine-tuning dataset gets both
// positive and negative examples instead of just negatives).
// _maybeBbox returns the edited bbox for this event if the modal is in
// edit mode AND the user actually moved the box, else null. The three
// submit handlers all merge it in so any of them can carry a bbox
// correction alongside the species / endorsement / not-a-fish decision.
function _maybeCelebrity(eventId) {
  // Read the optional "Tag as celebrity" dropdown value if it's
  // present in the current correction form. Empty string → null.
  const sel = document.getElementById(`cf-celebrity-${eventId}`);
  const v = (sel?.value || "").trim();
  return v || null;
}

function _maybeBbox(eventId) {
  if (!_bboxEdit.active || _bboxEdit.eventId !== eventId) return null;
  return _consumeEditedBbox();
}

async function endorseEvent(eventId) {
  const r = EVENT_CACHE.get(eventId);
  if (!r?.best_name) return;
  const body = {
    event_id: eventId,
    corrected_name: r.best_name,
    corrected_species_id: r.best_species_id || null,
    not_a_fish: false,
    is_endorsement: true,
    corrected_bbox: _maybeBbox(eventId),
    celebrity_id: _maybeCelebrity(eventId),
    confidence: "certain",
    notes: null,
  };
  await postCorrection(eventId, body, "endorsed");
}

async function submitNotAFish(eventId) {
  const body = {
    event_id: eventId,
    corrected_name: null,
    corrected_species_id: null,
    not_a_fish: true,
    is_endorsement: false,
    corrected_bbox: _maybeBbox(eventId),
    celebrity_id: _maybeCelebrity(eventId),
    confidence: document.getElementById(`cf-conf-${eventId}`)?.value || "probable",
    notes: (document.getElementById(`cf-notes-${eventId}`)?.value || "").trim() || null,
  };
  await postCorrection(eventId, body, "not a fish");
}

async function submitCorrection(eventId) {
  const name  = document.getElementById(`cf-name-${eventId}`).value.trim();
  const spid  = document.getElementById(`cf-species-id-${eventId}`).value.trim();
  const conf  = document.getElementById(`cf-conf-${eventId}`).value;
  const notes = document.getElementById(`cf-notes-${eventId}`).value.trim();
  if (!name) {
    alert("type a species name or pick one from the suggestions / top-K chips");
    return;
  }
  const r = EVENT_CACHE.get(eventId);
  const isEndorsement = !!(r?.best_name && r.best_name === name);
  const body = {
    event_id: eventId,
    corrected_name: name,
    corrected_species_id: spid || null,
    not_a_fish: false,
    is_endorsement: isEndorsement,
    corrected_bbox: _maybeBbox(eventId),
    celebrity_id: _maybeCelebrity(eventId),
    confidence: conf,
    notes: notes || null,
  };
  await postCorrection(eventId, body, isEndorsement ? "endorsed" : "corrected");
}

async function postCorrection(eventId, body, label) {
  try {
    const resp = await fetch("/api/corrections", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Requested-With": "wahoobay" },
      body: JSON.stringify(body),
    });
    if (resp.status === 401) {
      alert("sign in as a reviewer to submit corrections");
      return;
    }
    let data = {};
    try { data = await resp.json(); } catch (e) { data = {}; }
    // A role refusal has no `error` field (it is FastAPI's `detail`); the
    // review rules' refusals do, and say why (follow-up spec A1).
    if (resp.status === 403 && !data.error) {
      alert("your account isn't approved for corrections yet — an administrator needs to grant reviewer access");
      return;
    }
    if (!resp.ok) {
      alert(data.error || `save failed: ${resp.status}`);
      return;
    }
    // Reflect the save on BOTH surfaces (list card + modal action panel) so
    // the reviewer sees the same "✓ saved" state whichever one they look at,
    // and can't double-submit by switching between them.
    const li = document.querySelector(`li[data-event-id="${eventId}"]`);
    const modalActions = document.getElementById("roi-modal-actions");
    const modalIsForThisEvent = modalActions?.dataset?.eventId === String(eventId);
    const containers = [li, modalIsForThisEvent ? modalActions : null].filter(Boolean);
    for (const c of containers) {
      const slot = c.querySelector(".correction-slot");
      if (slot) slot.innerHTML = `<div class="correction-ok">✓ ${label}</div>`;
      for (const b of c.querySelectorAll(".ev-btn")) b.disabled = true;
    }
    tickCorrectionStats();
    // Once an event has been reviewed it should drop out of the queue.
    // Let the "✓ saved" indicator linger briefly for visual confirmation,
    // then fade the list card out and close the modal (if it's open on
    // this event). EVENT_CACHE eviction keeps the next tickEvents() in
    // sync — the server now filters reviewed events out of /api/events
    // by default, so refreshes also see the cleaner queue.
    EVENT_CACHE.delete(eventId);
    setTimeout(() => {
      if (li) {
        li.classList.add("event-removing");
        setTimeout(() => li.remove(), 320);
      }
      if (modalIsForThisEvent) {
        const modal = document.getElementById("roi-modal");
        if (modal) modal.classList.add("hidden");
      }
    }, 700);
  } catch (e) {
    alert(`save failed: ${e}`);
  }
}

async function tickCorrectionStats() {
  try {
    const s = await poll("/api/corrections/stats");
    const el = document.getElementById("corrections-stats");
    if (!s || !s.total) {
      el.textContent = "corrections: none yet";
    } else {
      el.textContent = `corrections: ${s.total} total · ${s.reviewers ?? 0} reviewer(s) · ${s.not_a_fish ?? 0} false positives`;
    }
  } catch {}
}

async function tick() {
  try {
    const [live, stats] = await Promise.all([
      poll("/api/live.json"),
      poll("/api/stats"),
    ]);
    renderCurrent(live);
    renderStats(live, stats);
  } catch (e) {
    // keep previous render
  }
}

async function tickHistory() {
  // Render last-known-good snapshot immediately so returning visitors
  // see populated panels on first paint; live data overwrites below.
  const cs = readCached("/api/species_counts?hours=24");
  const ce = readCached("/api/events?limit=25");
  if (cs) renderSpecies(cs.data);
  if (ce) renderEvents(ce.data);
  try {
    const [species, events] = await Promise.all([
      pollWithCache("/api/species_counts?hours=24").catch(() => null),
      pollWithCache("/api/events?limit=25").catch(() => null),
    ]);
    if (species != null) renderSpecies(species);
    if (events  != null) renderEvents(events);
  } catch (e) {
    // ignore — the cached snapshot above is what the user sees
  }
}

// ---------- Water quality panel ----------

// Stored values stay in their source units (e.g. water_temp_c is Celsius
// from the sonde); fmt converts to display units. The `normal` range is
// in the *stored* units so the out-of-range comparison stays consistent
// across cell value, sparkline, and any future alerting that hits the
// raw value.
const WQ_PARAMS = [
  { key: "water_temp_c",      label: "Water temp", unit: "°F",    fmt: (v) => (v * 9/5 + 32).toFixed(1), normal: [18, 32] },
  { key: "ph",                label: "pH",          unit: "",     fmt: (v) => v.toFixed(2), normal: [7.6, 8.4] },
  { key: "do_pct",            label: "DO",          unit: "% sat", fmt: (v) => v.toFixed(1), normal: [60, 130] },
  { key: "chlorophyll_rfu",   label: "Chlorophyll", unit: "RFU",  fmt: (v) => v.toFixed(2), normal: [0, 6] },
  { key: "phycoerythrin_rfu", label: "Phycoerythrin", unit: "RFU", fmt: (v) => v.toFixed(2), normal: [0, 3] },
  { key: "turbidity_fnu",     label: "Turbidity",   unit: "FNU",  fmt: (v) => v.toFixed(2), normal: [0, 15] },
  { key: "spcond_ms_cm",      label: "Sp. cond.",   unit: "mS/cm", fmt: (v) => v.toFixed(2), normal: [25, 60] },
];

function sparkline(values, normalRange) {
  const pts = values.filter((v) => v != null);
  if (pts.length < 2) return "";
  const w = 100, h = 28, pad = 1;
  const lo = Math.min(...pts), hi = Math.max(...pts);
  const span = hi - lo || 1;
  const x = (i) => pad + (i / (pts.length - 1)) * (w - 2 * pad);
  const y = (v) => h - pad - ((v - lo) / span) * (h - 2 * pad);
  const d = pts.map((v, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
  const last = pts[pts.length - 1];
  const [nlo, nhi] = normalRange || [-Infinity, Infinity];
  const tone = (last < nlo || last > nhi) ? "var(--warn)" : "var(--accent)";
  return `
    <svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
      <path d="${d}" fill="none" stroke="${tone}" stroke-width="1.2" vector-effect="non-scaling-stroke"/>
      <circle cx="${x(pts.length - 1).toFixed(1)}" cy="${y(last).toFixed(1)}" r="1.8" fill="${tone}"/>
    </svg>
  `;
}

function renderWaterQuality(latest, history) {
  const grid = document.getElementById("wq-grid");
  const meta = document.getElementById("wq-meta");
  if (!latest) {
    grid.innerHTML = `<div class="empty">no readings yet</div>`;
    meta.textContent = "—";
    return;
  }

  const when = new Date(latest.ts).toLocaleString();
  const src = latest.source || "live";
  meta.textContent = `${latest.deployment_uri} · ${when} · ${src}`;

  const series = (history && history.series) || [];
  const cells = WQ_PARAMS.map((p) => {
    const v = latest[p.key];
    const vals = series.map((r) => r[p.key]);
    const [lo, hi] = p.normal;
    const oor = v != null && (v < lo || v > hi);
    return `
      <div class="wq-cell ${oor ? "out-of-range" : ""}">
        <div class="wq-label">${p.label}</div>
        <div class="wq-value">${v == null ? "—" : p.fmt(v)}<span class="wq-unit">${p.unit}</span></div>
        ${sparkline(vals, p.normal)}
      </div>
    `;
  }).join("");

  const banner = src === "synthetic"
    ? `<div class="wq-synthetic-banner">Live SenseStream feed temporarily unreachable — showing synthetic placeholder readings. Auto-recovers on the next successful poll.</div>`
    : "";
  grid.innerHTML = banner + cells;
}

async function tickWaterQuality() {
  const cl = readCached("/api/water_quality/latest");
  const ch = readCached("/api/water_quality/history?hours=24&max_points=144");
  if (cl || ch) renderWaterQuality(cl?.data ?? null, ch?.data ?? null);
  try {
    const [latest, history] = await Promise.all([
      pollWithCache("/api/water_quality/latest").catch(() => null),
      pollWithCache("/api/water_quality/history?hours=24&max_points=144").catch(() => null),
    ]);
    renderWaterQuality(latest, history);
  } catch (e) {
    // keep cached render
  }
}

// ---------- Input-drift panel ----------

const DRIFT_METRICS = [
  { key: "mean_luma",                 label: "Brightness",  fmt: (v) => v.toFixed(1),       warnOnAbsDelta: 15 },
  { key: "green_blue",                label: "Green-blue",  fmt: (v) => v.toFixed(1),       derive: (r) => r.mean_g - r.mean_b, warnOnAbsDelta: 6 },
  { key: "mean_detections_per_frame", label: "Detections/fr", fmt: (v) => v.toFixed(2),     warnOnAbsDelta: 1.0 },
  { key: "frame_with_fish_rate",      label: "Fish-frame %", fmt: (v) => (100 * v).toFixed(0) + "%", warnOnAbsDelta: 0.2 },
];

function driftSparkline(values) {
  const pts = values.filter((v) => v != null);
  if (pts.length < 2) return "";
  const w = 100, h = 20, pad = 1;
  const lo = Math.min(...pts), hi = Math.max(...pts);
  const span = hi - lo || 1;
  const x = (i) => pad + (i / (pts.length - 1)) * (w - 2 * pad);
  const y = (v) => h - pad - ((v - lo) / span) * (h - 2 * pad);
  const d = pts.map((v, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
  return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    <path d="${d}" fill="none" stroke="var(--accent)" stroke-width="1.2" vector-effect="non-scaling-stroke"/>
    <circle cx="${x(pts.length - 1).toFixed(1)}" cy="${y(pts[pts.length - 1]).toFixed(1)}" r="1.6" fill="var(--accent)"/>
  </svg>`;
}

function renderDrift(timeline, recent) {
  const grid = document.getElementById("drift-grid");
  const meta = document.getElementById("drift-meta");

  if (!timeline || timeline.length === 0) {
    grid.innerHTML = `<div class="empty">collecting samples…</div>`;
    meta.textContent = "—";
    return;
  }

  // per-source pick the first source's timeline (single-camera for now)
  const bySrc = {};
  for (const r of timeline) {
    (bySrc[r.source_name] ||= []).push(r);
  }
  const source = Object.keys(bySrc)[0];
  const series = bySrc[source];
  const lastHour = series[series.length - 1];
  const totalSamples = series.reduce((a, b) => a + (b.samples || 0), 0);
  meta.textContent = `${source} · ${series.length} h · ${totalSamples} samples`;

  // baseline rows from drift view (per source)
  const baseline = (recent || []).find((r) => r.source_name === source) || {};

  const rows = DRIFT_METRICS.map((m) => {
    const valFn = m.derive ? m.derive : (r) => r[m.key];
    const vals = series.map(valFn);
    const current = vals[vals.length - 1];

    // choose baseline to compare against
    let baseVal = null, baseLabel = "";
    if (m.key === "mean_luma")            { baseVal = baseline.luma_7d; baseLabel = "vs 7d"; }
    else if (m.key === "green_blue")      { baseVal = (baseline.g_7d != null && baseline.b_7d != null) ? (baseline.g_7d - baseline.b_7d) : null; baseLabel = "vs 7d"; }
    else if (m.key === "frame_with_fish_rate") { baseVal = baseline.fish_rate_7d; baseLabel = "vs 7d"; }

    let deltaHtml = '<span class="dl-delta">—</span>';
    if (baseVal != null && current != null) {
      const d = current - baseVal;
      const tone = Math.abs(d) > m.warnOnAbsDelta ? "neg" : "pos";
      const sign = d >= 0 ? "+" : "";
      deltaHtml = `<span class="dl-delta ${tone}">${sign}${m.fmt(d)} ${baseLabel}</span>`;
    }

    return `<div class="drift-row">
      <span class="dl-label">${m.label}</span>
      <span class="dl-value">${current == null ? "—" : m.fmt(current)}</span>
      ${driftSparkline(vals)}
      ${deltaHtml}
    </div>`;
  }).join("");

  grid.innerHTML = rows;
}

async function tickDrift() {
  try {
    const [timeline, recent] = await Promise.all([
      poll("/api/drift/timeline?hours=72").catch(() => []),
      poll("/api/drift/recent").catch(() => []),
    ]);
    renderDrift(timeline, recent);
  } catch {}
}

// ---------- Alerts banner ----------

const AGE_FMT = (isoStr) => {
  const ms = Date.now() - new Date(isoStr).getTime();
  if (ms < 60_000) return `${Math.round(ms / 1000)}s`;
  if (ms < 3600_000) return `${Math.round(ms / 60_000)}m`;
  return `${Math.round(ms / 3_600_000)}h`;
};

function renderAlerts(rows) {
  const banner = document.getElementById("alerts-banner");
  if (!rows || rows.length === 0) {
    banner.classList.add("hidden");
    banner.innerHTML = "";
    return;
  }
  banner.classList.remove("hidden");
  banner.innerHTML = rows.map((a) => `
    <div class="alert-row">
      <span class="alert-sev ${a.severity}">${a.severity}</span>
      <span class="alert-msg"><b>${a.name}</b>: ${a.message}</span>
      <span class="alert-age">${AGE_FMT(a.first_seen)} old</span>
      ${a.acknowledged_at
        ? `<span class="alert-age">ack'd by ${a.acknowledged_by}</span>`
        : `<button class="alert-ack" onclick="ackAlert(${a.id})">ack</button>`
      }
    </div>`).join("");
}

async function ackAlert(id) {
  try {
    const r = await fetch(`/api/alerts/${id}/ack`,
      { method: "POST", headers: { "X-Requested-With": "wahoobay" } });
    if (r.status === 401) {
      alert("sign in as an admin to acknowledge alerts");
      return;
    }
    if (r.status === 403) {
      alert("admin role required to acknowledge alerts");
      return;
    }
    tickAlerts();
  } catch {}
}

async function tickAlerts() {
  try {
    const rows = await poll("/api/alerts/active");
    renderAlerts(rows);
  } catch {}
}

// ---------- Downloads panel ----------

function refreshDownloadLinks() {
  const sel = document.getElementById("dl-window");
  const fmt = document.getElementById("dl-format");
  if (!sel) return;
  const hours = sel.value;
  const wantParquet = fmt && fmt.value === "parquet";
  for (const a of document.querySelectorAll(".dl-btn")) {
    const resource = a.dataset.export;
    // Static sidecars are unaffected by window / format
    if (a.dataset.static === "md") {
      a.href = `/api/export/${resource}.md`;
      continue;
    }
    if (a.dataset.static === "json") {
      a.href = `/api/export/${resource}.json`;
      continue;
    }
    const params = new URLSearchParams();
    if (resource === "alerts") {
      params.set("include_resolved", "true");
    } else if (resource === "labeled_corrections") {
      // no params — corrections export is unbounded
    } else {
      const w = a.dataset.defaultWindow || hours;
      params.set("hours", w);
    }
    const ext = (wantParquet && a.dataset.parquet === "ok") ? "parquet" : "csv";
    a.href = `/api/export/${resource}.${ext}?${params.toString()}`;
  }
}

function initDownloads() {
  const sel = document.getElementById("dl-window");
  const fmt = document.getElementById("dl-format");
  if (!sel) return;
  sel.addEventListener("change", refreshDownloadLinks);
  if (fmt) fmt.addEventListener("change", refreshDownloadLinks);
  refreshDownloadLinks();
}

// ---------- Reef explorer (visitor-friendly charts) ----------

function f2c(c) { return c == null ? null : (c * 9 / 5 + 32); }

function classifyTemp(c) {
  if (c == null) return ["—", "muted"];
  if (c < 18) return ["cool", "warn"];
  if (c < 24) return ["mild", "good"];
  if (c < 30) return ["warm", "good"];
  return ["hot", "warn"];
}
function classifyDO(p) {
  if (p == null) return ["—", "muted"];
  if (p < 60) return ["low", "warn"];
  if (p < 130) return ["healthy", "good"];
  return ["very high", "warn"];
}
function classifyTurbidity(f) {
  if (f == null) return ["—", "muted"];
  if (f < 3) return ["clear", "good"];
  if (f < 8) return ["slightly cloudy", "good"];
  return ["cloudy", "warn"];
}

function renderTopSpecies(rows) {
  const el = document.getElementById("reef-top-species");
  if (!rows || rows.length === 0) {
    el.innerHTML = `<div class="empty">no fish recorded yet today</div>`;
    return;
  }
  const max = Math.max(...rows.map(r => r.sightings), 1);
  el.innerHTML = rows.map(r => {
    const pct = (100 * r.sightings / max).toFixed(1);
    const display = speciesDisplayHtml(r.latin, r.common);
    return `
      <div class="reef-top-row">
        <div class="reef-bar-wrap">
          <div class="reef-bar" style="width:${pct}%"></div>
          <div class="reef-name">${display}</div>
        </div>
        <div class="reef-count">${r.sightings}</div>
      </div>`;
  }).join("");
}

function renderHourly(rows) {
  const el = document.getElementById("reef-hourly");
  if (!rows || rows.length === 0) {
    el.innerHTML = `<div class="empty" style="font-size:12px">collecting data…</div>`;
    return;
  }
  // Bucket by local hour-of-day (0-23) so a kid sees a daily rhythm rather
  // than a 24-hour rolling timeline (which is harder to read).
  const counts = new Array(24).fill(0);
  for (const r of rows) {
    if (!r.hour) continue;
    const h = new Date(r.hour).getHours();
    counts[h] += r.sightings;
  }
  const max = Math.max(...counts, 1);

  // ViewBox roughly matches the rendered card aspect (24 hours × ~10 units
  // wide each → 240×100). preserveAspectRatio kept default so bars don't
  // stretch; we let height scale naturally from CSS width:100% / height:auto.
  const W = 240, H = 100;
  const padX = 6, padTop = 6, padBottom = 22;
  const plotH = H - padTop - padBottom;
  const bw = (W - 2 * padX) / 24;
  const baselineY = padTop + plotH;

  const bars = counts.map((c, i) => {
    if (c === 0) return "";
    const bh = Math.max(1.2, plotH * (c / max));
    const x = padX + i * bw + 0.6;
    const y = baselineY - bh;
    const op = (0.5 + 0.5 * c / max).toFixed(2);
    return `<rect x="${x.toFixed(2)}" y="${y.toFixed(2)}" width="${(bw - 1.2).toFixed(2)}" height="${bh.toFixed(2)}" fill="var(--accent)" opacity="${op}" rx="0.8"/>`;
  }).join("");

  const baseline = `<line x1="${padX}" x2="${W - padX}" y1="${baselineY}" y2="${baselineY}" stroke="var(--muted)" stroke-width="0.5" opacity="0.4"/>`;

  const ticks = [0, 6, 12, 18, 23].map(i => {
    const x = padX + i * bw + bw / 2;
    return `<text x="${x.toFixed(2)}" y="${(baselineY + 11).toFixed(2)}" font-size="8" fill="var(--muted)" text-anchor="middle">${i}</text>`;
  }).join("");

  // Tiny "max" tick at the top so a viewer can read the y-scale at a glance.
  const peakLabel = `<text x="${(W - padX - 1).toFixed(2)}" y="${(padTop + 6).toFixed(2)}" font-size="7" fill="var(--muted)" text-anchor="end">peak ${max}</text>`;

  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}">${baseline}${bars}${ticks}${peakLabel}</svg>`;
}

function renderConditions(wq) {
  const el = document.getElementById("reef-conditions");
  if (!wq) {
    el.innerHTML = `<div class="empty" style="font-size:12px">water-quality data offline</div>`;
    return;
  }
  const tempC = wq.water_temp_c;
  const tempF = f2c(tempC);
  const [tStatus, tCls] = classifyTemp(tempC);
  const [oStatus, oCls] = classifyDO(wq.do_pct);
  const [trStatus, trCls] = classifyTurbidity(wq.turbidity_fnu);
  const note = wq.source === "synthetic"
    ? `<div class="reef-axis-label">live SenseStream feed temporarily unreachable — showing synthetic placeholder</div>`
    : "";
  el.innerHTML = `
    <div class="reef-cond-tile">
      <div class="reef-cond-label">Water temp</div>
      <div class="reef-cond-value">${tempC == null ? "—" : tempF.toFixed(1) + "°F"}</div>
      <div class="reef-cond-status ${tCls}">${tStatus}</div>
    </div>
    <div class="reef-cond-tile">
      <div class="reef-cond-label">Oxygen</div>
      <div class="reef-cond-value">${wq.do_pct == null ? "—" : wq.do_pct.toFixed(0) + "%"}</div>
      <div class="reef-cond-status ${oCls}">${oStatus}</div>
    </div>
    <div class="reef-cond-tile">
      <div class="reef-cond-label">Water clarity</div>
      <div class="reef-cond-value">${wq.turbidity_fnu == null ? "—" : wq.turbidity_fnu.toFixed(1) + " FNU"}</div>
      <div class="reef-cond-status ${trCls}">${trStatus}</div>
    </div>
  ` + note;
}

function renderTotals(t) {
  const el = document.getElementById("reef-totals");
  if (!t) { el.innerHTML = ""; return; }
  el.innerHTML = `
    <div><span class="reef-num">${t.sightings ?? 0}</span> <span class="reef-cap">fish spotted today</span></div>
    <div><span class="reef-num">${t.unique_species ?? 0}</span> <span class="reef-cap">species identified</span></div>
  `;
}

async function tickReef() {
  const cached = readCached("/api/visitor_stats?hours=24");
  if (cached) {
    const d = cached.data;
    renderTotals(d.totals);
    renderTopSpecies(d.top_species);
    renderHourly(d.hourly_activity);
    renderConditions(d.water_quality);
  }
  try {
    const d = await pollWithCache("/api/visitor_stats?hours=24");
    renderTotals(d.totals);
    renderTopSpecies(d.top_species);
    renderHourly(d.hourly_activity);
    renderConditions(d.water_quality);
  } catch {}
}

function initBboxToggle() {
  const btn = document.getElementById("bbox-toggle");
  const img = document.getElementById("stream");
  if (!btn || !img) return;

  function streamSrc(camId, showing) {
    if (camId === "pier_cam") {
      return showing ? "/api/stream_pier.mjpeg" : "/api/stream_pier_raw.mjpeg";
    }
    return showing ? "/api/stream.mjpeg" : "/api/stream_raw.mjpeg";
  }

  function apply(showing) {
    const cam = window._wbActiveCamera || localStorage.getItem("wb_camera") || "seahivecam";
    img.src = streamSrc(cam, showing);
    btn.textContent = showing ? "🟦 boxes: on" : "🟦 boxes: off";
    btn.classList.toggle("off", !showing);
    localStorage.setItem("wb_bboxes_off", showing ? "0" : "1");
  }

  // restore previous state
  apply(localStorage.getItem("wb_bboxes_off") !== "1");

  btn.addEventListener("click", () => {
    const showing = !btn.classList.contains("off") ? false : true;
    apply(showing);
  });
}

function initReefToggle() {
  const btn = document.getElementById("reef-toggle");
  const body = document.getElementById("reef-body");
  if (!btn || !body) return;
  const saved = localStorage.getItem("wb_reef_hidden") === "1";
  if (saved) { body.classList.add("collapsed"); btn.textContent = "show"; }
  btn.addEventListener("click", () => {
    const collapsed = body.classList.toggle("collapsed");
    btn.textContent = collapsed ? "show" : "hide";
    localStorage.setItem("wb_reef_hidden", collapsed ? "1" : "0");
  });
}

// ====================================================================
// Trends view — charts. All read from materialised hourly views, so
// each fetch is sub-100 ms regardless of detection_events size.
// ====================================================================

// Wong (2011) colour-blind-safe palette + extras for top-species lines.
const TRENDS_PALETTE = [
  "#56b4e9", "#e69f00", "#009e73", "#f0e442",
  "#0072b2", "#d55e00", "#cc79a7", "#ad59c6",
  "#5fb35f", "#9b9b9b",
];

function _timeAxisTicks(minX, maxX, xS, yBaseline, hours) {
  const stepHrs =
    hours <= 12 ? 2 :
    hours <= 48 ? 6 :
    hours <= 168 ? 24 :
    72;
  const stepMs = stepHrs * 3600 * 1000;
  let t = Math.ceil(minX / stepMs) * stepMs;
  const out = [];
  while (t <= maxX) { out.push(t); t += stepMs; }
  return out.map(t => {
    const x = xS(t);
    const d = new Date(t);
    const lbl = hours <= 48
      ? `${d.getHours().toString().padStart(2,"0")}:00`
      : `${d.getMonth()+1}/${d.getDate()}`;
    return `<line x1="${x.toFixed(1)}" x2="${x.toFixed(1)}" y1="${yBaseline}" y2="${yBaseline+3}" stroke="var(--muted)" stroke-width="0.5"/>
            <text x="${x.toFixed(1)}" y="${yBaseline+12}" font-size="9" fill="var(--muted)" text-anchor="middle">${lbl}</text>`;
  }).join("");
}

function _yAxisTicks(values, yS, padL, W, padR) {
  const max = Math.max(...values.filter(v => v != null), 1);
  return [0, Math.round(max/2), max].map(v => {
    const y = yS(v);
    return `<line x1="${padL}" x2="${W-padR}" y1="${y.toFixed(1)}" y2="${y.toFixed(1)}" stroke="var(--muted)" stroke-width="0.3" opacity="0.4"/>
            <text x="${(padL-4).toFixed(1)}" y="${(y+3).toFixed(1)}" font-size="9" fill="var(--muted)" text-anchor="end">${v}</text>`;
  }).join("");
}

function renderDetectionRate(data) {
  const svg  = document.getElementById("chart-detection-rate");
  const meta = document.getElementById("chart-detection-rate-meta");
  if (!svg) return;
  const series = (data && data.series) || [];
  if (series.length === 0) {
    svg.innerHTML = `<text x="240" y="90" font-size="11" fill="var(--muted)" text-anchor="middle">no sightings yet in this window</text>`;
    if (meta) meta.textContent = "";
    return;
  }
  const W=480, H=180, padL=32, padR=10, padT=8, padB=20;
  const xs = series.map(p => new Date(p.hour).getTime());
  const ys = series.map(p => p.sightings);
  const minX = Math.min(...xs), maxX = Math.max(...xs);
  const xrange = (maxX - minX) || 1;
  const maxY = Math.max(...ys, 1);
  const xS = v => padL + ((v - minX)/xrange) * (W - padL - padR);
  const yS = v => H - padB - (v/maxY) * (H - padT - padB);

  const pts = xs.map((x,i) => `${xS(x).toFixed(1)},${yS(ys[i]).toFixed(1)}`);
  const linePath = `M ${pts.join(" L ")}`;
  const areaPath = `M ${xS(xs[0]).toFixed(1)},${(H-padB).toFixed(1)} L ${pts.join(" L ")} L ${xS(xs[xs.length-1]).toFixed(1)},${(H-padB).toFixed(1)} Z`;

  svg.innerHTML = `${_yAxisTicks(ys, yS, padL, W, padR)}
    ${_timeAxisTicks(minX, maxX, xS, H-padB, data.hours || 24)}
    <path d="${areaPath}" fill="var(--accent)" opacity="0.18"/>
    <path d="${linePath}" stroke="var(--accent)" stroke-width="1.4" fill="none"/>`;

  if (meta) {
    const total = ys.reduce((a,b) => a+b, 0);
    meta.textContent = `${total} sightings · last ${data.hours}h`;
  }
}

function renderTopSpeciesTimeseries(data) {
  const svg    = document.getElementById("chart-top-species");
  const legend = document.getElementById("chart-top-species-legend");
  const meta   = document.getElementById("chart-top-species-meta");
  if (!svg) return;
  const species = (data && data.species) || [];
  if (species.length === 0) {
    svg.innerHTML = `<text x="240" y="100" font-size="11" fill="var(--muted)" text-anchor="middle">no sightings yet in this window</text>`;
    if (legend) legend.innerHTML = "";
    if (meta) meta.textContent = "";
    return;
  }
  const W=480, H=200, padL=32, padR=10, padT=8, padB=20;
  const allXs = [], allYs = [];
  species.forEach(sp => sp.points.forEach(p => {
    allXs.push(new Date(p.hour).getTime());
    allYs.push(p.sightings);
  }));
  const minX = Math.min(...allXs), maxX = Math.max(...allXs);
  const xrange = (maxX - minX) || 1;
  const maxY = Math.max(...allYs, 1);
  const xS = v => padL + ((v - minX)/xrange) * (W - padL - padR);
  const yS = v => H - padB - (v/maxY) * (H - padT - padB);

  const lines = species.map((sp, i) => {
    const color = TRENDS_PALETTE[i % TRENDS_PALETTE.length];
    const pts = sp.points.map(p => `${xS(new Date(p.hour).getTime()).toFixed(1)},${yS(p.sightings).toFixed(1)}`).join(" L ");
    return pts ? `<path d="M ${pts}" stroke="${color}" stroke-width="1.4" fill="none"/>` : "";
  }).join("");

  svg.innerHTML = `${_yAxisTicks(allYs, yS, padL, W, padR)}
    ${_timeAxisTicks(minX, maxX, xS, H-padB, data.hours || 24)}
    ${lines}`;

  if (legend) {
    legend.innerHTML = species.map((sp, i) => {
      const color = TRENDS_PALETTE[i % TRENDS_PALETTE.length];
      return `<span><span class="legend-swatch" style="background:${color}"></span>${sp.name} <span style="opacity:.65">(${sp.total})</span></span>`;
    }).join("");
  }
  if (meta) meta.textContent = `top ${species.length} · last ${data.hours}h`;
}

function renderSightingsXWater(data, paramKey) {
  const svg = document.getElementById("chart-sxw");
  if (!svg) return;
  const series = (data && data.series) || [];
  if (series.length === 0) {
    svg.innerHTML = `<text x="240" y="100" font-size="11" fill="var(--muted)" text-anchor="middle">no data yet</text>`;
    return;
  }
  const W=480, H=200, padL=32, padR=42, padT=8, padB=20;

  const xs = series.map(p => new Date(p.hour).getTime());
  const minX = Math.min(...xs), maxX = Math.max(...xs);
  const xrange = (maxX - minX) || 1;
  const xS = v => padL + ((v - minX)/xrange) * (W - padL - padR);

  const sightings = series.map(p => p.sightings ?? 0);
  const maxL = Math.max(...sightings, 1);
  const yL = v => H - padB - (v/maxL) * (H - padT - padB);

  // Display water temperature in Fahrenheit even though it's stored in
  // Celsius. Other params are pass-through.
  const tempCelsius = paramKey === "water_temp_c";
  const wq = series.map(p => {
    const v = p[paramKey];
    return v == null ? null : (tempCelsius ? v * 9/5 + 32 : v);
  });
  const wqDef = wq.filter(v => v != null);
  const minR = wqDef.length ? Math.min(...wqDef) : 0;
  const maxR = wqDef.length ? Math.max(...wqDef) : 1;
  const Rrange = (maxR - minR) || 1;
  const yR = v => H - padB - ((v - minR)/Rrange) * (H - padT - padB);

  const barW = Math.max(1, ((W - padL - padR) / Math.max(1, series.length)) * 0.8);
  const bars = series.map((p, i) => {
    const v = sightings[i];
    if (v <= 0) return "";
    const x = xS(xs[i]) - barW/2;
    const yTop = yL(v);
    const h = (H - padB) - yTop;
    return h >= 0.5 ? `<rect x="${x.toFixed(1)}" y="${yTop.toFixed(1)}" width="${barW.toFixed(1)}" height="${h.toFixed(1)}" fill="var(--accent)" opacity="0.5"/>` : "";
  }).join("");

  let linePath = "";
  if (wqDef.length >= 2) {
    const pts = wq
      .map((v, i) => v != null ? `${xS(xs[i]).toFixed(1)},${yR(v).toFixed(1)}` : null)
      .filter(Boolean)
      .join(" L ");
    linePath = `<path d="M ${pts}" stroke="var(--warn)" stroke-width="1.4" fill="none"/>`;
  }

  const yLeftTicks  = [0, Math.round(maxL/2), maxL].map(v => {
    const y = yL(v);
    return `<text x="${(padL-4).toFixed(1)}" y="${(y+3).toFixed(1)}" font-size="9" fill="var(--accent)" text-anchor="end">${v}</text>`;
  }).join("");
  const yRightTicks = wqDef.length ? [minR, (minR+maxR)/2, maxR].map(v => {
    const y = yR(v);
    return `<text x="${(W-padR+4).toFixed(1)}" y="${(y+3).toFixed(1)}" font-size="9" fill="var(--warn)" text-anchor="start">${v.toFixed(1)}</text>`;
  }).join("") : "";

  svg.innerHTML = `${yLeftTicks}${yRightTicks}
    ${_timeAxisTicks(minX, maxX, xS, H-padB, data.hours || 24)}
    ${bars}${linePath}`;
}

let _lastSxwData = null;
function tickSxwParamOnly() {
  if (!_lastSxwData) return;
  const param = document.getElementById("chart-sxw-param")?.value || "water_temp_c";
  renderSightingsXWater(_lastSxwData, param);
}

function trendsActive() {
  const body = document.getElementById("trends-body");
  return body && !body.classList.contains("collapsed");
}

async function tickTrends() {
  // Skip work entirely when the panel is collapsed.
  if (!trendsActive()) return;
  const hours = parseInt(document.getElementById("trends-window")?.value || "24", 10);
  const param = document.getElementById("chart-sxw-param")?.value || "water_temp_c";

  // Snapshot first (returning visitors get instant content)
  const cdr  = readCached(`/api/charts/detection_rate?hours=${hours}`);
  const cts  = readCached(`/api/charts/top_species_timeseries?hours=${hours}&top_n=8`);
  const csxw = readCached(`/api/charts/sightings_x_water?hours=${hours}`);
  if (cdr)  renderDetectionRate(cdr.data);
  if (cts)  renderTopSpeciesTimeseries(cts.data);
  if (csxw) { _lastSxwData = csxw.data; renderSightingsXWater(csxw.data, param); }

  try {
    const [dr, ts, sxw] = await Promise.all([
      pollWithCache(`/api/charts/detection_rate?hours=${hours}`).catch(() => null),
      pollWithCache(`/api/charts/top_species_timeseries?hours=${hours}&top_n=8`).catch(() => null),
      pollWithCache(`/api/charts/sightings_x_water?hours=${hours}`).catch(() => null),
    ]);
    if (dr)  renderDetectionRate(dr);
    if (ts)  renderTopSpeciesTimeseries(ts);
    if (sxw) { _lastSxwData = sxw; renderSightingsXWater(sxw, param); }
  } catch {}
}

function initTrendsToggle() {
  const btn  = document.getElementById("trends-toggle");
  const body = document.getElementById("trends-body");
  if (!btn || !body) return;
  // Restore previous collapse state.
  const saved = localStorage.getItem("wb_trends_hidden") === "1";
  if (saved) { body.classList.add("collapsed"); btn.textContent = "show"; }
  btn.addEventListener("click", () => {
    const collapsed = body.classList.toggle("collapsed");
    btn.textContent = collapsed ? "show" : "hide";
    localStorage.setItem("wb_trends_hidden", collapsed ? "1" : "0");
    if (!collapsed) tickTrends();
  });
  // Handlers for the in-panel controls. Both must be bound exactly once,
  // here, so they fire reliably (the SxW param dropdown was previously
  // attached inside a different init path that didn't run reliably).
  document.getElementById("trends-window")?.addEventListener("change", tickTrends);
  document.getElementById("chart-sxw-param")?.addEventListener("change", tickSxwParamOnly);
}

// ====================================================================
// Reports panel — Katie's Power BI mockup pages rendered live.
// Tabs: Overview · Fish Explorer (placeholder) · Water & Weather ·
// Insights · Data Notes. Each tab fetches what it needs lazily; the
// fetches use the same SWR cache as the rest of the dashboard.
// ====================================================================

function reportsActive() {
  const body = document.getElementById("reports-body");
  return body && !body.classList.contains("collapsed");
}

function activeReportsTab() {
  return document.querySelector(".reports-tab.active")?.dataset.tab || "overview";
}

function reportsHours() {
  return parseInt(document.getElementById("reports-window")?.value || "24", 10);
}

// ---------- shared helpers ----------

function _scatterChart(svg, points, opts) {
  if (!svg) return;
  const W = 480, H = 200, padL = 36, padR = 10, padT = 8, padB = 22;
  const pts = points.filter(p => p.x != null && p.y != null);
  if (pts.length < 2) {
    svg.innerHTML = `<text x="240" y="100" font-size="11" fill="var(--muted)" text-anchor="middle">not enough data yet</text>`;
    return;
  }
  const xs = pts.map(p => p.x), ys = pts.map(p => p.y);
  const xLo = Math.min(...xs), xHi = Math.max(...xs);
  const yHi = Math.max(...ys, 1);
  const xRange = (xHi - xLo) || 1;
  const xS = v => padL + ((v - xLo) / xRange) * (W - padL - padR);
  const yS = v => H - padB - (v / yHi) * (H - padT - padB);

  const dots = pts.map(p =>
    `<circle cx="${xS(p.x).toFixed(1)}" cy="${yS(p.y).toFixed(1)}" r="2.2" fill="var(--accent)" opacity="0.7"/>`
  ).join("");

  const yTicks = [0, Math.round(yHi / 2), yHi].map(v => {
    const y = yS(v);
    return `<line x1="${padL}" x2="${W - padR}" y1="${y.toFixed(1)}" y2="${y.toFixed(1)}" stroke="var(--muted)" stroke-width="0.3" opacity="0.35"/>
            <text x="${(padL - 4).toFixed(1)}" y="${(y + 3).toFixed(1)}" font-size="9" fill="var(--muted)" text-anchor="end">${v}</text>`;
  }).join("");

  const xTickVals = [xLo, (xLo + xHi) / 2, xHi];
  const xTicks = xTickVals.map(v => {
    const x = xS(v);
    return `<text x="${x.toFixed(1)}" y="${(H - 8).toFixed(1)}" font-size="9" fill="var(--muted)" text-anchor="middle">${(opts?.xfmt || (n => n.toFixed(1)))(v)}</text>`;
  }).join("");

  const axisLabels = `
    <text x="${(W / 2).toFixed(1)}" y="${(H - 1).toFixed(1)}" font-size="9" fill="var(--muted)" text-anchor="middle">${opts?.xlabel || ""}</text>
    <text x="10" y="${(padT + 8).toFixed(1)}" font-size="9" fill="var(--muted)">${opts?.ylabel || ""}</text>
  `;

  svg.innerHTML = `${yTicks}${xTicks}${dots}${axisLabels}`;
}

// Classic meteorological wind rose. `samples` is an array of
// {dir_deg, speed_mph} pairs over the time window. Renders stacked bands
// per sector with a percentage radial scale (matches the canonical NOAA /
// CHIWindrose style). Wind below 2 mph is treated as "Calm" and shown as
// a labeled center disc rather than as a sector wedge — calm air has no
// meaningful direction. Direction convention is *meteorological*: each
// sector points to where the wind is coming FROM (0° = north, clockwise).
function _windRose(svg, samples, opts = {}) {
  if (!svg) return;
  const W = 420, H = 420;
  const cx = W / 2, cy = H / 2;
  const maxR = Math.min(W, H) / 2 - 44;   // outer wedge edge
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);

  const cleaned = (samples || []).filter(s =>
    s && Number.isFinite(s.dir_deg) && Number.isFinite(s.speed_mph),
  );
  if (cleaned.length === 0) {
    svg.innerHTML = `<text x="${cx}" y="${cy}" text-anchor="middle" fill="var(--muted)" font-size="12">no wind data</text>`;
    return;
  }

  const N_SECTORS = 16;
  const sectorDeg = 360 / N_SECTORS;
  // Cool-to-warm speed gradient (blue → cyan → green → yellow → orange → red)
  // — matches the standard wind-rose color convention so anyone who's seen
  // a Chicago / NOAA wind rose recognises this at a glance. "Calm" (< 2 mph)
  // gets its own dedicated center disc rather than a band, since calm air
  // has no meaningful direction.
  const CALM_MAX = 2;
  const BANDS = [
    { label: "2–5 mph",   max: 5,        fill: "#1f4ed8" }, // blue
    { label: "5–7 mph",   max: 7,        fill: "#06b6d4" }, // cyan
    { label: "7–10 mph",  max: 10,       fill: "#22c55e" }, // green
    { label: "10–15 mph", max: 15,       fill: "#eab308" }, // yellow
    { label: "15–20 mph", max: 20,       fill: "#f97316" }, // orange
    { label: "20+ mph",   max: Infinity, fill: "#dc2626" }, // red
  ];

  // counts[sector][band] = number of non-calm samples
  const counts = Array.from({ length: N_SECTORS }, () => Array(BANDS.length).fill(0));
  let calmCount = 0;
  for (const s of cleaned) {
    if (s.speed_mph < CALM_MAX) { calmCount += 1; continue; }
    const sec = Math.floor(((s.dir_deg % 360 + 360) % 360 + sectorDeg / 2) / sectorDeg) % N_SECTORS;
    const band = BANDS.findIndex(b => s.speed_mph < b.max);
    counts[sec][band >= 0 ? band : BANDS.length - 1] += 1;
  }

  const total = cleaned.length;
  const calmPct = (calmCount / total) * 100;
  const sectorPct = counts.map(row => row.map(c => (c / total) * 100));
  const sectorTotalPct = sectorPct.map(row => row.reduce((a, c) => a + c, 0));
  const maxSectorPct = Math.max(0.5, ...sectorTotalPct);

  // Pick a "nice" radial step (0.5% / 1% / 2% / 5%) so the rings carry
  // round-number percentage labels. Aim for 4-5 rings within the scale.
  const niceSteps = [0.25, 0.5, 1, 2, 2.5, 5, 10, 20, 25];
  const ringStep = niceSteps.find(s => maxSectorPct / s <= 5) || 25;
  const scaleMaxPct = Math.ceil(maxSectorPct / ringStep) * ringStep;
  const nRings = Math.round(scaleMaxPct / ringStep);

  // SVG angle convention: 0° at 3 o'clock, clockwise positive. Convert
  // meteorological compass-degree (0° = N = 12 o'clock) by subtracting 90°.
  const toRad = (deg) => (deg - 90) * Math.PI / 180;
  const pointOnCircle = (r, deg) => [cx + r * Math.cos(toRad(deg)), cy + r * Math.sin(toRad(deg))];

  // Inner "Calm" disc — separate visual element with its own percentage.
  // Sized to ~14% of maxR so it doesn't crowd the stacked rings.
  const discR = maxR * 0.14;
  const centerDisc = `
    <circle cx="${cx}" cy="${cy}" r="${discR.toFixed(1)}" fill="rgba(31, 78, 216, 0.18)" stroke="#1f4ed8" stroke-width="1"/>
    <text x="${cx}" y="${(cy - 4).toFixed(1)}" text-anchor="middle" font-size="11" fill="var(--muted)">Calm</text>
    <text x="${cx}" y="${(cy + 11).toFixed(1)}" text-anchor="middle" font-size="13" font-weight="600" fill="var(--ink)">${calmPct.toFixed(1)}%</text>
  `;

  // Concentric reference rings (every ringStep %) with percentage labels
  // along the N–NNE boundary (11.25°) — empty of wedges by construction.
  const ringPieces = [];
  for (let k = 1; k <= nRings; k++) {
    const pct = k * ringStep;
    const r = discR + (maxR - discR) * (pct / scaleMaxPct);
    ringPieces.push(`<circle cx="${cx}" cy="${cy}" r="${r.toFixed(1)}" fill="none" stroke="rgba(255,255,255,0.08)" stroke-width="1"/>`);
    const [lx, ly] = pointOnCircle(r, 11.25);
    ringPieces.push(`<text x="${lx.toFixed(1)}" y="${ly.toFixed(1)}" font-size="9" fill="var(--muted)" text-anchor="start" dx="2" dy="3">${pct.toFixed(ringStep < 1 ? 2 : (ringStep < 2 ? 1 : 0))}%</text>`);
  }

  // 8 cardinal direction labels (N/NE/E/SE/S/SW/W/NW). Skip the 16-point
  // intermediate compass letters — too cluttered in a 360px-class chart.
  const cardLabels = [
    { deg: 0,   text: "N"  }, { deg: 45,  text: "NE" },
    { deg: 90,  text: "E"  }, { deg: 135, text: "SE" },
    { deg: 180, text: "S"  }, { deg: 225, text: "SW" },
    { deg: 270, text: "W"  }, { deg: 315, text: "NW" },
  ].map(({ deg, text }) => {
    const [x, y] = pointOnCircle(maxR + 20, deg);
    return `<text x="${x.toFixed(1)}" y="${(y + 4).toFixed(1)}" text-anchor="middle" font-size="12" font-weight="600" fill="var(--ink)">${text}</text>`;
  }).join("");

  // Stacked bands per sector, starting from the calm-disc edge outward.
  // Bands stack inside→out lightest→strongest, so the outermost band of a
  // sector's stack is the fastest wind speed seen from that direction;
  // the stack's total height is the sector's % of all samples, scaled to
  // the same percentage axis as the reference rings (so "the moderate
  // band reached the outer ring" can't happen — the outer ring is a
  // fixed-percent cap above max sector total).
  const wedges = [];
  const tipLabels = [];
  for (let i = 0; i < N_SECTORS; i++) {
    if (!sectorTotalPct[i]) continue;
    const a1 = i * sectorDeg - sectorDeg / 2 + 0.4;
    const a2 = i * sectorDeg + sectorDeg / 2 - 0.4;
    let prevR = discR;
    for (let b = 0; b < BANDS.length; b++) {
      const p = sectorPct[i][b];
      if (!p) continue;
      const bandR = prevR + (maxR - discR) * (p / scaleMaxPct);
      const [p1x, p1y] = pointOnCircle(prevR, a1);
      const [p2x, p2y] = pointOnCircle(prevR, a2);
      const [p3x, p3y] = pointOnCircle(bandR, a2);
      const [p4x, p4y] = pointOnCircle(bandR, a1);
      const d = `M ${p1x.toFixed(1)} ${p1y.toFixed(1)} ` +
                `A ${prevR.toFixed(1)} ${prevR.toFixed(1)} 0 0 1 ${p2x.toFixed(1)} ${p2y.toFixed(1)} ` +
                `L ${p3x.toFixed(1)} ${p3y.toFixed(1)} ` +
                `A ${bandR.toFixed(1)} ${bandR.toFixed(1)} 0 0 0 ${p4x.toFixed(1)} ${p4y.toFixed(1)} Z`;
      const tip = `${labels16(i)} · ${BANDS[b].label} · ${p.toFixed(2)}% (${counts[i][b]} samples)`;
      wedges.push(`<path class="wr-slice" d="${d}" fill="${BANDS[b].fill}" stroke="rgba(11,26,36,0.55)" stroke-width="0.4" data-tip="${tip.replace(/"/g, "&quot;")}"></path>`);
      prevR = bandR;
    }
    // Per-sector tip label (sector total %), only for sectors with a meaningful share.
    if (sectorTotalPct[i] >= ringStep) {
      const [tx, ty] = pointOnCircle(prevR + 6, i * sectorDeg);
      tipLabels.push(`<text x="${tx.toFixed(1)}" y="${(ty + 3).toFixed(1)}" font-size="9" fill="var(--muted)" text-anchor="middle">${sectorTotalPct[i].toFixed(1)}%</text>`);
    }
  }

  // Six-band legend in a single row at the bottom. Each item is compact.
  const legendY = H - 6;
  const legendItemWidth = (W - 16) / BANDS.length;
  const legendItems = BANDS.map((b, i) => {
    const x = 8 + i * legendItemWidth;
    return `<rect x="${x.toFixed(1)}" y="${legendY - 9}" width="10" height="10" rx="2" fill="${b.fill}"/>` +
           `<text x="${(x + 13).toFixed(1)}" y="${legendY}" font-size="10" fill="var(--muted)">${b.label}</text>`;
  }).join("");

  svg.innerHTML = `${ringPieces.join("")}${wedges.join("")}${centerDisc}${tipLabels.join("")}${cardLabels}${legendItems}`;
  _attachWindRoseTooltip(svg);
}

// 16-sector compass label (N, NNE, NE, ENE, E, ...)
function labels16(idx) {
  return ["N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSW","SW","WSW","W","WNW","NW","NNW"][idx % 16];
}

// Wind barbs strip: chronological view to complement the wind rose. One
// arrow per time bucket along a horizontal axis, pointing in the direction
// the wind is *blowing toward* (most intuitive for a general audience —
// "wind moving east → arrow east"). Shaft length + color encode speed using
// the same Beaufort-ish bands as the rose so the two charts read together.
function _windBarbs(svg, samples, opts = {}) {
  if (!svg) return;
  const W = 480, H = 120, padL = 36, padR = 10, padT = 14, padB = 22;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.removeAttribute("preserveAspectRatio");

  const cleaned = (samples || []).filter(s =>
    s && Number.isFinite(s.x) && Number.isFinite(s.dir_deg) && Number.isFinite(s.speed_mph),
  );
  if (cleaned.length === 0) {
    svg.innerHTML = `<text x="${W / 2}" y="${H / 2}" text-anchor="middle" fill="var(--muted)" font-size="12">no wind data</text>`;
    return;
  }

  const xs = cleaned.map(s => s.x);
  const xMin = Math.min(...xs), xMax = Math.max(...xs);
  const xSpan = xMax - xMin || 1;
  const innerW = W - padL - padR;
  const cy = padT + (H - padT - padB) / 2;
  const speedMax = Math.max(5, ...cleaned.map(s => s.speed_mph));
  const maxArrowLen = (H - padT - padB) / 2 - 2;

  // Same mph thresholds as the wind rose so the two charts read as one.
  const BANDS = [
    { max: 5,        fill: "#1e4a6e" },
    { max: 12,       fill: "#3a8fc7" },
    { max: 24,       fill: "#f5b941" },
    { max: Infinity, fill: "#e0533d" },
  ];
  const bandColor = (sp) => BANDS.find(b => sp < b.max).fill;

  // Subsample down to ~64 ticks for legibility on long windows.
  const stride = Math.max(1, Math.ceil(cleaned.length / 64));
  const arrows = [];
  for (let i = 0; i < cleaned.length; i += stride) {
    const s = cleaned[i];
    const x = padL + (innerW * (s.x - xMin)) / xSpan;
    const len = (Math.min(s.speed_mph, speedMax) / speedMax) * maxArrowLen + 4;
    // Convention: arrow points where the wind is *going*, which is opposite
    // of the meteorological "from" direction. dir_deg is "from" — flip 180°.
    const headDeg = (s.dir_deg + 180) % 360;
    const color = bandColor(s.speed_mph);
    const tip = `${new Date(s.x).toLocaleString()}\n${labels16(Math.floor(((s.dir_deg % 360 + 360) % 360 + 11.25) / 22.5) % 16)} (${s.dir_deg.toFixed(0)}°) · ${s.speed_mph.toFixed(1)} mph`;
    // Path of an arrow that points "up" (negative-y), with shaft length `len`:
    // - shaft: (0,0) to (0, -len)
    // - head:  short triangle at (0, -len)
    const headHalf = 3.5;
    const headLen = 5;
    const d = `M 0 0 L 0 ${(-len + headLen).toFixed(1)} M ${-headHalf} ${(-len + headLen).toFixed(1)} L 0 ${(-len).toFixed(1)} L ${headHalf} ${(-len + headLen).toFixed(1)}`;
    arrows.push(
      `<g class="wb-barb" transform="translate(${x.toFixed(1)} ${cy}) rotate(${headDeg.toFixed(1)})" ` +
      `data-tip="${tip.replace(/"/g, "&quot;").replace(/\n/g, "&#10;")}">` +
      `<path d="${d}" stroke="${color}" stroke-width="1.4" fill="none" stroke-linecap="round"/>` +
      `</g>`,
    );
  }

  // X-axis ticks (time labels at start, mid, end)
  const fmtT = (ms) => {
    const d = new Date(ms);
    return `${d.getMonth() + 1}/${d.getDate()} ${d.getHours().toString().padStart(2, "0")}:${d.getMinutes().toString().padStart(2, "0")}`;
  };
  const tickXs = [xMin, xMin + xSpan / 2, xMax];
  const xTicks = tickXs.map(t => {
    const x = padL + (innerW * (t - xMin)) / xSpan;
    return `<text x="${x.toFixed(1)}" y="${(H - 4).toFixed(1)}" font-size="9" fill="var(--muted)" text-anchor="middle">${fmtT(t)}</text>`;
  }).join("");
  const baseline = `<line x1="${padL}" y1="${cy}" x2="${W - padR}" y2="${cy}" stroke="rgba(255,255,255,0.06)" stroke-width="1"/>`;

  svg.innerHTML = `${baseline}${arrows.join("")}${xTicks}`;
  _attachWindRoseTooltip(svg); // re-use the same hover-tooltip mechanism
}

// Cursor-following HTML tooltip for the wind-rose wedges. Falls back to a
// no-op when the body already has a `.wb-tip` element (idempotent across
// re-renders). Listens on the SVG itself rather than attaching per-path
// handlers so re-rendering the rose doesn't accumulate stale listeners.
function _attachWindRoseTooltip(svg) {
  if (!svg || svg.dataset.tipBound) return;
  svg.dataset.tipBound = "1";
  let tipEl = document.getElementById("wb-tip");
  if (!tipEl) {
    tipEl = document.createElement("div");
    tipEl.id = "wb-tip";
    tipEl.className = "wb-tip";
    document.body.appendChild(tipEl);
  }
  svg.addEventListener("mousemove", (e) => {
    // Walk up from the event target — barbs sit inside a <g class="wb-barb">
    // so the path itself doesn't carry the data-tip attribute.
    let node = e.target;
    while (node && node !== svg && !node.dataset?.tip) node = node.parentNode;
    if (node && node !== svg && node.dataset?.tip) {
      tipEl.innerHTML = node.dataset.tip.replace(/\n/g, "<br>");
      tipEl.style.left = (e.clientX + 12) + "px";
      tipEl.style.top  = (e.clientY + 12) + "px";
      tipEl.classList.add("visible");
    } else {
      tipEl.classList.remove("visible");
    }
  });
  svg.addEventListener("mouseleave", () => tipEl.classList.remove("visible"));
}

function _miniLine(svg, points, opts) {
  if (!svg) return;
  const W = 480, H = 140, padL = 36, padR = 10, padT = 8, padB = 18;
  const pts = points.filter(p => p.y != null);
  if (pts.length < 2) {
    svg.innerHTML = `<text x="240" y="70" font-size="11" fill="var(--muted)" text-anchor="middle">collecting…</text>`;
    return;
  }
  const xs = pts.map(p => p.x), ys = pts.map(p => p.y);
  const xLo = Math.min(...xs), xHi = Math.max(...xs);
  const yLo = Math.min(...ys), yHi = Math.max(...ys);
  const xRange = (xHi - xLo) || 1;
  const yRange = (yHi - yLo) || 1;
  const xS = v => padL + ((v - xLo) / xRange) * (W - padL - padR);
  const yS = v => H - padB - ((v - yLo) / yRange) * (H - padT - padB);

  const path = "M " + pts.map(p => `${xS(p.x).toFixed(1)},${yS(p.y).toFixed(1)}`).join(" L ");
  const fmt = opts?.fmt || (n => n.toFixed(1));
  const yLabels = `
    <text x="${(padL - 4).toFixed(1)}" y="${(padT + 8).toFixed(1)}" font-size="9" fill="var(--muted)" text-anchor="end">${fmt(yHi)}</text>
    <text x="${(padL - 4).toFixed(1)}" y="${(H - padB + 3).toFixed(1)}" font-size="9" fill="var(--muted)" text-anchor="end">${fmt(yLo)}</text>
  `;
  const last = ys[ys.length - 1];
  const lastLabel = `<text x="${(W - padR - 1).toFixed(1)}" y="${(padT + 8).toFixed(1)}" font-size="10" fill="var(--accent)" text-anchor="end" font-weight="600">${fmt(last)}${opts?.unit ? " " + opts.unit : ""}</text>`;

  svg.innerHTML = `${yLabels}${lastLabel}
    <path d="${path}" stroke="var(--accent)" stroke-width="1.4" fill="none"/>`;
}

// ---------- Tab switching ----------

function initReportsToggle() {
  const btn = document.getElementById("reports-toggle");
  const body = document.getElementById("reports-body");
  if (btn && body) {
    const saved = localStorage.getItem("wb_reports_hidden") === "1";
    if (saved) { body.classList.add("collapsed"); btn.textContent = "show"; }
    btn.addEventListener("click", () => {
      const collapsed = body.classList.toggle("collapsed");
      btn.textContent = collapsed ? "show" : "hide";
      localStorage.setItem("wb_reports_hidden", collapsed ? "1" : "0");
      if (!collapsed) tickReports();
    });
  }

  // Tab switching
  document.querySelectorAll(".reports-tab").forEach(t => {
    t.addEventListener("click", () => {
      document.querySelectorAll(".reports-tab").forEach(x => x.classList.remove("active"));
      document.querySelectorAll(".reports-tab-panel").forEach(x => x.classList.remove("active"));
      t.classList.add("active");
      document.getElementById("tab-" + t.dataset.tab)?.classList.add("active");
      localStorage.setItem("wb_reports_tab", t.dataset.tab);
      tickReports();
    });
  });
  // Restore active tab. Migrate the legacy "water-weather" tab key for
  // anyone who had it saved before we split it in two — land them on the
  // Water tab, which is the closer mental match for the previous content.
  let saved = localStorage.getItem("wb_reports_tab");
  if (saved === "water-weather") {
    saved = "water";
    localStorage.setItem("wb_reports_tab", saved);
  }
  if (saved) {
    const target = document.querySelector(`.reports-tab[data-tab="${saved}"]`);
    if (target) target.click();
  }

  document.getElementById("reports-window")?.addEventListener("change", tickReports);
}

// ---------- Render: Overview ----------

let _cameraMeta = null;
async function _loadCameraMeta() {
  if (_cameraMeta) return _cameraMeta;
  try {
    const r = await fetch("/api/export/camera_metadata.json", { cache: "force-cache" });
    if (!r.ok) return null;
    _cameraMeta = await r.json();
    return _cameraMeta;
  } catch { return null; }
}

let _leafletMap = null;
function _renderMap(sources) {
  const el = document.getElementById("ov-map");
  if (!el) return;
  if (typeof L === "undefined") {
    // Leaflet still loading — try again on the next tick.
    setTimeout(() => _renderMap(sources), 250);
    return;
  }
  if (!sources || !Object.keys(sources).length) {
    el.innerHTML = `<div style="padding:20px;color:var(--muted);font-size:12px;text-align:center">no camera metadata</div>`;
    return;
  }
  const pts = Object.entries(sources)
    .filter(([_, v]) => v.lat != null && v.lng != null)
    .map(([k, v]) => ({ key: k, name: v.display_name || k, lat: v.lat, lng: v.lng }));
  if (pts.length === 0) { el.innerHTML = `<div style="padding:20px;color:var(--muted)">no coordinates in camera_metadata.json</div>`; return; }

  // First call: create the map. Subsequent calls reuse it.
  if (!_leafletMap || !el.contains(_leafletMap.getContainer())) {
    el.innerHTML = "";
    _leafletMap = L.map(el, {
      attributionControl: true,
      zoomControl: true,
      // Dark-theme-friendly defaults
      preferCanvas: true,
    });
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 18,
      attribution: '&copy; <a href="https://openstreetmap.org/copyright">OpenStreetMap</a>',
    }).addTo(_leafletMap);
  }

  // Drop any existing markers and re-add (cheap; only ever 2 markers).
  _leafletMap.eachLayer(layer => { if (layer instanceof L.Marker) _leafletMap.removeLayer(layer); });
  for (const p of pts) {
    L.marker([p.lat, p.lng])
      .addTo(_leafletMap)
      .bindPopup(`<strong>${p.name}</strong><br><small>${p.lat.toFixed(5)}, ${p.lng.toFixed(5)}</small>`);
  }

  if (pts.length === 1) {
    _leafletMap.setView([pts[0].lat, pts[0].lng], 14);
  } else {
    const bounds = L.latLngBounds(pts.map(p => [p.lat, p.lng]));
    _leafletMap.fitBounds(bounds, { padding: [30, 30], maxZoom: 15 });
  }
  // Some layouts (display:none → block) leave Leaflet sized to 0 until
  // invalidateSize() is called.
  setTimeout(() => _leafletMap.invalidateSize(), 50);
}

async function renderOverviewTab(hours) {
  // KPI tiles + fish-over-time chart + map + top species + WQ snapshot.
  const [visitor, detRate, wqLatest, wqHist] = await Promise.all([
    pollWithCache(`/api/visitor_stats?hours=${hours}`).catch(() => null),
    pollWithCache(`/api/charts/detection_rate?hours=${hours}`).catch(() => null),
    pollWithCache("/api/water_quality/latest").catch(() => null),
    pollWithCache(`/api/water_quality/history?hours=${hours}&max_points=120`).catch(() => null),
  ]);
  const camMeta = await _loadCameraMeta();

  // KPIs
  const totals = visitor?.totals || {};
  const wq = wqLatest || visitor?.water_quality || {};
  const setText = (id, txt) => { const e = document.getElementById(id); if (e) e.textContent = txt; };
  setText("ov-kpi-sightings", totals.sightings ?? "—");
  setText("ov-kpi-species", totals.unique_species ?? "—");
  setText("ov-kpi-temp", wq.water_temp_c != null ? (wq.water_temp_c * 9/5 + 32).toFixed(1) + "°F" : "—");
  setText("ov-kpi-do", wq.do_pct != null ? wq.do_pct.toFixed(0) + "%" : "—");
  setText("ov-kpi-clarity", wq.turbidity_fnu != null ? classifyTurbidity(wq.turbidity_fnu)[0] : "—");

  // Fish observations over time (single line)
  const obsSvg = document.getElementById("ov-chart-obs");
  if (obsSvg && detRate?.series?.length) {
    const points = detRate.series.map(p => ({
      x: new Date(p.hour).getTime(),
      y: p.sightings,
    }));
    _miniLine(obsSvg, points, { unit: "fish/hr", fmt: n => n.toFixed(0) });
  } else if (obsSvg) {
    obsSvg.innerHTML = `<text x="240" y="70" font-size="11" fill="var(--muted)" text-anchor="middle">collecting…</text>`;
  }

  // Map of camera sites
  _renderMap(camMeta?.sources);

  // Top species (reuse the reef explorer's renderer signature)
  const topEl = document.getElementById("ov-top-species");
  if (topEl) {
    const rows = (visitor?.top_species || []).slice(0, 6).map(s => ({
      latin: s.latin,
      common: s.common,
      sightings: s.sightings,
    }));
    if (rows.length === 0) {
      topEl.innerHTML = `<div class="empty" style="font-size:12px">no sightings yet</div>`;
    } else {
      const max = Math.max(...rows.map(r => r.sightings), 1);
      topEl.innerHTML = rows.map(r => {
        const pct = (100 * r.sightings / max).toFixed(1);
        const display = speciesDisplayHtml(r.latin, r.common);
        return `<div class="reef-top-row">
          <div class="reef-bar-wrap"><div class="reef-bar" style="width:${pct}%"></div><div class="reef-name">${display}</div></div>
          <div class="reef-count">${r.sightings}</div>
        </div>`;
      }).join("");
    }
  }

  // Water quality snapshot trend — water temp line in °F
  const wqSvg = document.getElementById("ov-wq-trend");
  if (wqSvg && wqHist?.series?.length) {
    const points = wqHist.series
      .filter(r => r.water_temp_c != null)
      .map(r => ({ x: new Date(r.bucket).getTime(), y: r.water_temp_c * 9/5 + 32 }));
    _miniLine(wqSvg, points, { unit: "°F", fmt: n => n.toFixed(1) });
  }
}

// ---------- Render: Water tab (sonde) ----------

async function renderWaterTab(hours) {
  const [wqLatest, wqHist] = await Promise.all([
    pollWithCache("/api/water_quality/latest").catch(() => null),
    pollWithCache(`/api/water_quality/history?hours=${hours}&max_points=120`).catch(() => null),
  ]);

  const setText = (id, txt) => { const e = document.getElementById(id); if (e) e.textContent = txt; };
  const f = (v, n=1) => v == null ? "—" : v.toFixed(n);

  if (wqLatest) {
    setText("wt-kpi-temp",   wqLatest.water_temp_c != null ? f(wqLatest.water_temp_c * 9/5 + 32, 1) : "—");
    setText("wt-kpi-do",     f(wqLatest.do_pct, 0));
    setText("wt-kpi-ph",     f(wqLatest.ph, 2));
    setText("wt-kpi-turb",   f(wqLatest.turbidity_fnu, 1));
    setText("wt-kpi-chl",    f(wqLatest.chlorophyll_rfu, 1));
    setText("wt-kpi-spcond", f(wqLatest.spcond_ms_cm, 1));
  }

  const mkLine = (id, points, opts) => _miniLine(document.getElementById(id), points, opts);
  if (wqHist?.series?.length) {
    const ts = wqHist.series.map(r => new Date(r.bucket).getTime());
    mkLine("ww-chart-wtemp", wqHist.series.map((r, i) => ({ x: ts[i], y: r.water_temp_c != null ? r.water_temp_c * 9/5 + 32 : null })), { unit: "°F", fmt: n => n.toFixed(1) });
    mkLine("ww-chart-do",    wqHist.series.map((r, i) => ({ x: ts[i], y: r.do_pct })),                                                  { unit: "%",  fmt: n => n.toFixed(0) });
    mkLine("ww-chart-ph",    wqHist.series.map((r, i) => ({ x: ts[i], y: r.ph })),                                                      { fmt: n => n.toFixed(2) });
    mkLine("ww-chart-turb",  wqHist.series.map((r, i) => ({ x: ts[i], y: r.turbidity_fnu })),                                           { unit: "FNU", fmt: n => n.toFixed(1) });
  }
}

// ---------- Render: Weather tab (Vaisala + Open-Meteo + NWS) ----------

async function renderWeatherTab(hours) {
  const [wxLatest, wxHist] = await Promise.all([
    pollWithCache("/api/weather/latest").catch(() => null),
    pollWithCache(`/api/weather/history?hours=${hours}&max_points=120`).catch(() => null),
  ]);

  const setText = (id, txt) => { const e = document.getElementById(id); if (e) e.textContent = txt; };
  const f = (v, n=1) => v == null ? "—" : v.toFixed(n);

  if (wxLatest) {
    setText("ww-kpi-air",      wxLatest.air_temp_c != null ? f(wxLatest.air_temp_c * 9/5 + 32, 1) + " °F" : "—");
    setText("ww-kpi-wind",     wxLatest.wind_speed_avg_ms != null ? f(wxLatest.wind_speed_avg_ms * 2.23694, 1) + " mph" : "—");
    setText("ww-kpi-solar",    f(wxLatest.solar_rad_wm2, 0));
    setText("ww-kpi-pressure", wxLatest.bar_press_hpa != null ? f(wxLatest.bar_press_hpa * 0.750064, 1) : "—");
    setText("ww-kpi-cloud",    wxLatest.cloud_cover_pct != null ? f(wxLatest.cloud_cover_pct, 0) + "%" : "—");
    setText("ww-kpi-cond",     conditionsText(wxLatest));
  }
  if (wxHist?.series?.length) {
    const rainTotalMm = wxHist.series.reduce((s, r) => s + (r.rain_accum_mm || 0), 0);
    setText("ww-kpi-rain", (rainTotalMm * 0.0393701).toFixed(2) + " in");
  } else {
    setText("ww-kpi-rain", "—");
  }

  // Six single-metric weather sparklines. Rain uses rain_int_mm_hr
  // (instantaneous rate) rather than rain_accum_mm (cumulative counter —
  // stays flat between rain events). Wind direction is plotted in raw
  // degrees-from-north; vertical jumps at the 359°↔0° wraparound are an
  // artifact of the angle representation, not real wind shifts.
  const mkLine = (id, points, opts) => _miniLine(document.getElementById(id), points, opts);
  if (wxHist?.series?.length) {
    const ts = wxHist.series.map(r => new Date(r.bucket).getTime());
    mkLine("ww-chart-air",     wxHist.series.map((r, i) => ({ x: ts[i], y: r.air_temp_c != null ? r.air_temp_c * 9/5 + 32 : null })),               { unit: "°F",   fmt: n => n.toFixed(1) });
    mkLine("ww-chart-wind",    wxHist.series.map((r, i) => ({ x: ts[i], y: r.wind_speed_avg_ms != null ? r.wind_speed_avg_ms * 2.23694 : null })), { unit: "mph",  fmt: n => n.toFixed(1) });
    mkLine("ww-chart-rain",    wxHist.series.map((r, i) => ({ x: ts[i], y: r.rain_int_mm_hr != null ? r.rain_int_mm_hr * 0.0393701 : null })),      { unit: "in/h", fmt: n => n.toFixed(2) });
    mkLine("ww-chart-solar",   wxHist.series.map((r, i) => ({ x: ts[i], y: r.solar_rad_wm2 })),                                                     { unit: "W/m²", fmt: n => n.toFixed(0) });
    mkLine("ww-chart-cloud",   wxHist.series.map((r, i) => ({ x: ts[i], y: r.cloud_cover_pct })),                                                   { unit: "%",    fmt: n => n.toFixed(0) });
    // Wind direction is a circular variable — a line graph wraps around at
    // 359°↔0° and tells you nothing about prevailing wind. Use a polar rose
    // for the windowed distribution; pair it with a wind-barbs strip for
    // the chronological view ("when did the wind shift?"). Both renderers
    // expect speed in mph (matches the KPI tile + speed line chart on the
    // same tab — sonde stays SI, surface UI is freedom units).
    const MS_TO_MPH = 2.23694;
    _windRose(
      document.getElementById("ww-chart-winddir"),
      wxHist.series.map(r => ({
        dir_deg: r.wind_dir_avg_deg,
        speed_mph: r.wind_speed_avg_ms != null ? r.wind_speed_avg_ms * MS_TO_MPH : null,
      })),
    );
    _windBarbs(
      document.getElementById("ww-chart-windbarbs"),
      wxHist.series.map((r, i) => ({
        x: ts[i],
        dir_deg: r.wind_dir_avg_deg,
        speed_mph: r.wind_speed_avg_ms != null ? r.wind_speed_avg_ms * MS_TO_MPH : null,
      })),
    );
  }
}

// ---------- Render: Insights ----------

async function renderInsightsTab(hours) {
  const [sxw, sxweather, visitor] = await Promise.all([
    pollWithCache(`/api/charts/sightings_x_water?hours=${hours}`).catch(() => null),
    pollWithCache(`/api/charts/sightings_x_weather?hours=${hours}`).catch(() => null),
    pollWithCache(`/api/visitor_stats?hours=${hours}`).catch(() => null),
  ]);

  const setText = (id, txt) => { const e = document.getElementById(id); if (e) e.textContent = txt; };
  const totals = visitor?.totals || {};
  setText("in-kpi-richness", totals.unique_species ?? "—");
  setText("in-kpi-count",    totals.sightings ?? "—");

  let rainTotalIn = null;
  if (sxweather?.series?.length) {
    const totalMm = sxweather.series.reduce((s, r) => s + (r.rain_accum_mm || 0), 0);
    rainTotalIn = totalMm * 0.0393701;
  }
  setText("in-kpi-rain", rainTotalIn != null ? rainTotalIn.toFixed(2) + " in" : "—");

  // Data completeness: fraction of hourly buckets in the window that have both fish + WQ data
  if (sxw?.series?.length) {
    const expected = hours; // hourly buckets
    const filled = sxw.series.filter(r => r.sightings != null && r.water_temp_c != null).length;
    const pct = Math.min(100, Math.round(100 * filled / Math.max(1, expected)));
    setText("in-kpi-completeness", pct + "%");
  } else {
    setText("in-kpi-completeness", "—");
  }

  // Scatter: rainfall (in) vs sightings
  _scatterChart(
    document.getElementById("in-scatter-rain"),
    (sxweather?.series || []).map(r => ({ x: (r.rain_accum_mm || 0) * 0.0393701, y: r.sightings || 0 })),
    { xlabel: "rain (in) per hour", ylabel: "sightings", xfmt: n => n.toFixed(2) },
  );

  // Scatter: pressure (mmHg) vs sightings
  _scatterChart(
    document.getElementById("in-scatter-pressure"),
    (sxweather?.series || [])
      .filter(r => r.bar_press_hpa != null)
      .map(r => ({ x: r.bar_press_hpa * 0.750064, y: r.sightings || 0 })),
    { xlabel: "barometric pressure (mmHg)", ylabel: "sightings", xfmt: n => n.toFixed(1) },
  );

  // Scatter: water temp (°F) vs sightings
  _scatterChart(
    document.getElementById("in-scatter-temp"),
    (sxw?.series || [])
      .filter(r => r.water_temp_c != null)
      .map(r => ({ x: r.water_temp_c * 9/5 + 32, y: r.sightings || 0 })),
    { xlabel: "water temperature (°F)", ylabel: "sightings", xfmt: n => n.toFixed(1) },
  );

  // Scatter: DO vs sightings (proxy for species richness with what we already export)
  _scatterChart(
    document.getElementById("in-scatter-do"),
    (sxw?.series || [])
      .filter(r => r.do_pct != null)
      .map(r => ({ x: r.do_pct, y: r.sightings || 0 })),
    { xlabel: "dissolved O₂ (% sat)", ylabel: "sightings", xfmt: n => n.toFixed(0) },
  );
}

// ---------- Render: Data Notes ----------

function renderDataNotesTab() {
  const el = document.getElementById("dn-refresh-ts");
  if (el) el.textContent = new Date().toLocaleString();
}

// ---------- Render: Fish Explorer ----------

const _feState = { sortBy: "sightings", sortDir: "desc", species: [] };

function _camLabel(src) {
  if (src === "seahivecam") return "SeaHIVE";
  if (src === "pier_cam") return "Pier";
  return src || "—";
}

function _feSortSpecies(arr) {
  const key = _feState.sortBy;
  const dir = _feState.sortDir === "desc" ? -1 : 1;
  return arr.slice().sort((a, b) => {
    let va = a[key], vb = b[key];
    if (key === "name") { va = (a.common || a.latin || "").toLowerCase(); vb = (b.common || b.latin || "").toLowerCase(); }
    if (key === "last_seen") { va = new Date(va || 0).getTime(); vb = new Date(vb || 0).getTime(); }
    if (va == null) va = -Infinity;
    if (vb == null) vb = -Infinity;
    return va < vb ? -dir : va > vb ? dir : 0;
  });
}

function _renderSpeciesTable() {
  const tbody = document.querySelector("#fe-species-table tbody");
  if (!tbody) return;
  const rows = _feSortSpecies(_feState.species);
  if (rows.length === 0) {
    tbody.innerHTML = `<tr><td colspan="5" class="empty">no sightings in window</td></tr>`;
    return;
  }
  tbody.innerHTML = rows.map(s => {
    const name = speciesDisplayHtml(s.latin, s.common);
    const acc = s.mean_accuracy != null ? (s.mean_accuracy * 100).toFixed(0) + "%" : "—";
    const cams = (s.sources || []).map(_camLabel).join(", ") || "—";
    const last = s.last_seen ? new Date(s.last_seen).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—";
    return `<tr data-species-id="${s.species_id}">
      <td>${name}</td>
      <td class="num">${s.sightings}</td>
      <td class="num">${acc}</td>
      <td>${cams}</td>
      <td class="num">${last}</td>
    </tr>`;
  }).join("");
  // sort-active header decoration
  document.querySelectorAll("#fe-species-table th.sortable").forEach(th => {
    th.classList.toggle("sort-active", th.dataset.sort === _feState.sortBy);
  });
}

function _renderHabitatMatrix(matrix) {
  const el = document.getElementById("fe-habitat-matrix");
  if (!el) return;
  const sources = matrix?.sources || [];
  const species = matrix?.species || [];
  if (!sources.length || !species.length) {
    el.innerHTML = `<div class="empty" style="font-size:12px;color:var(--muted)">collecting…</div>`;
    return;
  }
  // grid: 1 name col + N source cols
  el.style.gridTemplateColumns = `minmax(160px, 2fr) ${sources.map(() => "1fr").join(" ")}`;
  let html = `<div class="hm-row"><div class="hm-header">species</div>`;
  for (const s of sources) html += `<div class="hm-header" style="text-align:right">${_camLabel(s)}</div>`;
  html += `</div>`;
  // Max count for color scaling
  let max = 1;
  for (const sp of species) for (const v of Object.values(sp.counts || {})) if (v > max) max = v;
  for (const sp of species) {
    const name = speciesDisplayHtml(sp.latin, sp.common);
    html += `<div class="hm-row"><div class="hm-name">${name}</div>`;
    for (const s of sources) {
      const v = sp.counts?.[s] || 0;
      const intensity = Math.min(1, v / max);
      const bg = `rgba(79, 193, 255, ${(0.06 + 0.55 * intensity).toFixed(3)})`;
      html += `<div class="hm-cell" style="background:${bg}">${v || ""}</div>`;
    }
    html += `</div>`;
  }
  el.innerHTML = html;
}

async function renderFishExplorerTab(hours) {
  const [speciesList, matrix, detRate] = await Promise.all([
    pollWithCache(`/api/species/list?hours=${hours}`).catch(() => null),
    pollWithCache(`/api/species/habitat_matrix?hours=${hours}&top_n=15`).catch(() => null),
    pollWithCache(`/api/charts/detection_rate?hours=${hours}`).catch(() => null),
  ]);

  const list = speciesList?.species || [];
  _feState.species = list;

  // KPIs
  const setText = (id, txt) => { const e = document.getElementById(id); if (e) e.textContent = txt; };
  setText("fe-kpi-species", list.length);
  const totalSightings = list.reduce((s, x) => s + (x.sightings || 0), 0);
  setText("fe-kpi-sightings", totalSightings);
  const rare = list.filter(x => (x.sightings || 0) <= 3).length;
  setText("fe-kpi-rare", rare);
  // Top camera = source_name appearing most often across sightings
  const camCounts = {};
  for (const s of list) {
    for (const src of (s.sources || [])) {
      camCounts[src] = (camCounts[src] || 0) + (s.sightings || 0);
    }
  }
  const topCam = Object.entries(camCounts).sort((a, b) => b[1] - a[1])[0];
  setText("fe-kpi-topcam", topCam ? _camLabel(topCam[0]) : "—");

  // Top species bar list — reuse the reef explorer renderer's style
  const topEl = document.getElementById("fe-top-species");
  if (topEl) {
    const top = list.slice(0, 8);
    if (top.length === 0) {
      topEl.innerHTML = `<div class="empty" style="font-size:12px">no sightings yet</div>`;
    } else {
      const max = Math.max(...top.map(r => r.sightings), 1);
      topEl.innerHTML = top.map(r => {
        const pct = (100 * r.sightings / max).toFixed(1);
        const display = speciesDisplayHtml(r.latin, r.common);
        return `<div class="reef-top-row">
          <div class="reef-bar-wrap"><div class="reef-bar" style="width:${pct}%"></div><div class="reef-name">${display}</div></div>
          <div class="reef-count">${r.sightings}</div>
        </div>`;
      }).join("");
    }
  }

  // Species richness over time — derive from detection_rate (one bucket = one hour;
  // we don't have hourly distinct species cheaply, so use a proxy: hourly sightings).
  const richSvg = document.getElementById("fe-chart-richness");
  if (richSvg && detRate?.series?.length) {
    const points = detRate.series.map(p => ({
      x: new Date(p.hour).getTime(),
      y: p.sightings,
    }));
    _miniLine(richSvg, points, { unit: "sightings/hr", fmt: n => n.toFixed(0) });
  }

  // Habitat matrix
  _renderHabitatMatrix(matrix);

  // Species table
  _renderSpeciesTable();
}

function _initFishExplorerInteractions() {
  // Header sort
  document.querySelectorAll("#fe-species-table th.sortable").forEach(th => {
    th.addEventListener("click", () => {
      const key = th.dataset.sort;
      if (_feState.sortBy === key) {
        _feState.sortDir = _feState.sortDir === "desc" ? "asc" : "desc";
      } else {
        _feState.sortBy = key;
        _feState.sortDir = (key === "name") ? "asc" : "desc";
      }
      _renderSpeciesTable();
    });
  });
  // Row click → drill-through modal (event delegation)
  const tbody = document.querySelector("#fe-species-table tbody");
  if (tbody) {
    tbody.addEventListener("click", (e) => {
      const tr = e.target.closest("tr[data-species-id]");
      if (!tr) return;
      openSpeciesModal(tr.dataset.speciesId);
    });
  }
}

// ---------- Species profile modal ----------

async function openSpeciesModal(speciesId) {
  const modal = document.getElementById("species-modal");
  if (!modal) return;
  modal.classList.remove("hidden");
  document.getElementById("sm-common").textContent = "loading…";
  document.getElementById("sm-latin").textContent = "";
  document.getElementById("sm-stats").textContent = "";
  document.getElementById("sm-frames").innerHTML = "";
  document.getElementById("sm-chart-hod").innerHTML = "";
  document.getElementById("sm-chart-acc").innerHTML = "";

  const hours = reportsHours();
  let data;
  try {
    data = await (await fetch(`/api/species/${encodeURIComponent(speciesId)}/profile?hours=${hours}`, { cache: "no-store" })).json();
  } catch {
    document.getElementById("sm-common").textContent = "failed to load";
    return;
  }

  document.getElementById("sm-common").textContent = data.common || "(no common name)";
  document.getElementById("sm-latin").textContent = data.latin ? `(${data.latin})` : "";
  const accMean = data.mean_accuracy != null ? (data.mean_accuracy * 100).toFixed(1) + "%" : "—";
  const accPeak = data.peak_accuracy != null ? (data.peak_accuracy * 100).toFixed(1) + "%" : "—";
  const camLabels = (data.sources || []).map(_camLabel).join(", ") || "—";
  const first = data.first_seen ? new Date(data.first_seen).toLocaleString() : "—";
  const last = data.last_seen ? new Date(data.last_seen).toLocaleString() : "—";
  document.getElementById("sm-stats").innerHTML =
    `<strong>${data.sightings || 0}</strong> sightings · ` +
    `<strong>${data.total_frames || 0}</strong> frames · ` +
    `mean acc ${accMean} · peak ${accPeak}<br/>` +
    `cameras: ${camLabels} · first: ${first} · last: ${last}`;

  // Hour-of-day bars
  const hod = data.hour_of_day || [];
  const hodSvg = document.getElementById("sm-chart-hod");
  if (hodSvg && hod.length) {
    const W = 480, H = 120, padL = 28, padR = 8, padT = 8, padB = 22;
    const maxN = Math.max(...hod.map(h => h.sightings), 1);
    const bw = (W - padL - padR) / 24;
    const bars = hod.map(h => {
      const x = padL + h.hour * bw + 1;
      const bh = ((h.sightings) / maxN) * (H - padT - padB);
      const y = H - padB - bh;
      return `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${(bw - 2).toFixed(1)}" height="${Math.max(0, bh).toFixed(1)}" fill="var(--accent)" opacity="0.65"/>`;
    }).join("");
    const ticks = [0, 6, 12, 18].map(h => {
      const x = padL + h * bw + bw / 2;
      return `<text x="${x.toFixed(1)}" y="${(H - 6).toFixed(1)}" font-size="9" fill="var(--muted)" text-anchor="middle">${h}h</text>`;
    }).join("");
    const peakLabel = `<text x="${(padL - 4).toFixed(1)}" y="${(padT + 8).toFixed(1)}" font-size="9" fill="var(--muted)" text-anchor="end">peak ${maxN}</text>`;
    hodSvg.innerHTML = `${peakLabel}${bars}${ticks}`;
  }

  // Accuracy distribution bars
  const acc = data.accuracy_bins || [];
  const accSvg = document.getElementById("sm-chart-acc");
  if (accSvg && acc.length) {
    const W = 480, H = 120, padL = 36, padR = 8, padT = 8, padB = 22;
    const labels = acc.map(a => a.bucket);
    const counts = acc.map(a => a.n);
    const maxN = Math.max(...counts, 1);
    const bw = (W - padL - padR) / labels.length;
    const bars = counts.map((c, i) => {
      const x = padL + i * bw + 4;
      const bh = (c / maxN) * (H - padT - padB);
      const y = H - padB - bh;
      return `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${(bw - 8).toFixed(1)}" height="${Math.max(0, bh).toFixed(1)}" fill="var(--accent)" opacity="0.65"/>
              <text x="${(x + (bw - 8) / 2).toFixed(1)}" y="${(y - 3).toFixed(1)}" font-size="9" fill="var(--muted)" text-anchor="middle">${c}</text>`;
    }).join("");
    const lbls = labels.map((l, i) => {
      const x = padL + i * bw + bw / 2;
      return `<text x="${x.toFixed(1)}" y="${(H - 6).toFixed(1)}" font-size="9" fill="var(--muted)" text-anchor="middle">${l}</text>`;
    }).join("");
    accSvg.innerHTML = `${bars}${lbls}`;
  }

  // Recent frames — link out to the actual JPGs; thumbnails use the image_path
  // (under /frames/), served separately by an nginx in front in prod; in dev
  // the path is just shown as text.
  const frEl = document.getElementById("sm-frames");
  if (frEl) {
    const frames = data.recent_frames || [];
    if (frames.length === 0) {
      frEl.innerHTML = `<div class="empty" style="font-size:12px;color:var(--muted)">no saved frames containing this species yet</div>`;
    } else {
      frEl.innerHTML = frames.map(f => {
        const when = f.ts ? new Date(f.ts).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "";
        const cam = _camLabel(f.source_name);
        return `<div class="sm-frame"><div>📷 ${cam}</div><div style="margin-top:4px">${when}</div><div style="opacity:.6;margin-top:2px">${f.num_fish || 0} fish</div></div>`;
      }).join("");
    }
  }
}

function _initSpeciesModal() {
  const modal = document.getElementById("species-modal");
  if (!modal) return;
  modal.addEventListener("click", (e) => {
    if (e.target.dataset.modalClose) modal.classList.add("hidden");
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") modal.classList.add("hidden");
  });
}

// ---------- ROI / full-frame lightbox (drill-into from event thumbnail) ----------

let _roiModalEventId = null;
let _roiModalView = "roi"; // "roi" | "frame"

function openRoiModal(eventId) {
  _roiModalEventId = eventId;
  _roiModalView = "roi";
  const modal = document.getElementById("roi-modal");
  if (!modal) return;
  const r = EVENT_CACHE.get(eventId);
  const title = document.getElementById("roi-modal-title");
  const meta  = document.getElementById("roi-modal-meta");
  // innerHTML so the parenthetical gets its <em> italics applied; falls
  // through to a plain "Event N" label when the latin is missing.
  if (title) {
    title.innerHTML = r?.best_name
      ? speciesDisplayHtml(r.best_name, r.best_common)
      : `Event ${eventId}`;
  }
  if (meta) {
    const parts = [];
    if (r?.best_accuracy != null) parts.push(`${fmtPct(r.best_accuracy)} confidence`);
    if (r?.ts) parts.push(new Date(r.ts).toLocaleString());
    if (r?.source_name) parts.push(r.source_name);
    if (r?.frame_id != null) parts.push(`frame ${r.frame_id}`);
    meta.textContent = parts.join(" · ") || "—";
  }
  // Mirror the list-card action panel inside the modal so reviewers can
  // accept / correct / reject without closing the lightbox. Closing the
  // inline list slot for this event first avoids cf-*-${eventId} ID
  // collisions if it happened to be expanded.
  const listSlot = document.querySelector(
    `li[data-event-id="${eventId}"] .correction-slot`,
  );
  if (listSlot) listSlot.innerHTML = "";
  const actions = document.getElementById("roi-modal-actions");
  if (actions) {
    actions.dataset.eventId = String(eventId);
    actions.innerHTML = `${eventActionsHtml(eventId)}<div class="correction-slot"></div>`;
  }
  switchRoiView("roi");
  modal.classList.remove("hidden");
}

function switchRoiView(view) {
  _roiModalView = view;
  const img = document.getElementById("roi-modal-img");
  const svg = document.getElementById("roi-modal-svg");
  const editToolbar = document.getElementById("roi-modal-edit-toolbar");
  const tabRoi = document.getElementById("roi-tab-roi");
  const tabFrame = document.getElementById("roi-tab-frame");
  const tabEdit = document.getElementById("roi-tab-edit");
  if (!img || _roiModalEventId == null) return;
  // Reset all tab/element states; the view-specific branch below re-enables
  // what it needs.
  tabRoi?.classList.remove("active");
  tabFrame?.classList.remove("active");
  tabEdit?.classList.remove("active");
  img.classList.remove("hidden");
  svg?.classList.add("hidden");
  if (editToolbar) editToolbar.style.display = "none";

  if (view === "frame") {
    img.src = `/api/detections/${_roiModalEventId}/frame.jpg?size=1600`;
    tabFrame?.classList.add("active");
  } else if (view === "edit") {
    tabEdit?.classList.add("active");
    img.classList.add("hidden");
    svg?.classList.remove("hidden");
    if (editToolbar) editToolbar.style.display = "";
    _enterBboxEdit(_roiModalEventId);
  } else {
    img.src = `/api/detections/${_roiModalEventId}/roi.jpg?size=1024`;
    tabRoi?.classList.add("active");
  }
}

// ---------- Bbox editor ----------
//
// State machine for the modal's "Edit bbox" view. The SVG's viewBox is the
// source frame's pixel dimensions (read from the loaded image's
// naturalWidth/naturalHeight at full resolution), so the bbox rect and
// handle positions are stored in source pixels and need no scaling when
// submitted alongside a correction. _bboxEdit.current carries the
// in-progress edit; submitting a correction merges it onto the payload
// if (and only if) it differs from the original detection bbox.
const _bboxEdit = {
  active: false,
  eventId: null,
  orig: null,         // original [x1,y1,x2,y2] from the detection
  current: null,      // edited bbox (same coords)
  imgW: 0, imgH: 0,
  dragging: null,     // null | 'move' | 'nw' | 'n' | 'ne' | 'e' | 'se' | 's' | 'sw' | 'w'
  startSvgPt: null,   // pointer position at drag start (viewBox coords)
  startBbox: null,    // bbox at drag start
};

// Handle layout: 4 corners + 4 edge midpoints. Edge handles only resize the
// one axis they sit on; corner handles resize both.
const _BBOX_HANDLES = ["nw", "n", "ne", "e", "se", "s", "sw", "w"];

async function _enterBboxEdit(eventId) {
  const r = EVENT_CACHE.get(eventId);
  const svg = document.getElementById("roi-modal-svg");
  // Bail-out paths used to do `svg.innerHTML = "<text>...</text>"`, which also
  // destroyed the <image>, <rect>, and <g> children that the editor needs to
  // exist for *every* event. Once destroyed, they don't come back on the next
  // event either. Hide the status text in the toolbar instead and leave the
  // SVG structure alone.
  const status = document.getElementById("bbox-status");
  _bboxEdit.active = false;
  if (!svg || !r || !Array.isArray(r.bbox) || r.bbox.length !== 4) {
    if (status) status.textContent = "no bbox to edit for this event";
    return;
  }
  if (!r.image_path) {
    if (status) status.textContent = "no saved frame on disk — bbox editing requires the source image";
    return;
  }

  // Load the raw frame so we can read its native pixel dimensions. Using
  // size=2400 keeps small / normal frames at original resolution; very large
  // frames downscale, which is harmless since the bbox is in *source* coords
  // and the SVG viewBox scales independently.
  const url = `/api/detections/${eventId}/frame.jpg?annotate=0&size=2400`;
  const loader = new Image();
  loader.src = url;
  try { await loader.decode(); } catch { /* still proceed; some browsers don't need decode() */ }
  const W = loader.naturalWidth || 1280;
  const H = loader.naturalHeight || 720;

  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  const svgImg = document.getElementById("roi-modal-svg-img");
  svgImg.setAttribute("href", url);
  svgImg.setAttribute("width", String(W));
  svgImg.setAttribute("height", String(H));

  _bboxEdit.active = true;
  _bboxEdit.eventId = eventId;
  _bboxEdit.orig = [...r.bbox];
  _bboxEdit.current = [...r.bbox];
  _bboxEdit.imgW = W;
  _bboxEdit.imgH = H;
  _bboxEdit.dragging = null;

  // Build handle elements (4 corners + 4 edges). Use createElementNS so
  // the rects are *actually* in the SVG namespace — setting innerHTML on
  // an SVG group with a `<rect>` string parses as HTML in some browsers,
  // which produces non-SVG elements that render but don't catch SVG
  // pointer events. That was the symptom of the "drag does nothing"
  // bug in the first cut.
  const SVG_NS = "http://www.w3.org/2000/svg";
  const handlesGroup = document.getElementById("roi-modal-bbox-handles");
  handlesGroup.replaceChildren();
  for (const h of _BBOX_HANDLES) {
    const rect = document.createElementNS(SVG_NS, "rect");
    rect.setAttribute("class", `bbox-handle bbox-handle-${h}`);
    rect.dataset.handle = h;
    handlesGroup.appendChild(rect);
  }
  _renderBbox();
  _bindBboxEvents();
  _updateBboxStatus();
}

function _renderBbox() {
  const [x1, y1, x2, y2] = _bboxEdit.current;
  const rect = document.getElementById("roi-modal-bbox");
  rect.setAttribute("x", String(x1));
  rect.setAttribute("y", String(y1));
  rect.setAttribute("width", String(Math.max(1, x2 - x1)));
  rect.setAttribute("height", String(Math.max(1, y2 - y1)));
  // Handle size scales with bbox area so they stay reachable on small boxes
  // but don't overwhelm large ones.
  const hSize = Math.max(8, Math.min(28, Math.round((x2 - x1 + y2 - y1) / 30)));
  const half = hSize / 2;
  const positions = {
    nw: [x1, y1], n: [(x1 + x2) / 2, y1], ne: [x2, y1],
    e:  [x2, (y1 + y2) / 2],
    se: [x2, y2], s: [(x1 + x2) / 2, y2], sw: [x1, y2],
    w:  [x1, (y1 + y2) / 2],
  };
  for (const h of _BBOX_HANDLES) {
    const el = document.querySelector(`#roi-modal-bbox-handles .bbox-handle-${h}`);
    if (!el) continue;
    const [cx, cy] = positions[h];
    el.setAttribute("x", String(cx - half));
    el.setAttribute("y", String(cy - half));
    el.setAttribute("width", String(hSize));
    el.setAttribute("height", String(hSize));
  }
}

function _bboxToSvgPt(svg, clientX, clientY) {
  // Map a clientX/clientY (CSS pixel) into the SVG's viewBox coord space.
  // Uses the inverse of the screen CTM, which is the standard SVG-coords
  // recipe and survives CSS scaling automatically.
  const pt = svg.createSVGPoint();
  pt.x = clientX;
  pt.y = clientY;
  const inv = svg.getScreenCTM().inverse();
  const p = pt.matrixTransform(inv);
  return [p.x, p.y];
}

function _bindBboxEvents() {
  const svg = document.getElementById("roi-modal-svg");
  if (svg.dataset.bboxBound) return;          // bind once per page lifetime
  svg.dataset.bboxBound = "1";

  const onDown = (e) => {
    if (!_bboxEdit.active) return;
    const target = e.target.closest(".bbox-handle, #roi-modal-bbox");
    if (!target) return;
    const handle = target.dataset.handle || "move";
    _bboxEdit.dragging = handle;
    _bboxEdit.startBbox = [..._bboxEdit.current];
    _bboxEdit.startSvgPt = _bboxToSvgPt(svg, e.clientX, e.clientY);
    e.preventDefault();
  };

  const onMove = (e) => {
    if (!_bboxEdit.dragging) return;
    const [sx, sy] = _bboxEdit.startSvgPt;
    const [px, py] = _bboxToSvgPt(svg, e.clientX, e.clientY);
    const dx = px - sx, dy = py - sy;
    let [x1, y1, x2, y2] = _bboxEdit.startBbox;
    const W = _bboxEdit.imgW, H = _bboxEdit.imgH;
    const h = _bboxEdit.dragging;
    if (h === "move") {
      const w = x2 - x1, hgt = y2 - y1;
      x1 = Math.max(0, Math.min(W - w, x1 + dx));
      y1 = Math.max(0, Math.min(H - hgt, y1 + dy));
      x2 = x1 + w; y2 = y1 + hgt;
    } else {
      if (h.includes("n")) y1 = Math.max(0, Math.min(y2 - 4, y1 + dy));
      if (h.includes("s")) y2 = Math.min(H, Math.max(y1 + 4, y2 + dy));
      if (h.includes("w")) x1 = Math.max(0, Math.min(x2 - 4, x1 + dx));
      if (h.includes("e")) x2 = Math.min(W, Math.max(x1 + 4, x2 + dx));
    }
    _bboxEdit.current = [Math.round(x1), Math.round(y1), Math.round(x2), Math.round(y2)];
    _renderBbox();
    _updateBboxStatus();
  };

  const onUp = () => { _bboxEdit.dragging = null; };

  svg.addEventListener("mousedown", onDown);
  document.addEventListener("mousemove", onMove);
  document.addEventListener("mouseup", onUp);
  // Touch support — single-finger drag mirrors mouse.
  svg.addEventListener("touchstart", (e) => onDown(e.touches[0]), { passive: false });
  document.addEventListener("touchmove", (e) => onMove(e.touches[0]), { passive: false });
  document.addEventListener("touchend", onUp);
}

function _bboxIsEdited() {
  if (!_bboxEdit.active || !_bboxEdit.orig || !_bboxEdit.current) return false;
  return _bboxEdit.orig.some((v, i) => v !== _bboxEdit.current[i]);
}

function _updateBboxStatus() {
  const status = document.getElementById("bbox-status");
  if (!status) return;
  if (_bboxIsEdited()) {
    const [x1, y1, x2, y2] = _bboxEdit.current;
    status.textContent = `edited: [${x1}, ${y1}, ${x2}, ${y2}] — will be saved with the next correction`;
    status.classList.add("bbox-status-edited");
  } else {
    status.textContent = "drag corners or edges to resize · drag inside the box to move";
    status.classList.remove("bbox-status-edited");
  }
}

function resetBbox() {
  if (!_bboxEdit.active) return;
  _bboxEdit.current = [..._bboxEdit.orig];
  _renderBbox();
  _updateBboxStatus();
}

function _consumeEditedBbox() {
  // Returns the edited bbox (and clears the dirty state) if it differs from
  // the original; null otherwise. Called when a correction is submitted so
  // the payload includes the new bbox iff the user actually moved it.
  if (!_bboxIsEdited()) return null;
  const out = [..._bboxEdit.current];
  // Don't actually reset state — let the user see the same bbox if they
  // open the modal again. _bboxEdit.active gets cleared on modal close.
  return out;
}

// Click handler for the "ⓘ why no thumbnail?" link next to Recent events:
// expand the reports body if it's collapsed, switch to the Data Notes tab,
// and scroll the Frame Save Policy section into view. Reviewers shouldn't
// have to hunt for the explanation.
function openFrameSavePolicy() {
  const body = document.getElementById("reports-body");
  if (body?.classList.contains("collapsed")) {
    document.getElementById("reports-toggle")?.click();
  }
  document.querySelector('.reports-tab[data-tab="data-notes"]')?.click();
  setTimeout(() => {
    const target = document.getElementById("frame-save-policy");
    target?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, 80);
}

function _initRoiModal() {
  const modal = document.getElementById("roi-modal");
  if (!modal) return;
  const close = () => {
    modal.classList.add("hidden");
    _bboxEdit.active = false;     // forget any pending bbox edit on close
    _bboxEdit.eventId = null;
  };
  modal.addEventListener("click", (e) => {
    if (e.target.dataset.roiClose) close();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !modal.classList.contains("hidden")) close();
  });
}

// ---------- Camera toggle (live stream) ----------

async function _initCameraToggle() {
  // Hide the Pier button up front if the pier worker isn't configured.
  let cams = [];
  try {
    const r = await fetch("/api/cameras", { cache: "no-store" });
    if (r.ok) cams = (await r.json()).cameras || [];
  } catch { /* keep defaults */ }
  const ids = new Set(cams.map(c => c.id));

  document.querySelectorAll("#camera-toggle .cam-btn").forEach(b => {
    if (b.dataset.cam !== "seahivecam" && !ids.has(b.dataset.cam)) {
      b.style.display = "none";
    }
    b.addEventListener("click", () => _setActiveCamera(b.dataset.cam));
  });

  // Restore last choice
  const saved = localStorage.getItem("wb_camera") || "seahivecam";
  _setActiveCamera(saved);
}

function _setActiveCamera(camId) {
  // Highlight button
  document.querySelectorAll("#camera-toggle .cam-btn").forEach(b => {
    b.classList.toggle("active", b.dataset.cam === camId);
  });
  // Swap stream src + bbox-toggle target endpoints
  const img = document.getElementById("stream");
  const bboxBtn = document.getElementById("bbox-toggle");
  const bboxesOn = !bboxBtn?.classList.contains("off");
  const streamPath = camId === "pier_cam"
    ? (bboxesOn ? "/api/stream_pier.mjpeg" : "/api/stream_pier_raw.mjpeg")
    : (bboxesOn ? "/api/stream.mjpeg" : "/api/stream_raw.mjpeg");
  if (img) img.src = streamPath;
  window._wbActiveCamera = camId;
  localStorage.setItem("wb_camera", camId);
}

// ---------- Master tick ----------

async function tickReports() {
  if (!reportsActive()) return;
  const tab = activeReportsTab();
  const hours = reportsHours();
  try {
    if (tab === "overview")            await renderOverviewTab(hours);
    else if (tab === "fish-explorer")  await renderFishExplorerTab(hours);
    else if (tab === "celebrities")    await renderCelebritiesTab();
    else if (tab === "water")          await renderWaterTab(hours);
    else if (tab === "weather")        await renderWeatherTab(hours);
    else if (tab === "insights")       await renderInsightsTab(hours);
    else if (tab === "data-notes")     renderDataNotesTab();
  } catch (e) {
    // intentionally silent; cached UI stays as last successful render
  }
}

initDownloads();
initReefToggle();
initBboxToggle();
initTrendsToggle();
initReportsToggle();
_initFishExplorerInteractions();
_initSpeciesModal();
_initRoiModal();
_initCelebrityModal();
_initCameraToggle();
setInterval(tick, 500);
setInterval(tickHistory, 5000);
setInterval(tickWaterQuality, 30000);
setInterval(tickDrift, 30000);
setInterval(tickAlerts, 15000);
setInterval(tickCorrectionStats, 10000);
setInterval(tickReef, 30000);
setInterval(tickTrends, 60000);
setInterval(tickReports, 60000);
tick();
tickHistory();
tickWaterQuality();
tickDrift();
tickAlerts();
tickCorrectionStats();
tickReef();
tickTrends();
tickReports();

// ---------- Celebrities tab ----------
//
// Reads /api/celebrities (cached briefly), renders one card per named
// individual + opens a per-celebrity modal with the full gallery of
// reviewer-tagged sightings. Reviewer-tagging is wired into the ROI
// modal via getCelebrityOptionsHtml() — see correctionFormHtml.

let _celebsCache = null;        // [{...}, ...] from /api/celebrities, cached
let _celebsCacheAt = 0;
const _CELEBS_CACHE_TTL_MS = 60_000;

async function _fetchCelebrities(force = false) {
  const now = Date.now();
  if (!force && _celebsCache && (now - _celebsCacheAt) < _CELEBS_CACHE_TTL_MS) {
    return _celebsCache;
  }
  const r = await fetch("/api/celebrities", { cache: "no-store" });
  if (!r.ok) return _celebsCache || [];
  const d = await r.json();
  _celebsCache = d.celebrities || [];
  _celebsCacheAt = now;
  return _celebsCache;
}

async function renderCelebritiesTab() {
  const grid = document.getElementById("celeb-grid");
  if (!grid) return;
  const celebs = await _fetchCelebrities(true);
  if (!celebs.length) {
    grid.innerHTML = `<div class="celeb-empty">no celebrities catalogue loaded — see <code>scripts/build_celebrities.py</code></div>`;
    return;
  }
  // Sort: most-recently-tagged first, then alphabetical for the
  // never-tagged tail. Stable so the directory feels predictable.
  celebs.sort((a, b) => {
    const at = a.last_tagged_at || "";
    const bt = b.last_tagged_at || "";
    if (at && bt) return bt.localeCompare(at);
    if (at && !bt) return -1;
    if (bt && !at) return 1;
    return a.nickname.localeCompare(b.nickname);
  });
  grid.innerHTML = celebs.map(c => {
    const speciesLine = c.binomial
      ? speciesDisplayHtml(c.binomial, c.common || c.species_common)
      : `<span>${escHtml(c.species_common || "unknown species")}</span>`;
    const sightings = c.sightings || 0;
    const lastSeen = c.last_tagged_at
      ? new Date(c.last_tagged_at).toLocaleDateString([], { month: "short", day: "numeric" })
      : "—";
    const photo = c.photo_url
      ? `<img class="celeb-photo" src="${c.photo_url}" alt="${escAttr(c.nickname)}" loading="lazy" onerror="this.style.display='none'">`
      : `<div class="celeb-photo celeb-photo-empty">no tagged photo yet</div>`;
    return `
      <div class="celeb-card" onclick="openCelebrityModal('${escAttr(c.id)}')" tabindex="0">
        ${photo}
        <div class="celeb-card-body">
          <div class="celeb-name">${escHtml(c.nickname)}</div>
          <div class="celeb-species">${speciesLine}</div>
          ${c.id_mark ? `<div class="celeb-idmark">${escHtml(c.id_mark)}</div>` : ""}
          <div class="celeb-meta">
            <span title="reviewer-tagged sightings">${sightings} sighting${sightings === 1 ? "" : "s"}</span>
            <span title="last reviewer tag">last seen ${lastSeen}</span>
          </div>
        </div>
      </div>`;
  }).join("");
}

// ---- Per-celebrity detail modal ----

function _initCelebrityModal() {
  const modal = document.getElementById("celeb-modal");
  if (!modal) return;
  const close = () => modal.classList.add("hidden");
  modal.addEventListener("click", (e) => {
    if (e.target.dataset.celebClose) close();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !modal.classList.contains("hidden")) close();
  });
}

async function openCelebrityModal(celebId) {
  const modal = document.getElementById("celeb-modal");
  const body  = document.getElementById("celeb-modal-body");
  if (!modal || !body) return;
  body.innerHTML = `<div class="celeb-empty">loading ${escHtml(celebId)}…</div>`;
  modal.classList.remove("hidden");
  let d;
  try {
    const r = await fetch(`/api/celebrities/${encodeURIComponent(celebId)}`, { cache: "no-store" });
    d = await r.json();
    if (!r.ok) {
      body.innerHTML = `<div class="celeb-empty">${escHtml(d.error || "unknown error")}</div>`;
      return;
    }
  } catch (e) {
    body.innerHTML = `<div class="celeb-empty">failed: ${escHtml(String(e))}</div>`;
    return;
  }
  const speciesLine = d.binomial
    ? speciesDisplayHtml(d.binomial, d.common || d.species_common)
    : escHtml(d.species_common || "unknown species");
  const hero = d.photo_url
    ? `<img class="celeb-hero" src="${d.photo_url}" alt="${escAttr(d.nickname)}">`
    : `<div class="celeb-hero celeb-photo-empty">no tagged photo yet — once a reviewer tags a detection as ${escHtml(d.nickname)}, that detection's ROI shows up here.</div>`;
  const firstSeen = d.first_seen || (d.first_tagged_at ? new Date(d.first_tagged_at).toLocaleDateString() : null);
  const lastSeen  = d.last_seen  || (d.last_tagged_at  ? new Date(d.last_tagged_at).toLocaleDateString()  : null);
  const galleryHtml = (d.gallery || []).length
    ? `<div class="celeb-gallery">${d.gallery.map(g => `
        <a class="celeb-thumb" href="javascript:openRoiModal(${g.event_id})" title="${escAttr([g.tagged_at, g.reviewer].filter(Boolean).join(' · '))}">
          <img src="${g.roi_url}" alt="event ${g.event_id}" loading="lazy" onerror="this.style.display='none'">
        </a>`).join("")}</div>`
    : `<div class="celeb-empty">no reviewer-tagged sightings yet.</div>`;
  body.innerHTML = `
    <div class="celeb-detail-grid">
      <div class="celeb-detail-photo">${hero}</div>
      <div class="celeb-detail-info">
        <h2 class="celeb-name-h2">${escHtml(d.nickname)}</h2>
        <div class="celeb-species">${speciesLine}</div>
        ${d.id_mark ? `<p class="celeb-idmark">${escHtml(d.id_mark)}</p>` : ""}
        <dl class="celeb-meta-dl">
          ${d.named_by    ? `<dt>Named by</dt><dd>${escHtml(d.named_by)}</dd>` : ""}
          ${firstSeen     ? `<dt>First seen</dt><dd>${escHtml(firstSeen)}</dd>` : ""}
          ${lastSeen      ? `<dt>Last seen</dt><dd>${escHtml(lastSeen)}</dd>`   : ""}
          <dt>Reviewer-tagged sightings</dt><dd>${d.sightings || 0}</dd>
        </dl>
      </div>
    </div>
    <h3 class="celeb-section-title">Tagged sightings</h3>
    ${galleryHtml}
  `;
}

// ---- Reviewer modal: "Tag as celebrity" dropdown ----
//
// Called from correctionFormHtml() to inject the optional dropdown.
// Filters by binomial when the reviewer has chosen a species the
// catalogue knows; passes an empty filter to show all otherwise.

function getCelebrityOptionsHtml(eventId, filterBinomial) {
  const celebs = _celebsCache || [];
  // Default visible set: all celebrities whose binomial matches the
  // filter, plus celebrities with no binomial (so they're not stuck
  // behind a filter the catalogue can't apply).
  const candidates = filterBinomial
    ? celebs.filter(c => !c.binomial || c.binomial === filterBinomial)
    : celebs;
  if (!candidates.length) return "";
  const opts = candidates
    .map(c => `<option value="${escAttr(c.id)}">${escHtml(c.nickname)}${c.species_common ? " — " + escHtml(c.species_common) : ""}</option>`)
    .join("");
  return `
    <div class="cf-section">
      <div class="cf-label">Tag as celebrity (optional):</div>
      <select id="cf-celebrity-${eventId}" class="cf-celebrity">
        <option value="">— not a known individual —</option>
        ${opts}
      </select>
    </div>`;
}

// Kick off a cache priming on load so the first reviewer-modal open
// has the catalogue available.
_fetchCelebrities().catch(() => {});
