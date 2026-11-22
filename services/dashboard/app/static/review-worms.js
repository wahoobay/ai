// services/dashboard/app/static/review-worms.js
// Asking WoRMS, the World Register of Marine Species, whether a name a reviewer typed into "Add a
// species" is a species, and which (spec 2026-11-21-species-worms-check). The reviewer's browser
// asks WoRMS itself: staging's dashboard has no internet access, and WoRMS's REST API answers
// browser calls from any origin (CORS *). Each call is a plain GET with no custom header and no
// cookies, so the browser sends it without a preflight. The server applies the list rules to the
// record the reviewer picks; nothing here decides what is listed.
//
// RV.worms.check({scientific, common}) answers {status, records, note}:
//   match  -- WoRMS knows the scientific name exactly; records[0] is its accepted species (a
//             synonym's note says "WoRMS lists this as <valid name>");
//   choose -- candidates for "Did you mean?" (species only, each accepted species once,
//             accepted names first, at most 5);
//   none   -- nothing found, or only a name WoRMS doesn't accept (a nomen dubium, say);
//   genus  -- the name is a genus or another rank, not a species;
//   down   -- WoRMS couldn't be reached, answered an error, or took more than TIMEOUT_MS.
// Each record is the accepted one: {aphia_id, name, authority, family, rank, url}. Its text is
// WoRMS's and goes into the page as text only.
"use strict";

RV.worms = (() => {
  const BASE = "https://www.marinespecies.org/rest";
  const MAX = 5;
  const NONE = { status: "none", records: [], note: null };
  const DOWN = { status: "down", records: [], note: null };
  const api = { TIMEOUT_MS: 10000 };

  // GET `path` from WoRMS: its JSON, [] for 204 (nothing found), or null when WoRMS can't be
  // reached, answers anything else, or takes longer than api.TIMEOUT_MS.
  async function get(path) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), api.TIMEOUT_MS);
    try {
      const response = await fetch(BASE + path, { signal: controller.signal });
      if (response.status === 204) return [];
      if (!response.ok) return null;
      return await response.json();
    } catch (err) {
      return null;
    } finally {
      clearTimeout(timer);
    }
  }

  const list = (data) => (Array.isArray(data) ? data.filter((r) => r && typeof r === "object") : []);
  const positive = (id) => (Number.isInteger(id) && id > 0 ? id : null);

  // Whether `r` names a species WoRMS accepts (final review M1): a record that stands for
  // itself (no valid record, or itself) only when its status is "accepted" -- not a nomen
  // dubium, a taxon inquirendum, or a record TAXAMATCH finds quarantined or deleted; one
  // pointing to another valid record (a synonym) for the accepted record it points to.
  function usable(r) {
    const valid = positive(r.valid_AphiaID);
    return (valid !== null && valid !== positive(r.AphiaID)) || r.status === "accepted";
  }

  // The accepted record `r` stands for: itself, or the valid record a synonym points to. The link
  // is built from the AphiaID, never taken from the answer.
  function accepted(r) {
    const own = positive(r.AphiaID);
    const valid = positive(r.valid_AphiaID) || own;
    const self = valid === own;
    return {
      aphia_id: valid,
      name: self ? r.scientificname : r.valid_name || r.scientificname,
      authority: (self ? r.authority : r.valid_authority) || null,
      family: r.family || null,
      rank: r.rank || null,
      url: `https://www.marinespecies.org/aphia.php?p=taxdetails&id=${valid}`,
    };
  }

  // Candidates for "Did you mean?": usable species only, each accepted species once, accepted
  // names first, at most MAX.
  function candidates(records) {
    const accepted1st = records.slice().sort((a, b) =>
      (a.status === "accepted" ? 0 : 1) - (b.status === "accepted" ? 0 : 1));
    const seen = new Set();
    const out = [];
    for (const r of accepted1st) {
      if (r.rank !== "Species" || !usable(r)) continue;
      const rec = accepted(r);
      if (!rec.aphia_id || !rec.name || seen.has(rec.aphia_id)) continue;
      seen.add(rec.aphia_id);
      out.push(rec);
      if (out.length === MAX) break;
    }
    return out;
  }

  const choose = (records) => (records.length ? { status: "choose", records, note: null } : NONE);

  // A scientific name: TAXAMATCH (exact, or phonetic and near matches), then names that start
  // the same way.
  async function byScientific(name) {
    const q = encodeURIComponent(name);
    const matched = await get(`/AphiaRecordsByMatchNames?scientificnames[]=${q}&marine_only=false`);
    if (matched === null) return DOWN;
    const records = list(Array.isArray(matched) ? matched[0] : null);
    const exact = records.filter((r) => r.match_type === "exact");
    if (exact.length) {
      const found = candidates(exact);
      // Not a species: a genus or another rank WoRMS accepts, or a name it doesn't accept.
      if (!found.length) return exact.some(usable) ? { status: "genus", records: [], note: null } : NONE;
      if (found.length > 1) return choose(found);   // a name WoRMS holds twice
      const first = exact.find((r) => usable(r) && accepted(r).aphia_id === found[0].aphia_id);
      const synonym = positive(first.AphiaID) !== found[0].aphia_id;
      return { status: "match", records: found,
        note: synonym ? `WoRMS lists this as ${found[0].name}` : null };
    }
    const close = candidates(records);
    if (close.length) return choose(close);
    const started = await get(`/AphiaRecordsByName/${q}?like=true&marine_only=false`);
    if (started === null) return DOWN;
    return choose(candidates(list(started)));
  }

  // A common name alone: WoRMS's vernacular search, the exact name first, then by prefix.
  async function byCommon(name) {
    const q = encodeURIComponent(name);
    for (const like of ["false", "true"]) {
      const found = await get(`/AphiaRecordsByVernacular/${q}?like=${like}`);
      if (found === null) return DOWN;
      const records = candidates(list(found));
      if (records.length) return choose(records);
    }
    return NONE;
  }

  api.check = function check({ scientific, common }) {
    return scientific ? byScientific(scientific) : byCommon(common);
  };

  // The record's English common name from WoRMS, or null (none, or WoRMS isn't answering).
  api.englishName = async function englishName(aphiaId) {
    const names = await get(`/AphiaVernacularsByAphiaID/${Number(aphiaId)}`);
    const eng = list(names).find((n) => n.language_code === "eng" && typeof n.vernacular === "string"
      && n.vernacular.trim());
    return eng ? eng.vernacular.trim().slice(0, 120) : null;
  };

  return api;
})();
