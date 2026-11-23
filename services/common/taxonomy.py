"""assets/taxonomy.csv as the review catalogue reads it: raw names (the Species Log's and the
classifier's spellings) mapped to their WoRMS accepted names and AphiaIDs. Shared by the jobs
Lambda (the verified dataset's AphiaIDs) and scripts/assemble_training_set.py (which training
folder a reviewer's label belongs to), so both use the review catalogue's rule.
"""

from __future__ import annotations

import csv
from pathlib import Path


def normalize(raw: str) -> str:
    """'Pomatomus_saltarix ' -> 'Pomatomus saltarix' (review_catalogue.normalize_raw's rule)."""
    return " ".join(raw.replace("_", " ").split())


def read(path: Path) -> dict[str, dict]:
    """Normalized raw_name -> its row; {} when the file is missing."""
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8-sig") as f:
        return {normalize(r["raw_name"]): r for r in csv.DictReader(f) if r.get("raw_name")}


def accepted(rows: dict[str, dict], raw: str) -> str:
    """The accepted name for `raw`, or `raw` normalized when WoRMS did not resolve it."""
    n = normalize(raw)
    return (rows.get(n) or {}).get("accepted_name") or n


def aphia_by_accepted(rows: dict[str, dict]) -> dict[str, int]:
    """Accepted name -> AphiaID (the column holds the accepted taxon's id on every row)."""
    out: dict[str, int] = {}
    for row in rows.values():
        name, aphia = row.get("accepted_name"), (row.get("aphia_id") or "").strip()
        if name and aphia.isdigit():
            out.setdefault(name, int(aphia))
    return out
