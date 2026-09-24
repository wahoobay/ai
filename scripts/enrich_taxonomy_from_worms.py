"""Map every species name the project uses to its WoRMS record.

Inputs: the Species Log manifest (``assets/species_manifest.json``) and
the classifier's ``classes.json``. Both contain names as typed by
people, misspellings included. Every distinct name becomes one row of
``assets/taxonomy.csv``, keyed by ``raw_name``, carrying the WoRMS
AphiaID and accepted name, so labels can be stored under the accepted
name while training folders keep their original spelling.

CSV schema (order is fixed):
    raw_name,aphia_id,accepted_name,authority,rank,match_type,status,
    species,genus,family,order,class,phylum,kingdom,lowest

``accepted_name`` is empty when WoRMS found nothing. The last eight
columns keep their earlier meaning (``species`` is the epithet;
``lowest`` is the most specific rank resolved). Names that WoRMS could
not resolve, matched only fuzzily, or matched only to a genus are
written to ``assets/taxonomy_unresolved.txt`` for the Species Log's
maintainers.

WoRMS is the gold-standard taxonomy authority for marine taxa, but it
also accepts terrestrial / freshwater species. We pass
``marine_only=false`` so birds (Anhinga anhinga from the Pier cam
spotting list), terrestrial crustaceans, etc. resolve correctly.

Run idempotently — re-running with the same inputs reproduces the same
CSV. WoRMS responses are cached per-batch (in-memory only) within a
run so a partial failure on one batch doesn't retry every name.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Latin binomial / genus shape: capitalised first letter, lowercase rest,
# optionally followed by lowercase species epithet. Used to filter
# placeholders ("TBC", "Ophiothrix sp.", "Anilocra spp./Renocila spp.")
# out of the taxonomy.csv — those are volunteer "to-be-confirmed" markers,
# not real taxa.
LATIN_GENUS_RE = re.compile(r"^[A-Z][a-z]{2,}$")
LATIN_EPITHET_RE = re.compile(r"^[a-z][a-z\-]{1,}$")

# Second-token spellings that mean "we only know the genus" on purpose —
# distinct from a second token that was *meant* to be a species epithet but
# doesn't parse as Latin (wrong case, a slash, stray punctuation). The
# former is expected and not worth flagging; the latter goes to
# taxonomy_unresolved.txt via unresolved_lines()'s epithet-dropped check.
PLACEHOLDER_EPITHETS = frozenset({"", "sp", "spp", "-"})

try:
    import httpx
except ImportError as e:
    sys.exit(f"httpx is required: {e}")


REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "assets" / "species_manifest.json"
OUT_CSV = REPO_ROOT / "assets" / "taxonomy.csv"
DEFAULT_CLASSES = Path(os.environ.get(
    "CLASSIFIER_V2_CLASSES_JSON",
    str(REPO_ROOT / "data" / "models" / "classifier_v2" / "classes.json")))
OUT_UNRESOLVED = REPO_ROOT / "assets" / "taxonomy_unresolved.txt"
FIELDNAMES = ["raw_name", "aphia_id", "accepted_name", "authority", "rank", "match_type",
              "status", "species", "genus", "family", "order", "class", "phylum",
              "kingdom", "lowest"]

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


def _carry_down(row: dict) -> dict:
    """Propagate the lowest-resolved rank down into the more-specific
    columns so every row exposes a non-empty ``species`` field.

    When WoRMS only resolved to genus, the species column repeats the
    genus; when only to family, both species and genus repeat the family.
    Lets consumers read ``row["species"]`` as "the most specific
    identifier we have for this taxon" without per-row null handling,
    and keeps the ImageFolder-style folder names consistent across the
    dataset (a genus-only class becomes a folder named after the genus
    rather than a confusingly-empty species slot).

    Overwrites unconditionally below ``lowest`` — when WoRMS only got us
    to family, any species epithet the volunteer typed but WoRMS couldn't
    confirm is *not* preserved (it would be a false advertisement of
    precision).
    """
    out = dict(row)
    lowest = out.get("lowest")
    if lowest == "genus":
        out["species"] = out.get("genus")
    elif lowest == "family":
        fam = out.get("family")
        out["genus"] = fam
        out["species"] = fam
    elif lowest == "order":
        ordr = out.get("order")
        for col in ("family", "genus", "species"):
            out[col] = ordr
    elif lowest == "unknown":
        # Leave everything as-is; downstream code can branch on lowest.
        pass
    return out


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


MatchBatch = Callable[[list[str]], dict[str, dict | None]]


def _normalize_epithet(token: str) -> str:
    """Lower-case a second name-token and drop a trailing '.', ',' or ';'
    before it's tested as a placeholder or as Latin — shared by
    clean_query_name and _epithet_dropped so a capitalised or
    punctuation-trailed but otherwise real epithet ('Hystrix',
    'cyanomos.') is recognised the same way in both places."""
    return token.lower().rstrip(".,;")


def clean_query_name(raw: str | None) -> str | None:
    """The name to send to WoRMS: 'Genus epithet', or just 'Genus' when the
    epithet is a placeholder ('sp.', '.', '-'), absent, or not parseable
    as Latin even after normalising (a slash-separated pair, stray
    characters) — or None when the text is not a Latin name at all
    ('TBC'). The epithet is normalised (lower-cased, trailing '.', ',',
    ';' stripped) before either test, so 'Diodon Hystrix' still queries
    as 'Diodon hystrix' and 'Neopomacentrus cyanomos.' as 'Neopomacentrus
    cyanomos' rather than being silently cut to the genus."""
    if not raw:
        return None
    parts = raw.replace("_", " ").split()
    if not parts or not LATIN_GENUS_RE.match(parts[0]):
        return None
    genus = parts[0]
    if len(parts) < 2:
        return genus
    epithet = _normalize_epithet(parts[1])
    if epithet in PLACEHOLDER_EPITHETS or not LATIN_EPITHET_RE.match(epithet):
        return genus
    return f"{genus} {epithet}"


