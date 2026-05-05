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

function writeAuthHeaders() {
  const tok = (localStorage.getItem("wb_write_token") || "").trim();
  return tok ? { "Authorization": `Bearer ${tok}` } : {};
}

let WRITE_PROTECTED = false;
async function refreshAuthMode() {
  try {
    const m = await poll("/api/auth/mode");
    WRITE_PROTECTED = !!m.write_protected;
    updateAuthBadge();
  } catch {}
}
function updateAuthBadge() {
  const badge = document.getElementById("auth-state");
  if (!badge) return;
  const tok = (localStorage.getItem("wb_write_token") || "").trim();
  if (!WRITE_PROTECTED) {
    badge.textContent = "open";
    badge.className = "auth-badge reviewer";
  } else if (tok) {
    badge.textContent = "reviewer";
    badge.className = "auth-badge reviewer";
  } else {
    badge.textContent = "view-only";
    badge.className = "auth-badge";
  }
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
      const name = d.best_name ?? "unknown";
      const acc = d.best_accuracy;
      const topk = (d.topk || [])
        .slice(1)
        .map((p) => `<span class="meta">${p.name} ${fmtPct(p.accuracy)}</span>`)
        .join(" · ");
      return `
        <li>
          <span class="species-name">${name}</span>
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
        <span class="species-name">${r.name ?? r.species_id}</span>
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

function renderEvents(rows) {
  const el = document.getElementById("events-list");
  if (!rows || !rows.length) {
    el.innerHTML = `<li class="empty">no events yet</li>`;
    return;
  }
  el.innerHTML = rows
    .map(
      (r) => `
      <li class="event-item" data-event-id="${r.id}">
        <span class="species-name">${r.best_name ?? "unknown"}</span>
        <span class="acc">${fmtPct(r.best_accuracy)}</span>
        <div class="meta">${new Date(r.ts).toLocaleTimeString()} · ${short(r.source_name)} · frame ${r.frame_id}</div>
        <button class="correct-btn" onclick="toggleCorrection(${r.id}, '${(r.best_name || '').replace(/'/g, "\\'")}')">correct</button>
        <div class="correction-slot"></div>
      </li>`
    )
    .join("");
}

function correctionFormHtml(eventId, originalName) {
  const reviewer = localStorage.getItem("wb_reviewer") || "";
  return `
    <div class="correction-form">
      <div class="row">
        <input type="text" placeholder="correct species name" id="cf-name-${eventId}" value="">
      </div>
      <div class="row">
        <select id="cf-conf-${eventId}">
          <option value="certain">certain</option>
          <option value="probable" selected>probable</option>
          <option value="uncertain">uncertain</option>
        </select>
        <label style="font-size: 11px; color: var(--muted); display: flex; align-items: center; gap: 4px;">
          <input type="checkbox" id="cf-notfish-${eventId}"> not a fish
        </label>
      </div>
      <div class="row">
        <input type="text" placeholder="notes (optional)" id="cf-notes-${eventId}" style="flex:1">
      </div>
      <div class="row" style="justify-content: flex-end; gap: 6px;">
        <button class="secondary" onclick="closeCorrection(${eventId})">cancel</button>
        <button onclick="submitCorrection(${eventId})">save</button>
      </div>
    </div>
  `;
}

function toggleCorrection(eventId, originalName) {
  const li = document.querySelector(`li[data-event-id="${eventId}"]`);
  if (!li) return;
  const slot = li.querySelector(".correction-slot");
  if (slot.innerHTML.trim()) {
    slot.innerHTML = "";
  } else {
    slot.innerHTML = correctionFormHtml(eventId, originalName);
    const nameInput = document.getElementById(`cf-name-${eventId}`);
    if (nameInput) nameInput.focus();
  }
}

function closeCorrection(eventId) {
  const li = document.querySelector(`li[data-event-id="${eventId}"]`);
  if (!li) return;
  li.querySelector(".correction-slot").innerHTML = "";
}

async function submitCorrection(eventId) {
  const reviewerInput = document.getElementById("reviewer-input");
  const reviewer = reviewerInput.value.trim();
  if (reviewer) localStorage.setItem("wb_reviewer", reviewer);

  const name  = document.getElementById(`cf-name-${eventId}`).value.trim();
  const conf  = document.getElementById(`cf-conf-${eventId}`).value;
  const nf    = document.getElementById(`cf-notfish-${eventId}`).checked;
  const notes = document.getElementById(`cf-notes-${eventId}`).value.trim();

  if (!nf && !name) {
    alert("provide a species name or tick 'not a fish'");
    return;
  }
  const body = {
    event_id: eventId,
    corrected_name: name || null,
    corrected_species_id: null,
    not_a_fish: nf,
    confidence: conf,
    reviewer: reviewer || null,
    notes: notes || null,
  };
  try {
    const r = await fetch("/api/corrections", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...writeAuthHeaders() },
      body: JSON.stringify(body),
    });
    if (r.status === 401) {
      alert("write token required or invalid — paste it in the field next to 'reviewer'");
      return;
    }
    if (!r.ok) throw new Error(`${r.status}`);
    const li = document.querySelector(`li[data-event-id="${eventId}"]`);
    if (li) {
      li.querySelector(".correction-slot").innerHTML = `<div class="correction-ok">✓ saved</div>`;
    }
    tickCorrectionStats();
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

function initReviewer() {
  const el = document.getElementById("reviewer-input");
  if (el) {
    const saved = localStorage.getItem("wb_reviewer");
    if (saved) el.value = saved;
    el.addEventListener("change", () => {
      if (el.value.trim()) localStorage.setItem("wb_reviewer", el.value.trim());
    });
  }
  const tk = document.getElementById("write-token-input");
  if (tk) {
    const saved = localStorage.getItem("wb_write_token");
    if (saved) tk.value = saved;
    tk.addEventListener("change", () => {
      const v = tk.value.trim();
      if (v) localStorage.setItem("wb_write_token", v);
      else localStorage.removeItem("wb_write_token");
      updateAuthBadge();
    });
  }
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

const WQ_PARAMS = [
  { key: "water_temp_c",      label: "Water temp", unit: "°C",    fmt: (v) => v.toFixed(2), normal: [18, 32] },
  { key: "ph",                label: "pH",          unit: "",     fmt: (v) => v.toFixed(2), normal: [7.6, 8.4] },
  { key: "do_pct",            label: "DO",          unit: "% sat", fmt: (v) => v.toFixed(1), normal: [60, 130] },
  { key: "chlorophyll_rfu",   label: "Chlorophyll", unit: "RFU",  fmt: (v) => v.toFixed(2), normal: [0, 6] },
  { key: "phycoerythrin_rfu", label: "Phycoerythrin", unit: "RFU", fmt: (v) => v.toFixed(2), normal: [0, 3] },
  { key: "turbidity_fnu",     label: "Turbidity",   unit: "FNU",  fmt: (v) => v.toFixed(2), normal: [0, 15] },
  { key: "no3_mg_l",          label: "Nitrate-N",   unit: "mg/L", fmt: (v) => v.toFixed(3), normal: [0, 1.0] },
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
    ? `<div class="wq-synthetic-banner">Synthetic placeholder data (generator) — will switch to live SenseStream feed once credentials are configured.</div>`
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
  const reviewer = (document.getElementById("reviewer-input")?.value || "").trim() || "anonymous";
  try {
    const r = await fetch(`/api/alerts/${id}/ack?reviewer=${encodeURIComponent(reviewer)}`,
      { method: "POST", headers: writeAuthHeaders() });
    if (r.status === 401) {
      alert("write token required to acknowledge alerts");
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
    const display = r.common
      ? `${r.common} <em>(${r.latin})</em>`
      : `<em>${r.latin}</em>`;
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
    ? `<div class="reef-axis-label">synthetic placeholder data — live SenseStream feed pending credentials</div>`
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

  function apply(showing) {
    img.src = showing ? "/api/stream.mjpeg" : "/api/stream_raw.mjpeg";
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

  const wq = series.map(p => p[paramKey]);
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
    const pts = series
      .map((p, i) => p[paramKey] != null ? `${xS(xs[i]).toFixed(1)},${yR(p[paramKey]).toFixed(1)}` : null)
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

function renderWqHistoryGrid(latest, history) {
  const grid = document.getElementById("chart-wq-grid");
  if (!grid) return;
  const series = (history && history.series) || [];
  if (!series.length && !latest) {
    grid.innerHTML = `<div class="empty" style="font-size:12px">no readings</div>`;
    return;
  }
  const cells = WQ_PARAMS.map(p => {
    const vals = series.map(r => r[p.key]);
    const last = (latest && latest[p.key] != null)
      ? latest[p.key]
      : vals.filter(v => v != null).slice(-1)[0];
    const txt = last != null ? p.fmt(last) : "—";
    return `<div class="chart-wq-cell">
      <div class="wq-cell-name">${p.label}</div>
      <div class="wq-cell-value">${txt}<span class="wq-cell-unit">${p.unit}</span></div>
      ${sparkline(vals, p.normal)}
    </div>`;
  }).join("");
  grid.innerHTML = cells;
}

let _lastSxwData = null;
function tickSxwParamOnly() {
  if (!_lastSxwData) return;
  const param = document.getElementById("chart-sxw-param")?.value || "water_temp_c";
  renderSightingsXWater(_lastSxwData, param);
}

function trendsActive() {
  const t = document.getElementById("view-trends");
  return t && !t.classList.contains("hidden");
}

async function tickTrends() {
  // Skip work entirely when the user is on the Live view.
  if (!trendsActive()) return;
  const hours = parseInt(document.getElementById("trends-window")?.value || "24", 10);
  const param = document.getElementById("chart-sxw-param")?.value || "water_temp_c";

  // Snapshot first (returning visitors)
  const cdr  = readCached(`/api/charts/detection_rate?hours=${hours}`);
  const cts  = readCached(`/api/charts/top_species_timeseries?hours=${hours}&top_n=8`);
  const csxw = readCached(`/api/charts/sightings_x_water?hours=${hours}`);
  const cwql = readCached("/api/water_quality/latest");
  const cwqh = readCached(`/api/water_quality/history?hours=${hours}&max_points=300`);
  if (cdr)  renderDetectionRate(cdr.data);
  if (cts)  renderTopSpeciesTimeseries(cts.data);
  if (csxw) { _lastSxwData = csxw.data; renderSightingsXWater(csxw.data, param); }
  if (cwql || cwqh) renderWqHistoryGrid(cwql?.data || null, cwqh?.data || null);

  try {
    const [dr, ts, sxw, wql, wqh] = await Promise.all([
      pollWithCache(`/api/charts/detection_rate?hours=${hours}`).catch(() => null),
      pollWithCache(`/api/charts/top_species_timeseries?hours=${hours}&top_n=8`).catch(() => null),
      pollWithCache(`/api/charts/sightings_x_water?hours=${hours}`).catch(() => null),
      pollWithCache("/api/water_quality/latest").catch(() => null),
      pollWithCache(`/api/water_quality/history?hours=${hours}&max_points=300`).catch(() => null),
    ]);
    if (dr)  renderDetectionRate(dr);
    if (ts)  renderTopSpeciesTimeseries(ts);
    if (sxw) { _lastSxwData = sxw; renderSightingsXWater(sxw, param); }
    if (wql || wqh) renderWqHistoryGrid(wql, wqh);
  } catch {}
}

function initViewToggle() {
  const btn    = document.getElementById("view-toggle");
  const live   = document.getElementById("view-live");
  const trends = document.getElementById("view-trends");
  if (!btn || !live || !trends) return;
  function apply(showTrends) {
    live.classList.toggle("hidden", showTrends);
    trends.classList.toggle("hidden", !showTrends);
    btn.textContent = showTrends ? "🐠 Live" : "📊 Trends";
    localStorage.setItem("wb_view", showTrends ? "trends" : "live");
    if (showTrends) tickTrends();
  }
  apply(localStorage.getItem("wb_view") === "trends");
  btn.addEventListener("click", () => apply(trends.classList.contains("hidden")));
  document.getElementById("trends-window")?.addEventListener("change", tickTrends);
  document.getElementById("chart-sxw-param")?.addEventListener("change", tickSxwParamOnly);
}

initReviewer();
initDownloads();
initReefToggle();
initBboxToggle();
initViewToggle();
refreshAuthMode();
setInterval(tick, 500);
setInterval(tickHistory, 5000);
setInterval(tickWaterQuality, 30000);
setInterval(tickDrift, 30000);
setInterval(tickAlerts, 15000);
setInterval(tickCorrectionStats, 10000);
setInterval(tickReef, 30000);
setInterval(tickTrends, 60000);
tick();
tickHistory();
tickWaterQuality();
tickDrift();
tickAlerts();
tickCorrectionStats();
tickReef();
tickTrends();
