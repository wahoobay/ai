"""scripts/enrich_taxonomy_from_worms.py, against a fake WoRMS."""
from __future__ import annotations

import csv
import json
import os
import sys
from pathlib import Path

import httpx
import pytest

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
    # a real species whose raw spelling has a capitalised or
    # punctuation-trailed epithet — must still resolve at species level
    "Diodon hystrix": _rec("Diodon hystrix", 5),
    "Neopomacentrus cyanomos": _rec("Neopomacentrus cyanomos", 6),
    # the bare genus, found directly (not via the genus-fallback path) —
    # covers both the intentional "sp." placeholder and an epithet that
    # couldn't be parsed (a slash-separated pair of species)
    "Kyphosus": _rec("Kyphosus", 7, rank="Genus"),
    # a genus-fallback query that WoRMS resolves at Family rank, not Genus
    "Alligatoridae": _rec("Alligatoridae", 8, rank="Family"),
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


def test_clean_query_name_normalises_the_epithet_before_testing_it():
    # A capitalised epithet or one with a trailing mark is still Latin —
    # normalise before testing, so real species aren't cut to their genus.
    assert enr.clean_query_name("Diodon Hystrix") == "Diodon hystrix"
    assert enr.clean_query_name("Neopomacentrus cyanomos.") == "Neopomacentrus cyanomos"
    # An intentional placeholder still collapses to the genus alone.
    assert enr.clean_query_name("Kyphosus sp.") == "Kyphosus"
    # A slash-separated pair of species doesn't parse as one Latin epithet
    # either, so it also collapses to the genus (but see
    # test_unresolved_lines_flags_a_dropped_epithet_and_words_the_actual_rank:
    # this case is flagged, "sp." is not).
    assert enr.clean_query_name("Kyphosus sectatrix/bigibbus") == "Kyphosus"


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


def test_unresolved_lines_flags_a_dropped_epithet_and_words_the_actual_rank():
    out = enr.resolve_names(
        ["Diodon Hystrix", "Kyphosus sp.", "Kyphosus sectatrix/bigibbus",
         "Alligatoridae mississippiensis"],
        fake_batch)
    text = "\n".join(enr.unresolved_lines(out))
    # A real species, once correctly queried, isn't flagged at all.
    assert "Diodon Hystrix" not in text
    # An intentional "sp." placeholder isn't flagged either.
    assert "Kyphosus sp." not in text
    # But a dropped epithet that wasn't a placeholder is — same genus-only
    # resolution as "Kyphosus sp.", different reason, so it still needs a
    # human to look at the Species Log entry.
    assert "Kyphosus sectatrix/bigibbus\tgenus only\tKyphosus" in text
    # The flag names the rank WoRMS actually resolved, not just "genus".
    assert "Alligatoridae mississippiensis\tfamily only\tAlligatoridae" in text


def test_build_rows_uses_the_normalised_epithet_for_species_and_lowest():
    resolved = enr.resolve_names(["Diodon Hystrix", "Kyphosus sectatrix/bigibbus"], fake_batch)
    rows = {r["raw_name"]: r for r in enr.build_rows(resolved, [])}
    assert rows["Diodon Hystrix"]["genus"] == "Diodon"
    assert rows["Diodon Hystrix"]["species"] == "hystrix"
    assert rows["Diodon Hystrix"]["lowest"] == "species"
    # WoRMS only ever saw "Kyphosus" (the epithet was dropped before the
    # query), so the row is genus-level like any other genus-only match.
    assert rows["Kyphosus sectatrix/bigibbus"]["genus"] == "Kyphosus"
    assert rows["Kyphosus sectatrix/bigibbus"]["species"] == "Kyphosus"
    assert rows["Kyphosus sectatrix/bigibbus"]["lowest"] == "genus"


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


# ---- final review, ledger 87/88: WoRMS failures must not pass for "no match" --
# worms_match_batch gets an httpx client whose transport is a local
# function (httpx.MockTransport), so nothing here touches the network.

def _worms(handler):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return lambda names: enr.worms_match_batch(client, names)


