"""Enrich the species manifest with full Linnaean taxonomy (kingdom →
phylum → class → order → family → genus → species) from WoRMS — the
World Register of Marine Species (https://www.marinespecies.org/rest/).

Reads ``assets/species_manifest.json`` (built by build_species_manifest.py),
deduplicates by binomial, batch-queries WoRMS, and writes
``assets/taxonomy.csv`` with one row per unique taxon. The CSV is the
input for the taxonomically-aware loss function: given two predicted
species, walk up each row's columns to find the lowest shared rank and
compute tree distance from there.

CSV schema (order is fixed):
    species,genus,family,order,class,phylum,kingdom,lowest

``species`` is the species epithet only (e.g. ``paru``), to match
Linnaean convention; the binomial is reconstructible as ``genus +
species``. ``lowest`` records the most-specific rank we resolved
(``species`` for full matches; ``genus`` / ``family`` for partial
matches where WoRMS recognised the higher rank but couldn't pin down
the species).

WoRMS is the gold-standard taxonomy authority for marine taxa, but it
also accepts terrestrial / freshwater species. We pass
``marine_only=false`` so birds (Anhinga anhinga from the Pier cam
spotting list), terrestrial crustaceans, etc. resolve correctly.

Fallback chain when a binomial has no match:
  1. Try ``/AphiaRecordsByMatchNames`` on the binomial (batch).
  2. If empty, try the same endpoint with just the genus.
  3. If empty, try the family.
  4. Otherwise emit a row with only the manifest-known fields and
     ``lowest=unknown``.

Run idempotently — re-running with the same inputs reproduces the same
CSV. WoRMS responses are cached per-batch (in-memory only) within a
run so a partial failure on one batch doesn't retry every name.
"""
from __future__ import annotations

import csv
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

# Latin binomial / genus shape: capitalised first letter, lowercase rest,
# optionally followed by lowercase species epithet. Used to filter
# placeholders ("TBC", "Ophiothrix sp.", "Anilocra spp./Renocila spp.")
# out of the taxonomy.csv — those are volunteer "to-be-confirmed" markers,
# not real taxa.
LATIN_GENUS_RE = re.compile(r"^[A-Z][a-z]{2,}$")
LATIN_EPITHET_RE = re.compile(r"^[a-z][a-z\-]{1,}$")

try:
    import httpx
except ImportError as e:
    sys.exit(f"httpx is required: {e}")


REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "assets" / "species_manifest.json"
OUT_CSV = REPO_ROOT / "assets" / "taxonomy.csv"

WORMS_BASE = "https://www.marinespecies.org/rest"
WORMS_BATCH_SIZE = 50            # API supports up to 50 names per call
WORMS_TIMEOUT = 30.0


def pick_best_match(matches: list[dict]) -> dict | None:
    """Pick the most authoritative record from a WoRMS match list.

    WoRMS returns a list of candidates per input name — same binomial
    sometimes resolves to multiple records (the "accepted" form plus
    synonyms / unaccepted variants). Prefer ``status='accepted'``; if
    none accepted, fall back to the first non-empty match. Returns the
    record dict or None.
    """
    if not matches:
        return None
    accepted = [m for m in matches if isinstance(m, dict) and m.get("status") == "accepted"]
    if accepted:
        return accepted[0]
    for m in matches:
        if isinstance(m, dict) and m.get("scientificname"):
            return m
    return None


def worms_match_batch(
    client: httpx.Client, names: list[str], marine_only: bool = False,
) -> dict[str, dict | None]:
    """Batch /AphiaRecordsByMatchNames call. Returns {input_name: record-or-None}."""
    if not names:
        return {}
    # Build the param list directly so httpx repeats the key correctly.
    params: list[tuple[str, str]] = [("scientificnames[]", n) for n in names]
    params.append(("marine_only", "true" if marine_only else "false"))
    try:
        r = client.get(f"{WORMS_BASE}/AphiaRecordsByMatchNames",
                       params=params, timeout=WORMS_TIMEOUT)
    except Exception as e:
        print(f"  WoRMS batch failed: {e}", file=sys.stderr)
        return {n: None for n in names}
    if r.status_code == 204:    # no matches at all
        return {n: None for n in names}
    if r.status_code != 200:
        print(f"  WoRMS HTTP {r.status_code}: {r.text[:160]}", file=sys.stderr)
        return {n: None for n in names}

    # Response is a list parallel to `names`: each element is a list of matches.
    data = r.json()
    result: dict[str, dict | None] = {}
    for name, matches in zip(names, data, strict=False):
        rec = pick_best_match(matches if isinstance(matches, list) else [])
        result[name] = rec
    return result


