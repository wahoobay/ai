#!/usr/bin/env python
"""Map the old correction form's species names to the species list's accepted names.

The dashboard's older form wrote free-typed and classifier names (such as
"Pomatomus saltarix") straight into detection_corrections. This maps each
such row's corrected_name to the accepted name the review tool uses
(follow-up spec A1b). corrected_species_id is left alone: it can hold a
classifier class id or a Fishial UUID, and is provenance, not a label, so
this never reads it for comparison or writes it (follow-up ruling T3a).
Rows it can't map are listed and left alone.

A dry run by default: it prints what would change and changes nothing.
--apply makes every change in one transaction. On production, run --apply
only after the owner has seen the dry run.

    python scripts/dev/remap_legacy_corrections.py --env /raid/scratch/dzimmerman2021/wahoobay/.env
    python scripts/dev/remap_legacy_corrections.py --env ... --apply
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services" / "dashboard"))

# Old-form rows: every review-tool species verdict records where its label
# came from; the old form never did. An adjudication records no label
# source either, but it isn't an old-form row -- excluded explicitly
# (follow-up ruling T3b) rather than folded into "no label source".
LEGACY_ROWS_SQL = """
    SELECT id, corrected_name
      FROM detection_corrections
     WHERE label_source IS NULL AND corrected_name IS NOT NULL AND NOT is_adjudication
     ORDER BY id
"""


@dataclass
class Report:
    changes: list[tuple[int, str, str]] = field(default_factory=list)   # (id, old, new)
    unmapped: list[tuple[int, str]] = field(default_factory=list)       # (id, name)
    unchanged: int = 0


def remap(conn, catalogue, *, apply: bool) -> Report:
    """`conn` must be in autocommit mode, as main() opens it, so that the
    updates commit as one transaction of their own."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(LEGACY_ROWS_SQL)
        rows = cur.fetchall()
    report = Report()
    for r in rows:
        new = catalogue.accepted(r["corrected_name"])
        if not catalogue.has_name(new):
            report.unmapped.append((r["id"], r["corrected_name"]))
        elif new == r["corrected_name"]:
            report.unchanged += 1
        else:
            report.changes.append((r["id"], r["corrected_name"], new))
    if apply and report.changes:
        with conn.transaction(), conn.cursor() as cur:
            for row_id, _old, new in report.changes:
                cur.execute("UPDATE detection_corrections SET corrected_name = %s WHERE id = %s",
                            (new, row_id))
    return report


def _load_env(path: Path) -> None:
    """The dashboard's own settings, so the catalogue is built from the same
    files it uses. Variables already set win."""
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--env", type=Path, required=True, help=".env file holding DATABASE_URL")
    ap.add_argument("--apply", action="store_true", help="make the changes (default: dry run)")
    args = ap.parse_args(argv)
    _load_env(args.env)
    from app.main import DashboardCfg
    from app.review_catalogue import CatalogueStore

    catalogue = CatalogueStore(DashboardCfg().review_catalogue_paths()).get()
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        report = remap(conn, catalogue, apply=args.apply)
    for row_id, old, new in report.changes:
        print(f"{row_id}\t{old!r} -> {new!r}")
    for row_id, name in report.unmapped:
        print(f"{row_id}\t{name!r}: not in the species list; left alone")
    verb = "changed" if args.apply else "would change"
    print(f"{len(report.changes)} {verb}, {report.unchanged} already accepted, "
          f"{len(report.unmapped)} unmapped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
