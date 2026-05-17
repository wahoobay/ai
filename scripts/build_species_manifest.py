"""Extract a structured manifest from the community Species Log spreadsheet.

The spreadsheet at ``assets/species.xlsx`` is a volunteer-maintained inventory of
species spotted on the Wahoo Bay + Pompano Pier reef cams. Volunteer moderators
attach a Google Drive link (image or short clip) to the common-name cell when
they have a confirmed example of a species — those links are the start of the
fine-tuning dataset for the taxonomically-aware classifier.

This script reads the workbook and emits two artifacts in ``assets/``:

  * ``species_manifest.json`` — one record per (life-stage row) ⨯ (camera),
    with all the fields needed for downstream tools: ``family``, ``genus``,
    ``species``, ``binomial``, ``common_name``, ``life_stage`` parsed from the
    common-name prefix (juvenile / intermediate / adult / unknown),
    ``first_seen`` date, ``cam_wahoo`` / ``cam_pier`` flags, ``drive_id``,
    ``drive_url``, ``source_sheet``, ``source_row``.
  * ``species_manifest.csv`` — same data, flattened, easy to open in Excel
    or feed to a tool that doesn't speak JSON.

Plus a coverage summary printed to stdout: how many records carry a Drive
link, how many of those species are already in the classifier vocabulary,
which species would be new classes to add during fine-tuning, and the
top families by record count.

Scientific-name convention: the spreadsheet's "Scientific name" column is
binomial Latin — ``Genus species`` (genus capitalised, species lowercase).
We split it into separate ``genus`` and ``species`` fields so downstream
code can build a taxonomic distance matrix on top (family → genus →
species edges, eventually augmented with kingdom/phylum/class/order from
WoRMS or GBIF). Trinomial entries (rare; e.g. subspecies) are recorded as
``genus`` + ``species`` + ``subspecies``.
"""
from __future__ import annotations

import csv
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path

try:
    import openpyxl
except ImportError as e:
    sys.exit(f"openpyxl is required: pip install openpyxl ({e})")


REPO_ROOT = Path(__file__).resolve().parent.parent
XLSX_PATH = REPO_ROOT / "assets" / "species.xlsx"
OUT_JSON = REPO_ROOT / "assets" / "species_manifest.json"
OUT_CSV = REPO_ROOT / "assets" / "species_manifest.csv"
CLASSIFIER_LABELS = REPO_ROOT / "data" / "models" / "labels.json"


# Common-name prefixes that encode life stage. The species log lists multiple
# rows per species when juvenile / intermediate / adult colouration differs
# enough to warrant separate examples; preserving the stage gives downstream
# training the option to treat them as the same class with augmentation, or
# as separate sub-classes.
LIFE_STAGE_PATTERNS = [
    (re.compile(r"^juvenile\b",     re.I), "juvenile"),
    (re.compile(r"^juvi\b",         re.I), "juvenile"),
    (re.compile(r"\bjuvi[^a-z]?",    re.I), "juvenile"),
    (re.compile(r"^intermediate\b", re.I), "intermediate"),
    (re.compile(r"^sub-?adult\b",   re.I), "subadult"),
    (re.compile(r"^adult\b",        re.I), "adult"),
    (re.compile(r"^hybrid\b",       re.I), "hybrid"),
]


@dataclass
class ManifestRecord:
    family: str | None
    genus: str | None
    species: str | None
    subspecies: str | None
    binomial: str | None             # "Genus species" (or just "Genus" if species unknown)
    common_name: str
    life_stage: str                  # juvenile | intermediate | adult | hybrid | unknown
    first_seen: str | None           # ISO date "YYYY-MM-DD" or None
    cam_wahoo: bool
    cam_pier: bool
    drive_id: str | None
    drive_url: str | None
    source_sheet: str
    source_row: int
    raw_sci_name: str                # exactly what the volunteer typed, for audit


# ---------- parsing helpers ----------


def parse_binomial(sci: str) -> tuple[str | None, str | None, str | None, str | None]:
    """Split a Latin binomial into (genus, species, subspecies, normalised binomial).

    Handles ``Pomacanthus paru`` (the standard case), trinomial entries like
    ``Cyprinodon variegatus ovinus``, genus-only entries like
    ``Pomacanthus sp.``, and stray punctuation / parentheticals that
    occasionally leak in from common-name notes.
    """
    if not isinstance(sci, str):
        return None, None, None, None
    s = sci.strip()
    if not s:
        return None, None, None, None
    # Strip parentheticals — e.g. "Pomacanthus paru (juvenile)" sometimes
    # appears in the sci column; the life-stage hint belongs in common name.
    s = re.sub(r"\s*\([^)]*\)\s*", " ", s).strip()
    # Strip cf./aff./sp. modifiers — keep the genus but note no species ID.
    s_clean = re.sub(r"\b(cf|aff|sp|spp)\.?\b", "", s, flags=re.I).strip()
    parts = [p for p in re.split(r"\s+", s_clean) if p]
    if not parts:
        return None, None, None, None
    genus = parts[0]
    species = parts[1] if len(parts) > 1 else None
    subspecies = parts[2] if len(parts) > 2 else None
    binomial = f"{genus} {species}".strip() if species else genus
    return genus, species, subspecies, binomial


def parse_life_stage(common: str) -> str:
    if not isinstance(common, str):
        return "unknown"
    for pat, label in LIFE_STAGE_PATTERNS:
        if pat.search(common):
            return label
    return "unknown"