def fetch_taxonomy(unique_binomials: list[str],
                   fallback_genera: dict[str, str],
                   fallback_families: dict[str, str]) -> dict[str, dict[str, Any]]:
    """Resolve every binomial to a taxonomy dict.

    ``fallback_genera``: {binomial: genus_string} for the genus fallback path.
    ``fallback_families``: {binomial: family_string} for the family fallback path.

    Returns {binomial: {kingdom, phylum, class, order, family, genus,
    species, lowest}} where ``species`` is the species epithet (last
    word of the binomial) when known.
    """
    out: dict[str, dict[str, Any]] = {}
    with httpx.Client() as client:
        # Stage 1: batch match the full binomials.
        for i in range(0, len(unique_binomials), WORMS_BATCH_SIZE):
            batch = unique_binomials[i:i + WORMS_BATCH_SIZE]
            print(f"  worms batch {i // WORMS_BATCH_SIZE + 1}: {len(batch)} names")
            res = worms_match_batch(client, batch)
            time.sleep(0.5)   # polite pause between batches
            for name, rec in res.items():
                if rec:
                    out[name] = _flatten(rec, lowest="species",
                                         fallback_species=_species_epithet(name))
                else:
                    out[name] = None

        # Stage 2: genus-only fallback for the still-unresolved.
        unresolved = [n for n, v in out.items() if v is None]
        unresolved_genera = sorted({fallback_genera[n] for n in unresolved if fallback_genera.get(n)})
        if unresolved_genera:
            print(f"  worms genus fallback: {len(unresolved_genera)} unique genera")
            for i in range(0, len(unresolved_genera), WORMS_BATCH_SIZE):
                batch = unresolved_genera[i:i + WORMS_BATCH_SIZE]
                res = worms_match_batch(client, batch)
                time.sleep(0.5)
                for genus, rec in res.items():
                    if not rec:
                        continue
                    flat = _flatten(rec, lowest="genus", fallback_species=None)
                    for name in unresolved:
                        if fallback_genera.get(name) == genus:
                            out[name] = {
                                **flat,
                                # Keep the species epithet visible even though
                                # we couldn't confirm it via WoRMS — it lets a
                                # consumer reconstruct the binomial from the
                                # row, and matches the user-provided spelling.
                                "species": _species_epithet(name),
                            }

        # Stage 3: family-only fallback.
        unresolved = [n for n, v in out.items() if v is None]
        unresolved_families = sorted({fallback_families[n] for n in unresolved if fallback_families.get(n)})
        if unresolved_families:
            print(f"  worms family fallback: {len(unresolved_families)} families")
            for i in range(0, len(unresolved_families), WORMS_BATCH_SIZE):
                batch = unresolved_families[i:i + WORMS_BATCH_SIZE]
                res = worms_match_batch(client, batch)
                time.sleep(0.5)
                for fam, rec in res.items():
                    if not rec:
                        continue
                    flat = _flatten(rec, lowest="family", fallback_species=None)
                    for name in unresolved:
                        if fallback_families.get(name) == fam:
                            out[name] = {
                                **flat,
                                "genus": fallback_genera.get(name),
                                "species": _species_epithet(name),
                                "family": fam,
                            }

        # Stage 4: leave any survivors as manifest-only rows.
        for name, v in out.items():
            if v is None:
                out[name] = {
                    "kingdom": None, "phylum": None, "class": None, "order": None,
                    "family": fallback_families.get(name),
                    "genus": fallback_genera.get(name),
                    "species": _species_epithet(name),
                    "lowest": "unknown",
                }

    return out


def _flatten(rec: dict, *, lowest: str, fallback_species: str | None) -> dict[str, Any]:
    """Project a WoRMS record into our 8-column row shape.

    ``lowest`` is the *requested* resolution level from the caller, but if
    WoRMS only returned a higher-rank record (e.g. we asked for a binomial
    and got back a Genus-rank match), the actual lowest is downgraded so
    the CSV reflects what we genuinely know.
    """
    rank = (rec.get("rank") or "").lower()
    # WoRMS ranks WE care about, in order from finest to coarsest.
    rank_to_lowest = {
        "species": "species", "subspecies": "species",
        "variety": "species", "form": "species",
        "genus": "genus", "subgenus": "genus",
        "family": "family", "subfamily": "family",
        "tribe": "family",
    }
    actual_lowest = rank_to_lowest.get(rank, lowest)
    species = _epithet_from_scientificname(rec.get("scientificname"), rec.get("genus"))
    if actual_lowest != "species":
        # Don't keep a species epithet if WoRMS only resolved to genus / family.
        species = None
    else:
        species = species or fallback_species
    return {
        "kingdom": rec.get("kingdom"),
        "phylum":  rec.get("phylum"),
        "class":   rec.get("class"),
        "order":   rec.get("order"),
        "family":  rec.get("family"),
        "genus":   rec.get("genus"),
        "species": species,
        "lowest":  actual_lowest,
    }


