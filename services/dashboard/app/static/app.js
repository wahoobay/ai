const fmtMs = (x) => (x == null ? "—" : `${x.toFixed(0)} ms`);
const fmtPct = (x) => (x == null ? "—" : `${(x * 100).toFixed(1)}%`);
const short = (s) => (s ? s.slice(0, 40) : "");

async function poll(url) {
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  return r.json();
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
  if (!live || !live.detections || !live.detections.length) {
    el.innerHTML = `<li class="empty">no fish in current frame</li>`;
    return;
  }
  el.innerHTML = live.detections
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

function renderStats(live, stats) {
  document.getElementById("stat-source").textContent = `source: ${stats.current_source || "—"}`;
  document.getElementById("stat-frames").textContent = `frames: ${stats.frames_seen ?? "—"}`;
  document.getElementById("stat-fish").textContent = `with fish: ${stats.frames_with_fish ?? "—"}`;
  document.getElementById("stat-infer").textContent = `inference: ${fmtMs(live.infer_ms)}`;
}

function renderSpecies(rows) {
  const el = document.getElementById("species-list");
  if (!rows || !rows.length) {
    el.innerHTML = `<li class="empty">no species recorded yet</li>`;
    return;
  }
  el.innerHTML = rows
    .map(
      (r) => `
      <li>
        <span class="species-name">${r.name ?? r.species_id}</span>
        <span class="acc">×${r.n}</span>
        <span class="meta">mean ${fmtPct(r.mean_acc)} · last ${new Date(r.last_seen).toLocaleTimeString()}</span>
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
  try {
    const [species, events] = await Promise.all([
      poll("/api/species_counts?hours=24").catch(() => []),
      poll("/api/events?limit=25").catch(() => []),
    ]);
    renderSpecies(species);
    renderEvents(events);
  } catch (e) {
    // ignore
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
  try {
    const [latest, history] = await Promise.all([
      poll("/api/water_quality/latest").catch(() => null),
      poll("/api/water_quality/history?hours=24&max_points=144").catch(() => null),
    ]);
    renderWaterQuality(latest, history);
  } catch (e) {
    // keep previous render
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
  if (!sel) return;
  const hours = sel.value;
  for (const a of document.querySelectorAll(".dl-btn")) {
    const resource = a.dataset.export;
    const params = new URLSearchParams();
    // alerts.csv uses include_resolved instead of hours
    if (resource === "alerts") {
      params.set("include_resolved", "true");
    } else {
      params.set("hours", hours);
    }
    a.href = `/api/export/${resource}.csv?${params.toString()}`;
  }
}

function initDownloads() {
  const sel = document.getElementById("dl-window");
  if (!sel) return;
  sel.addEventListener("change", refreshDownloadLinks);
  refreshDownloadLinks();
}

initReviewer();
initDownloads();
refreshAuthMode();
setInterval(tick, 500);
setInterval(tickHistory, 5000);
setInterval(tickWaterQuality, 30000);
setInterval(tickDrift, 30000);
setInterval(tickAlerts, 15000);
setInterval(tickCorrectionStats, 10000);
tick();
tickHistory();
tickWaterQuality();
tickDrift();
tickAlerts();
tickCorrectionStats();
