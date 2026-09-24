"""scripts/enrich_taxonomy_from_worms.py, against a fake WoRMS."""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import enrich_taxonomy_from_worms as enr  # noqa: E402


def _rec(name, aphia, *, valid=None, valid_id=None, status="accepted", match="exact",
         rank="Species", family="Fam"):
    genus = name.split()[0]
    return {"AphiaID": aphia, "scientificname": name, "authority": "Someone, 1900",
            "status": status, "rank": rank, "valid_AphiaID": valid_id or aphia,
            "valid_name": valid or name, "valid_authority": "Someone, 1900",
            "match_type": match, "kingdom": "Animalia", "phylum": "Chordata",
            "class": "Teleostei", "order": "Ord", "family": family, "genus": genus}


FAKE = {
    "Seriola zonata": _rec("Seriola zonata", 1),
    # a misspelling: WoRMS matches it phonetically to an unaccepted record
    # whose accepted name is the correct spelling
    "Pomatomus saltarix": _rec("Pomatomus saltarix", 2, valid="Pomatomus saltatrix",
                               valid_id=20, status="unaccepted", match="phonetic"),
    "Anamobaea": _rec("Anamobaea", 3, rank="Genus"),
    "Ghostus": _rec("Ghostus", 4, rank="Genus"),
}


def fake_batch(names):
    return {n: FAKE.get(n) for n in names}


def test_clean_query_name():
    assert enr.clean_query_name("Seriola zonata") == "Seriola zonata"
    assert enr.clean_query_name("Seriola_zonata") == "Seriola zonata"
    assert enr.clean_query_name("Anamobaea .") == "Anamobaea"
    assert enr.clean_query_name("Anilocra ./Renocila") == "Anilocra"
    assert enr.clean_query_name("TBC") is None
    assert enr.clean_query_name("  ") is None


def test_collect_raw_names_merges_log_and_classes():
    raws = enr.collect_raw_names(
        [{"binomial": "Seriola zonata"}, {"binomial": None}, {"binomial": " Anamobaea  . "}],
        ["Seriola_zonata", "Pomatomus_saltarix"])
    assert raws == ["Anamobaea .", "Pomatomus saltarix", "Seriola zonata"]


def test_resolve_names_keeps_the_accepted_name_and_its_id():
    out = enr.resolve_names(["Pomatomus saltarix", "Seriola zonata"], fake_batch)
    assert out["Pomatomus saltarix"]["accepted_name"] == "Pomatomus saltatrix"
    assert out["Pomatomus saltarix"]["aphia_id"] == 20
    assert out["Pomatomus saltarix"]["match_type"] == "phonetic"
    assert out["Seriola zonata"]["accepted_name"] == "Seriola zonata"


def test_resolve_names_falls_back_to_the_genus():
    out = enr.resolve_names(["Ghostus unknownii", "Nothing here"], fake_batch)
    assert out["Ghostus unknownii"]["accepted_name"] == "Ghostus"
    assert out["Ghostus unknownii"]["via"] == "genus"
    assert out["Nothing here"]["accepted_name"] is None


def test_unresolved_lines_flag_fuzzy_genus_only_and_missing():
    out = enr.resolve_names(["Pomatomus saltarix", "Ghostus unknownii", "Nothing here",
                             "Seriola zonata"], fake_batch)
    text = "\n".join(enr.unresolved_lines(out))
    assert "Pomatomus saltarix\tfuzzy match (phonetic)\tPomatomus saltatrix" in text
    assert "Ghostus unknownii\tgenus only\tGhostus" in text
    assert "Nothing here\tno match\t" in text
    assert "Seriola zonata" not in text


def test_main_writes_the_csv_and_the_unresolved_list(tmp_path):
    manifest = tmp_path / "species_manifest.json"
    manifest.write_text(json.dumps([{"binomial": "Seriola zonata", "family": "Carangidae",
                                     "genus": "Seriola"}]))
    classes = tmp_path / "classes.json"
    classes.write_text(json.dumps(["Pomatomus_saltarix"]))
    out_csv = tmp_path / "taxonomy.csv"
    unresolved = tmp_path / "taxonomy_unresolved.txt"
    code = enr.main(["--manifest", str(manifest), "--classes", str(classes),
                     "--out", str(out_csv), "--unresolved", str(unresolved)],
                    match_batch=fake_batch)
    assert code == 0
    rows = {r["raw_name"]: r for r in csv.DictReader(out_csv.open())}
    assert set(rows) == {"Seriola zonata", "Pomatomus saltarix"}
    assert rows["Pomatomus saltarix"]["accepted_name"] == "Pomatomus saltatrix"
    assert rows["Seriola zonata"]["family"] == "Fam"
    assert list(rows["Seriola zonata"])[:3] == ["raw_name", "aphia_id", "accepted_name"]
    assert "Pomatomus saltarix" in unresolved.read_text()
