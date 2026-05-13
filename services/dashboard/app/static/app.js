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
      const latin = d.best_name ?? "unknown";
      const common = d.best_common;
      // Match the "Most-spotted fish" formatting: common name first,
      // scientific in italic parens; if there's no common name we fall
      // back to italic Latin alone.
      const display = common
        ? `${common} <em>(${latin})</em>`
        : `<em>${latin}</em>`;
      const acc = d.best_accuracy;
      const topk = (d.topk || [])
        .slice(1)
        .map((p) => `<span class="meta">${p.name} ${fmtPct(p.accuracy)}</span>`)
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
  // Restore active tab
  const saved = localStorage.getItem("wb_reports_tab");
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

function _renderMap(sources) {
  const el = document.getElementById("ov-map");
  if (!el) return;
  if (!sources || !Object.keys(sources).length) {
    el.innerHTML = `<div style="padding:20px;color:var(--muted);font-size:12px;text-align:center">no camera metadata</div>`;
    return;
  }
  const pts = Object.entries(sources)
    .filter(([_, v]) => v.lat != null && v.lng != null)
    .map(([k, v]) => ({ key: k, name: v.display_name || k, lat: v.lat, lng: v.lng }));
  if (pts.length === 0) { el.innerHTML = ""; return; }
  // Tight bbox around the points with a margin.
  const lats = pts.map(p => p.lat), lngs = pts.map(p => p.lng);
  const latMin = Math.min(...lats), latMax = Math.max(...lats);
  const lngMin = Math.min(...lngs), lngMax = Math.max(...lngs);
  const latPad = Math.max(0.005, (latMax - latMin) * 0.4);
  const lngPad = Math.max(0.005, (lngMax - lngMin) * 0.4);
  const W = 480, H = 200, padX = 30, padY = 22;
  const xS = lng => padX + ((lng - (lngMin - lngPad)) / ((lngMax + lngPad) - (lngMin - lngPad))) * (W - 2 * padX);
  // y inverted (north up)
  const yS = lat => padY + (1 - ((lat - (latMin - latPad)) / ((latMax + latPad) - (latMin - latPad)))) * (H - 2 * padY);

  const pins = pts.map(p => `
    <circle class="ov-map-pin" cx="${xS(p.lng).toFixed(1)}" cy="${yS(p.lat).toFixed(1)}" r="5"/>
    <text class="ov-map-label" x="${(xS(p.lng) + 8).toFixed(1)}" y="${(yS(p.lat) + 3).toFixed(1)}">${p.name}</text>
  `).join("");

  // North arrow + scale ticks
  const compass = `
    <text x="14" y="18" font-size="10" fill="var(--muted)" font-weight="600">N ▲</text>
    <text x="${(W - 8).toFixed(1)}" y="${(H - 6).toFixed(1)}" font-size="9" fill="var(--muted)" text-anchor="end">Pompano Beach, FL</text>
  `;
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">${compass}${pins}</svg>`;
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
        const display = r.common ? `${r.common} <em>(${r.latin})</em>` : `<em>${r.latin}</em>`;
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

// ---------- Render: Water & Weather ----------

async function renderWaterWeatherTab(hours) {
  const [wqLatest, wqHist, wxLatest, wxHist] = await Promise.all([
    pollWithCache("/api/water_quality/latest").catch(() => null),
    pollWithCache(`/api/water_quality/history?hours=${hours}&max_points=120`).catch(() => null),
    pollWithCache("/api/weather/latest").catch(() => null),
    pollWithCache(`/api/weather/history?hours=${hours}&max_points=120`).catch(() => null),
  ]);

  const setText = (id, txt) => { const e = document.getElementById(id); if (e) e.textContent = txt; };
  const f = (v, n=1) => v == null ? "—" : v.toFixed(n);

  // KPIs (latest weather + sonde)
  if (wxLatest) {
    setText("ww-kpi-air",      wxLatest.air_temp_c != null ? f(wxLatest.air_temp_c * 9/5 + 32, 1) + " °F" : "—");
    setText("ww-kpi-wind",     wxLatest.wind_speed_avg_ms != null ? f(wxLatest.wind_speed_avg_ms * 2.23694, 1) + " mph" : "—");
    setText("ww-kpi-solar",    f(wxLatest.solar_rad_wm2, 0));
    setText("ww-kpi-pressure", wxLatest.bar_press_hpa != null ? f(wxLatest.bar_press_hpa * 0.750064, 1) : "—");
    setText("ww-kpi-cloud",    wxLatest.cloud_cover_pct != null ? f(wxLatest.cloud_cover_pct, 0) + "%" : "—");
  }
  // Rain total summed over the window from the history series
  if (wxHist?.series?.length) {
    const rainTotalMm = wxHist.series.reduce((s, r) => s + (r.rain_accum_mm || 0), 0);
    setText("ww-kpi-rain", (rainTotalMm * 0.0393701).toFixed(2) + " in");
  } else {
    setText("ww-kpi-rain", "—");
  }

  // Sparklines — water quality
  const mkLine = (id, points, opts) => _miniLine(document.getElementById(id), points, opts);
  if (wqHist?.series?.length) {
    const ts = wqHist.series.map(r => new Date(r.bucket).getTime());
    mkLine("ww-chart-wtemp", wqHist.series.map((r, i) => ({ x: ts[i], y: r.water_temp_c != null ? r.water_temp_c * 9/5 + 32 : null })), { unit: "°F", fmt: n => n.toFixed(1) });
    mkLine("ww-chart-do",    wqHist.series.map((r, i) => ({ x: ts[i], y: r.do_pct })),                                                          { unit: "%",  fmt: n => n.toFixed(0) });
    mkLine("ww-chart-ph",    wqHist.series.map((r, i) => ({ x: ts[i], y: r.ph })),                                                              { fmt: n => n.toFixed(2) });
    mkLine("ww-chart-turb",  wqHist.series.map((r, i) => ({ x: ts[i], y: r.turbidity_fnu })),                                                   { unit: "FNU", fmt: n => n.toFixed(1) });
  }
  // Sparklines — weather (air/wind combo and rain/solar combo, simplified to single-line per panel)
  if (wxHist?.series?.length) {
    const ts = wxHist.series.map(r => new Date(r.bucket).getTime());
    mkLine("ww-chart-air",  wxHist.series.map((r, i) => ({ x: ts[i], y: r.air_temp_c != null ? r.air_temp_c * 9/5 + 32 : null })), { unit: "°F", fmt: n => n.toFixed(1) });
    mkLine("ww-chart-rain", wxHist.series.map((r, i) => ({ x: ts[i], y: r.rain_accum_mm != null ? r.rain_accum_mm * 0.0393701 : null })), { unit: "in", fmt: n => n.toFixed(2) });
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
    const name = s.common
      ? `${s.common} <em>(${s.latin})</em>`
      : `<em>${s.latin}</em>`;
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
    const name = sp.common ? `${sp.common} <em>(${sp.latin})</em>` : `<em>${sp.latin}</em>`;
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
        const display = r.common ? `${r.common} <em>(${r.latin})</em>` : `<em>${r.latin}</em>`;
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
    else if (tab === "water-weather")  await renderWaterWeatherTab(hours);
    else if (tab === "insights")       await renderInsightsTab(hours);
    else if (tab === "data-notes")     renderDataNotesTab();
  } catch (e) {
    // intentionally silent; cached UI stays as last successful render
  }
}

initReviewer();
initDownloads();
initReefToggle();
initBboxToggle();
initTrendsToggle();
initReportsToggle();
_initFishExplorerInteractions();
_initSpeciesModal();
_initCameraToggle();
refreshAuthMode();
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