def _species_epithet(binomial: str) -> str | None:
    parts = binomial.strip().split()
    return parts[1] if len(parts) >= 2 else None


def _epithet_from_scientificname(sci: str | None, genus: str | None) -> str | None:
    """Extract the species epithet from WoRMS's ``scientificname`` field.

    WoRMS sometimes returns a name with a subgenus token in parentheses —
    e.g. ``Charybdis (Charybdis) hellerii``. Strip those before picking
    the trailing word so the epithet column doesn't end up holding
    ``(Charybdis)``.
    """
    if not sci:
        return None
    parts = [p for p in sci.strip().split() if not p.startswith("(")]
    if len(parts) >= 2 and genus and parts[0] == genus:
        return parts[1]
    if len(parts) >= 2:
        return parts[1]
    return None


def main() -> None:
    if not MANIFEST_PATH.exists():
        sys.exit(f"missing manifest: {MANIFEST_PATH} — run build_species_manifest.py first")
    records = json.loads(MANIFEST_PATH.read_text())

    # Dedupe by binomial; remember the manifest's family + genus for fallbacks.
    # Filter out volunteer "TBC" placeholders and other non-Latin entries —
    # they pollute the CSV without contributing useful taxonomy.
    seen: dict[str, dict[str, str | None]] = {}
    skipped_placeholders = 0
    for r in records:
        b = r.get("binomial")
        genus = r.get("genus")
        species = r.get("species")
        if not b or not genus:
            skipped_placeholders += 1
            continue
        if not LATIN_GENUS_RE.match(genus):
            skipped_placeholders += 1
            continue
        if species is not None and not LATIN_EPITHET_RE.match(species):
            # Genus is valid Latin but the epithet is "sp.", ".", "-", etc.
            # We keep the row but blank the bad species so the binomial reads
            # as just the genus and we'll fall back to genus-level lookup.
            species = None
            b = genus
        seen.setdefault(b, {
            "family": r.get("family") if r.get("family") and LATIN_GENUS_RE.match(r.get("family") or "") else None,
            "genus": genus,
        })
    if skipped_placeholders:
        print(f"  skipped {skipped_placeholders} placeholder / non-Latin record(s)")

    unique_binomials = sorted(seen)
    fallback_genera = {b: v["genus"] for b, v in seen.items() if v["genus"]}
    fallback_families = {b: v["family"] for b, v in seen.items() if v["family"]}

    print(f"resolving taxonomy for {len(unique_binomials)} unique binomials via WoRMS")
    tax = fetch_taxonomy(unique_binomials, fallback_genera, fallback_families)

    fieldnames = ["species", "genus", "family", "order", "class", "phylum", "kingdom", "lowest"]
    with OUT_CSV.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        # Sort by kingdom → phylum → class → order → family → genus → species
        # so the CSV reads as a coherent tree when opened in Excel.
        rows = sorted(
            tax.values(),
            key=lambda r: tuple((r.get(k) or "").lower() for k in fieldnames),
        )
        for row in rows:
            w.writerow({k: row.get(k) or "" for k in fieldnames})

    # Coverage report
    by_lowest: dict[str, int] = {}
    missing_kingdom = 0
    for v in tax.values():
        by_lowest[v["lowest"]] = by_lowest.get(v["lowest"], 0) + 1
        if not v.get("kingdom"):
            missing_kingdom += 1

    print()
    print(f"wrote {OUT_CSV.relative_to(REPO_ROOT)} ({len(tax)} rows)")
    print("resolution breakdown:")
    for level in ("species", "genus", "family", "unknown"):
        if by_lowest.get(level):
            print(f"  {level}: {by_lowest[level]}")
    print(f"missing kingdom-rank coverage: {missing_kingdom}")


if __name__ == "__main__":
    main()