def _unreachable(request):
    raise httpx.ConnectError("connection refused", request=request)


def _answers(status, **kw):
    return lambda request: httpx.Response(status, **kw)


def _run(tmp_path, match_batch, *, classes=("Pomatomus_saltarix",)):
    """main() over a one-name manifest and `classes` (None: no classes.json
    at all), with outputs in tmp_path. Returns (exit code, csv, unresolved)."""
    manifest = tmp_path / "species_manifest.json"
    manifest.write_text(json.dumps([{"binomial": "Seriola zonata", "family": "Carangidae"}]))
    classes_json = tmp_path / "classes.json"
    if classes is not None:
        classes_json.write_text(json.dumps(list(classes)))
    out_csv, unresolved = tmp_path / "taxonomy.csv", tmp_path / "taxonomy_unresolved.txt"
    code = enr.main(["--manifest", str(manifest), "--classes", str(classes_json),
                     "--out", str(out_csv), "--unresolved", str(unresolved)],
                    match_batch=match_batch)
    return code, out_csv, unresolved


@pytest.mark.parametrize("handler", [
    _unreachable,
    _answers(503, text="Service Unavailable"),
    _answers(400, text="Bad Request"),
    _answers(200, json=[]),                     # fewer results than names asked
    _answers(200, text="<html>maintenance</html>"),
], ids=["transport-error", "http-503", "http-400", "short-result", "not-json"])
def test_a_worms_failure_writes_nothing_and_exits_nonzero(tmp_path, handler):
    (tmp_path / "taxonomy.csv").write_text("previous csv\n")
    (tmp_path / "taxonomy_unresolved.txt").write_text("previous list\n")
    code, out_csv, unresolved = _run(tmp_path, _worms(handler))
    assert code != 0
    assert out_csv.read_text() == "previous csv\n"
    assert unresolved.read_text() == "previous list\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "classes.json", "species_manifest.json", "taxonomy.csv", "taxonomy_unresolved.txt"]


def test_worms_no_match_at_all_is_still_just_no_match():
    # HTTP 204 is WoRMS's documented "nothing matched", not a failure
    assert _worms(_answers(204))(["Nothing here"]) == {"Nothing here": None}
    found = _worms(_answers(200, json=[[FAKE["Seriola zonata"]], []]))(
        ["Seriola zonata", "Nothing here"])
    assert found == {"Seriola zonata": FAKE["Seriola zonata"], "Nothing here": None}


@pytest.mark.parametrize("classes", [None, []], ids=["missing", "empty"])
def test_main_fails_without_the_classifier_classes(tmp_path, classes):
    asked = []
    code, out_csv, unresolved = _run(tmp_path, lambda names: asked.append(names) or {},
                                     classes=classes)
    assert code != 0 and asked == []
    assert not out_csv.exists() and not unresolved.exists()


def test_main_replaces_both_outputs_atomically(tmp_path, monkeypatch):
    """The dashboard hot-reloads taxonomy.csv on its mtime, so it must never
    see a half-written file: each output is written to a temporary file in
    the same directory and renamed over the old one, keeping its mode."""
    for name in ("taxonomy.csv", "taxonomy_unresolved.txt"):
        (tmp_path / name).write_text("previous\n")
        (tmp_path / name).chmod(0o644)
    replaced = []
    real_replace = os.replace

    def spy(src, dst):
        replaced.append((Path(src), Path(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(enr.os, "replace", spy)
    code, out_csv, unresolved = _run(tmp_path, fake_batch)
    assert code == 0
    ours = {dst: src for src, dst in replaced if dst in (out_csv, unresolved)}
    assert set(ours) == {out_csv, unresolved}
    assert all(src.parent == tmp_path and src != dst for dst, src in ours.items())
    assert "Pomatomus saltatrix" in out_csv.read_text()
    assert "Pomatomus saltarix" in unresolved.read_text()
    assert {out_csv.stat().st_mode & 0o777, unresolved.stat().st_mode & 0o777} == {0o644}
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "classes.json", "species_manifest.json", "taxonomy.csv", "taxonomy_unresolved.txt"]