def _epithet_dropped(raw: str) -> bool:
    """True when ``raw`` has a second token that was meant to be a species
    epithet — not a recognised placeholder — but clean_query_name
    couldn't parse it as Latin, so WoRMS only ever saw the genus even
    though the raw name wasn't just a genus. Used by unresolved_lines to
    flag names like 'Kyphosus sectatrix/bigibbus' that a placeholder
    check alone (e.g. 'Kyphosus sp.') would wrongly catch too."""
    parts = raw.replace("_", " ").split()
    if len(parts) < 2:
        return False
    epithet = _normalize_epithet(parts[1])
    return epithet not in PLACEHOLDER_EPITHETS and not LATIN_EPITHET_RE.match(epithet)


def collect_raw_names(manifest_records: list[dict], class_names: list[str]) -> list[str]:
    raws = set()
    for r in manifest_records:
        b = r.get("binomial")
        if b and b.strip():
            raws.add(" ".join(b.split()))
    for c in class_names:
        raws.add(" ".join(c.replace("_", " ").split()))
    return sorted(raws)


def _accepted_fields(rec: dict | None) -> dict:
    if not rec:
        return {"aphia_id": None, "accepted_name": None, "authority": None, "rank": None,
                "match_type": None, "status": None}
    return {
        "aphia_id": rec.get("valid_AphiaID") or rec.get("AphiaID"),
        "accepted_name": rec.get("valid_name") or rec.get("scientificname"),
        "authority": rec.get("valid_authority") or rec.get("authority"),
        "rank": rec.get("rank"),
        "match_type": rec.get("match_type"),
        "status": rec.get("status"),
    }


def _batched(match_batch: MatchBatch, names: list[str]) -> dict[str, dict | None]:
    found: dict[str, dict | None] = {}
    for i in range(0, len(names), WORMS_BATCH_SIZE):
        found.update(match_batch(names[i:i + WORMS_BATCH_SIZE]))
    return found


def resolve_names(raw_names: list[str], match_batch: MatchBatch) -> dict[str, dict]:
    """Resolve each raw name: first as written (cleaned), then, for a
    binomial with no match, by its genus alone."""
    queries = {raw: clean_query_name(raw) for raw in raw_names}
    found = _batched(match_batch, sorted({q for q in queries.values() if q}))
    genera = sorted({q.split()[0] for q in found if found[q] is None and " " in q})
    by_genus = _batched(match_batch, genera) if genera else {}
    out: dict[str, dict] = {}
    for raw, query in queries.items():
        rec, via = None, None
        if query:
            rec = found.get(query)
            via = "name" if rec else None
            if rec is None and " " in query:
                rec = by_genus.get(query.split()[0])
                via = "genus" if rec else None
        out[raw] = {"raw_name": raw, "query": query, "via": via, "record": rec,
                    **_accepted_fields(rec)}
    return out


