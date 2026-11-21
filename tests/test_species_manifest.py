"""The Species Log manifest (scripts/build_species_manifest.py) and the review catalogue built
from it: parrotfish and wrasse phases are choices of their own, and the tracked additions file
adds species the CamOps' sheet does not list (Roy, 2026-11-21)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "build_species_manifest", REPO / "scripts" / "build_species_manifest.py"
)
bsm = importlib.util.module_from_spec(_spec)
sys.modules["build_species_manifest"] = bsm  # dataclasses look their module up by name
_spec.loader.exec_module(bsm)


@pytest.mark.parametrize(
    ("common", "stage"),
    [
        ("Yellowtail Parrotfish TP", "terminal phase"),
        ("Yellowtail/Redfin PF (IP)", "initial phase"),
        ("Blue Parrotfish (Initial phase)", "initial phase"),
        ("Hogfish (Terminal phase)", "terminal phase"),
        ("Bluehead Wrasse (TP)", "terminal phase"),
        ("Redband Parrotfish Juvenile ", "juvenile"),
        ("Blue Tang (Juvenile)", "juvenile"),
        ("Stoplight Parrotfish Juvi", "juvenile"),
        ("Juvenile Sheepshead Porgy", "juvenile"),
        ("Intermediate Queen Angelfish", "intermediate"),
        ("Greenblotch Parrotfish", "unknown"),
        ("Bluefish", "unknown"),
        ("Spotted Drum tip", "unknown"),
    ],
)
def test_life_stage_from_the_common_name(common, stage):
    assert bsm.parse_life_stage(common) == stage


def test_the_additions_file_adds_rows_the_sheet_lacks(tmp_path):
    additions = tmp_path / "species_additions.csv"
    additions.write_text(
        "common_name,scientific_name,family,note\n"
        "Striped Parrotfish Juvi,Scarus iseri,Scaridae,Roy 2026-11-21\n"
    )
    [rec] = bsm.load_additions(additions)
    assert (rec.binomial, rec.common_name, rec.life_stage, rec.family) == (
        "Scarus iseri",
        "Striped Parrotfish Juvi",
        "juvenile",
        "Scaridae",
    )
    assert rec.source_sheet == "additions" and rec.source_row == 2
    assert bsm.load_additions(tmp_path / "missing.csv") == []


def test_the_tracked_list_has_roys_missing_parrotfish(tmp_path):
    """Built from the tracked spreadsheet plus the tracked additions, as the image does."""
    import openpyxl

    records = bsm.extract_records(openpyxl.load_workbook(bsm.XLSX_PATH, data_only=True))
    records += bsm.load_additions(bsm.ADDITIONS_CSV)
    stages = {(r.binomial, r.life_stage) for r in records}
    assert ("Sparisoma rubripinne", "initial phase") in stages  # Yellowtail Parrotfish IP
    assert ("Sparisoma rubripinne", "terminal phase") in stages
    assert ("Scarus iseri", "juvenile") in stages  # Juvi Striped Parrotfish

    from app.review_catalogue import CataloguePaths, load_catalogue

    manifest = tmp_path / "species_manifest.json"
    manifest.write_text(json.dumps([r.__dict__ for r in records]))
    cat = load_catalogue(
        CataloguePaths(
            species_manifest=manifest,
            classes_json=tmp_path / "none.json",
            taxonomy_csv=REPO / "assets" / "taxonomy.csv",
            inat_manifest=tmp_path / "none.json",
            celebrities_json=tmp_path / "none.json",
            photo_root=tmp_path,
        )
    )
    assert sorted(cat.stages("Sparisoma rubripinne")) == ["initial phase", "terminal phase"]
    assert cat.entry("Scarus iseri", "juvenile") is not None
    # the species as a whole is named without a phase
    assert cat.common_name("Sparisoma rubripinne") == "Yellowtail Parrotfish"
    assert cat.common_name("Scarus iseri") == "Striped Parrotfish"
    assert cat.common_name("Pomatomus saltatrix") == "Bluefish"