def parse_drive_id(url: str | None) -> str | None:
    """Extract the file ID from a ``drive.google.com/file/d/<ID>/view`` URL."""
    if not isinstance(url, str):
        return None
    m = re.search(r"/d/([A-Za-z0-9_-]+)", url)
    return m.group(1) if m else None


def iso_date(v) -> str | None:
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return None


# ---------- main extraction ----------


def load_pier_cam_flags(wb) -> dict[str, dict[str, bool]]:
    """Build a {binomial: {wahoo: bool, pier: bool}} map from the Pier Cam sheet.

    The sheet uses ``x`` in the Pier / Wahoo columns to mark which camera has
    seen the species; we record presence on either as boolean flags so
    downstream code can split the dataset by camera if needed.
    """
    flags: dict[str, dict[str, bool]] = {}
    if "Pier Cam" not in wb.sheetnames:
        return flags
    s = wb["Pier Cam"]
    # Header row: # | Family | Common Name | Scientific Name | Date | Pier Cam | Wahoo Cam
    for row in range(2, s.max_row + 1):
        sci = s.cell(row, 4).value
        if not isinstance(sci, str) or not sci.strip():
            continue
        genus, species, _sub, binomial = parse_binomial(sci)
        if not binomial:
            continue
        pier = s.cell(row, 6).value
        wahoo = s.cell(row, 7).value
        flags[binomial] = {
            "cam_pier":  bool(pier and str(pier).strip().lower() in ("x", "yes", "1")),
            "cam_wahoo": bool(wahoo and str(wahoo).strip().lower() in ("x", "yes", "1")),
        }
    return flags


def extract_records(wb) -> list[ManifestRecord]:
    pier_flags = load_pier_cam_flags(wb)
    s = wb["Species Log"]
    records: list[ManifestRecord] = []
    # Column order on the Species Log sheet:
    #   1=section header (family group, all-caps) | 2=common name | 3=scientific
    #   4=family | 5=first sighting | ...
    for row in range(6, s.max_row + 1):
        common = s.cell(row, 2).value
        sci = s.cell(row, 3).value
        fam = s.cell(row, 4).value
        first_seen = s.cell(row, 5).value
        link = s.cell(row, 2).hyperlink

        if not common or not isinstance(common, str):
            continue
        # Skip section header rows: all-caps family banners in column 2 with no sci.
        if not isinstance(sci, str) or not sci.strip():
            continue

        genus, species, subspecies, binomial = parse_binomial(sci)
        if not genus:
            continue

        url = link.target if link else None
        drive_id = parse_drive_id(url) if url and "drive.google.com" in url else None
        cams = pier_flags.get(binomial or "", {})

        records.append(ManifestRecord(
            family=str(fam).strip() if fam else None,
            genus=genus,
            species=species,
            subspecies=subspecies,
            binomial=binomial,
            common_name=common.strip(),
            life_stage=parse_life_stage(common),
            first_seen=iso_date(first_seen),
            cam_wahoo=bool(cams.get("cam_wahoo", True)),  # default: assume Wahoo cam if not in Pier sheet
            cam_pier=bool(cams.get("cam_pier", False)),
            drive_id=drive_id,
            drive_url=url if drive_id else None,
            source_sheet="Species Log",
            source_row=row,
            raw_sci_name=sci.strip(),
        ))
    return records


def coverage_summary(records: list[ManifestRecord]) -> dict:
    classifier_classes: set[str] = set()
    if CLASSIFIER_LABELS.exists():
        labels = json.loads(CLASSIFIER_LABELS.read_text())
        classifier_classes = {v.strip() for v in labels.values() if isinstance(v, str)}

    from collections import Counter

    total = len(records)
    with_link = [r for r in records if r.drive_id]
    unique_species = {r.binomial for r in records if r.binomial}
    species_with_link = {r.binomial for r in with_link if r.binomial}
    species_in_classifier = {b for b in unique_species if b in classifier_classes}
    species_new = unique_species - classifier_classes
    fam_counts = Counter(r.family for r in records if r.family)
    life_stage_counts = Counter(r.life_stage for r in records)

    return {
        "total_records": total,
        "records_with_drive_link": len(with_link),
        "unique_species": len(unique_species),
        "unique_species_with_link": len(species_with_link),
        "species_already_in_classifier": len(species_in_classifier),
        "species_new_to_classifier": len(species_new),
        "with_link_already_in_classifier": len(species_with_link & classifier_classes),
        "with_link_new_to_classifier": len(species_with_link - classifier_classes),
        "top_families": fam_counts.most_common(10),
        "life_stage_distribution": dict(life_stage_counts),
        "classifier_class_count": len(classifier_classes),
    }


def main() -> None:
    if not XLSX_PATH.exists():
        sys.exit(f"missing spreadsheet: {XLSX_PATH}")

    wb = openpyxl.load_workbook(XLSX_PATH, data_only=True)
    records = extract_records(wb)

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps([asdict(r) for r in records], indent=2))
    fieldnames = list(asdict(records[0]).keys()) if records else []
    with OUT_CSV.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in records:
            w.writerow(asdict(r))

    summary = coverage_summary(records)
    print(f"wrote {OUT_JSON.relative_to(REPO_ROOT)} ({len(records)} records)")
    print(f"wrote {OUT_CSV.relative_to(REPO_ROOT)}")
    print()
    print("=== coverage ===")
    for k, v in summary.items():
        if k == "top_families":
            print("  top_families:")
            for fam, n in v:
                print(f"    {fam}: {n}")
        elif k == "life_stage_distribution":
            print("  life_stage_distribution:")
            for stage, n in sorted(v.items(), key=lambda kv: -kv[1]):
                print(f"    {stage}: {n}")
        else:
            print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