def build_rows(resolved: dict[str, dict], manifest_records: list[dict]) -> list[dict]:
    families = {" ".join(r["binomial"].split()): r.get("family")
                for r in manifest_records if r.get("binomial")}
    rows = []
    for raw in sorted(resolved):
        res = resolved[raw]
        rec = res["record"]
        if rec:
            # Take the epithet from the accepted name, not the matched one: a
            # misspelled name matches an unaccepted record carrying the typo.
            accepted = {**rec, "scientificname": res["accepted_name"] or rec.get("scientificname")}
            ranks = _carry_down(_flatten(
                accepted, lowest="species" if res["via"] == "name" else "genus",
                fallback_species=_species_epithet(raw)))
        else:
            query = res["query"] or ""
            ranks = {"kingdom": None, "phylum": None, "class": None, "order": None,
                     "family": families.get(raw), "genus": query.split()[0] if query else None,
                     "species": _species_epithet(query) if query else None,
                     "lowest": "unknown"}
        row = {k: res.get(k) for k in ("raw_name", "aphia_id", "accepted_name", "authority",
                                        "rank", "match_type", "status")}
        row.update({k: ranks.get(k) for k in ("species", "genus", "family", "order", "class",
                                              "phylum", "kingdom", "lowest")})
        rows.append(row)
    return rows


def unresolved_lines(resolved: dict[str, dict]) -> list[str]:
    """Tab-separated: raw name, what is wrong, WoRMS's accepted name if any.

    The "rank only" flag covers three ways a row can fall short of
    species level: the genus-fallback path (``via == "genus"``), a
    two-word query that matched directly but only at a higher rank (e.g.
    a family name used as a genus), and a second token that looked like
    it was meant to be a species epithet but couldn't be parsed as Latin
    (``_epithet_dropped`` — a slash-separated pair, stray characters).
    The flag text names the rank WoRMS actually resolved (``"family
    only"`` for a family-rank record), not just "genus only" regardless
    of what really came back.
    """
    lines = []
    for raw in sorted(resolved):
        res = resolved[raw]
        rank = (res.get("rank") or "").lower()
        if res["record"] is None:
            problem = "no match"
        elif (res["via"] == "genus"
              or (res["query"] and " " in res["query"] and rank == "genus")
              or _epithet_dropped(raw)):
            problem = f"{rank} only" if rank else "genus only"
        elif res.get("match_type") and res["match_type"] != "exact":
            problem = f"fuzzy match ({res['match_type']})"
        else:
            continue
        lines.append(f"{raw}\t{problem}\t{res.get('accepted_name') or ''}")
    return lines


def _live_match_batch() -> MatchBatch:
    client = httpx.Client()

    def match(names: list[str]) -> dict[str, dict | None]:
        print(f"  worms: {len(names)} names")
        result = worms_match_batch(client, names)
        time.sleep(0.5)   # polite pause between batches
        return result

    return match


def main(argv: list[str] | None = None, match_batch: MatchBatch | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    ap.add_argument("--classes", type=Path, default=DEFAULT_CLASSES)
    ap.add_argument("--out", type=Path, default=OUT_CSV)
    ap.add_argument("--unresolved", type=Path, default=OUT_UNRESOLVED)
    args = ap.parse_args(argv)

    if not args.manifest.exists():
        print(f"missing manifest: {args.manifest} (run build_species_manifest.py first)",
              file=sys.stderr)
        return 1
    records = json.loads(args.manifest.read_text())
    classes = json.loads(args.classes.read_text()) if args.classes.exists() else []
    if not classes:
        print(f"note: no classifier classes read from {args.classes}", file=sys.stderr)

    raw_names = collect_raw_names(records, classes)
    print(f"resolving {len(raw_names)} distinct names via WoRMS")
    resolved = resolve_names(raw_names, match_batch or _live_match_batch())

    rows = build_rows(resolved, records)
    with args.out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES)
        w.writeheader()
        for row in rows:
            w.writerow({k: ("" if row.get(k) is None else row[k]) for k in FIELDNAMES})
    problems = unresolved_lines(resolved)
    args.unresolved.write_text(
        "# raw_name\tproblem\taccepted_name  (fix these names in the Species Log)\n"
        + "".join(line + "\n" for line in problems))
    print(f"wrote {args.out} ({len(rows)} rows); {len(problems)} names need attention "
          f"(see {args.unresolved})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
