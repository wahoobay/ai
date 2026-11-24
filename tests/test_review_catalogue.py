"""The species catalogue (spec §9): Species Log + classifier classes +
WoRMS names + photos + named individuals."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest
from app.review_catalogue import (
    CataloguePaths,
    CatalogueStore,
    load_catalogue,
    normalize_raw,
    slug,
)
from review_fixtures import catalogue_paths, write_review_assets


@pytest.fixture
def cat(tmp_path):
    return load_catalogue(catalogue_paths(write_review_assets(tmp_path)))


def test_normalize_raw():
    assert normalize_raw("  Pomatomus_saltarix ") == "Pomatomus saltarix"


def test_misspelled_names_map_to_the_accepted_name(cat):
    assert cat.accepted("Pomatomus saltarix") == "Pomatomus saltatrix"
    assert cat.accepted("Pomatomus_saltarix") == "Pomatomus saltatrix"
    assert cat.accepted("Unheard of") == "Unheard of"
    assert cat.accepted(None) is None
    assert cat.class_dir("Pomatomus saltatrix") == "Pomatomus_saltarix"
    e = cat.entry("Pomatomus saltatrix", None)
    assert e.resolved and e.aphia_id == 900004 and e.in_species_log and e.in_classifier


def test_life_stages_are_separate_entries(cat):
    assert cat.entry("Pomacanthus paru", "adult").common_name == "Adult French Angelfish"
    assert cat.entry("Pomacanthus paru", "juvenile").common_name == "Juvenile French Angelfish"
    assert cat.entry("Pomacanthus paru", None) is None
    assert cat.entry("Seriola zonata", None) is not None  # 'unknown' stage means none
    assert cat.class_dir("Pomacanthus paru") == "Pomacanthus_paru"


def test_classifier_only_and_unresolved_entries(cat):
    neo = cat.entry("Neopomacentrus cyanomos", None)
    assert neo.in_classifier and not neo.in_species_log
    assert neo.common_name == "Regal Demoiselle"  # from the iNat manifest
    ani = cat.entry("Anilocra ./Renocila", None)
    assert ani is not None and not ani.resolved and not ani.in_classifier
    assert len(cat.entries) == 7  # the row with no binomial is skipped


def test_photos_carry_credit_and_never_leave_the_photo_root(cat):
    path, credit = cat.photo(slug("Seriola zonata"))
    assert path.name == "11.jpg" and "CC BY-NC" in credit
    assert cat.photo(slug("Acanthurus coeruleus")) is None  # its local_path escapes the root


def test_named_individuals_attach_to_their_species(cat):
    assert cat.celebrity_ids("Acanthurus coeruleus") == {"bobby"}
    js = next(x for x in cat.to_json() if x["name"] == "Acanthurus coeruleus")
    assert js["celebrities"] == [{"id": "bobby", "nickname": "Bobby"}]


def test_json_shape(cat):
    js = {x["key"]: x for x in cat.to_json()}
    s = js["Seriola zonata|"]
    assert s["photo_url"] == "/api/review/species/Seriola_zonata/photo.jpg"
    assert s["common"] == "Banded Rudderfish" and s["aphia_id"] == 900005
    assert js["Anilocra ./Renocila|"]["photo_url"] is None


def test_store_rebuilds_when_a_source_file_changes(tmp_path):
    info = write_review_assets(tmp_path)
    store = CatalogueStore(catalogue_paths(info))
    first = store.get()
    assert store.get() is first
    manifest = info["assets"] / "species_manifest.json"
    data = json.loads(manifest.read_text())
    data.append(
        {"binomial": "Caranx crysos", "common_name": "Blue Runner", "life_stage": "unknown"}
    )
    manifest.write_text(json.dumps(data))
    later = time.time_ns() + 10**9
    os.utime(manifest, ns=(later, later))
    second = store.get()
    assert second is not first and second.entry("Caranx crysos", None) is not None


def test_missing_files_give_an_empty_working_catalogue(tmp_path):
    paths = CataloguePaths(
        tmp_path / "a.json",
        tmp_path / "b.json",
        tmp_path / "c.csv",
        tmp_path / "d.json",
        tmp_path / "e.json",
        photo_root=tmp_path,
    )
    cat = load_catalogue(paths)
    assert cat.entries == [] and cat.accepted("X y") == "X y"


def test_a_bad_file_keeps_the_previous_catalogue(review_assets):
    """Phase-1 triage 97: this is what keeps labelling alive when the daily
    poller writes a bad file."""
    paths = catalogue_paths(review_assets)
    store = CatalogueStore(paths)
    before = store.get()
    assert before.has_name("Seriola zonata")
    manifest = paths.species_manifest
    good, st = manifest.read_text(), manifest.stat()
    manifest.write_text("{ not json")
    os.utime(manifest, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert store.get() is before
    manifest.write_text(good)
    os.utime(manifest, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
    after = store.get()
    assert after is not before and after.has_name("Seriola zonata")


def test_a_store_whose_first_load_fails_serves_an_empty_catalogue(review_assets):
    paths = catalogue_paths(review_assets)
    paths.species_manifest.write_text("{ not json")
    assert CatalogueStore(paths).get().names() == []


@pytest.mark.anyio
async def test_aget_rebuilds_off_the_event_loop_and_current_never_rebuilds(
    review_assets, monkeypatch
):
    from app import review_catalogue

    seen = []
    real = review_catalogue.load_catalogue

    def spy(paths):
        seen.append(threading.current_thread() is threading.main_thread())
        return real(paths)

    monkeypatch.setattr(review_catalogue, "load_catalogue", spy)
    store = CatalogueStore(catalogue_paths(review_assets))
    cat = await store.aget()
    assert cat.has_name("Seriola zonata") and seen == [False]
    assert store.current() is cat and seen == [False]


# --- The renamed classifier classes (docs/superpowers/specs/2026-11-22-class-renames-design.md) ---

REPO = Path(__file__).resolve().parents[1]
CLASSES_JSON = REPO / "data" / "models" / "classifier_v2" / "classes.json"
# sha256 of classes.json as published in classifier_v2/dgx-2026-10-24 (its SHA256SUMS).
PUBLISHED_SHA256 = "05acae233f4eeae4db53b9ce3b47721035f68620eab8a0047479fd9d40600b54"


def test_class_list_uses_the_new_names():
    """Only the two names change: same 223 classes in the same order, so the same weights mean
    the same predictions. Undoing the renames gives back the published file byte for byte."""
    import hashlib

    from common.class_renames import CLASS_RENAMES, folder

    text = CLASSES_JSON.read_text()
    classes = json.loads(text)
    assert len(classes) == 223
    assert classes.index("Brachygenys_chrysargyrea") == 85
    assert classes.index("Kyphosus") == 108
    for old, new in CLASS_RENAMES.items():
        assert folder(new) in classes and folder(old) not in classes
        text = text.replace(f'"{folder(new)}"', f'"{folder(old)}"')
    assert hashlib.sha256(text.encode()).hexdigest() == PUBLISHED_SHA256


def test_renamed_classes_attach_to_their_species_log_entries(tmp_path):
    """The Species Log still spells both the old way; the renamed classes must land on those
    entries (resolved, with their WoRMS data), not make new ones."""
    manifest = tmp_path / "species_manifest.json"
    manifest.write_text(
        json.dumps(
            [
                {"binomial": "Haemulon chrysargyreum", "common_name": "Smallmouth Grunt"},
                {"binomial": "Kyphosus sectatrix/bigibbus", "common_name": "Bermuda/Gray Chub"},
            ]
        )
    )
    (tmp_path / "inat.json").write_text("[]")
    (tmp_path / "celebrities.json").write_text("{}")
    cat = load_catalogue(
        CataloguePaths(
            species_manifest=manifest,
            classes_json=CLASSES_JSON,
            taxonomy_csv=REPO / "assets" / "taxonomy.csv",
            inat_manifest=tmp_path / "inat.json",
            celebrities_json=tmp_path / "celebrities.json",
            photo_root=tmp_path,
        )
    )
    for name, class_dir in (
        ("Brachygenys chrysargyrea", "Brachygenys_chrysargyrea"),
        ("Kyphosus", "Kyphosus"),
    ):
        [entry] = [e for e in cat.entries if e.name == name]
        assert (entry.class_dir, entry.in_classifier, entry.in_species_log, entry.resolved) == (
            class_dir,
            True,
            True,
            True,
        )
    assert cat.accepted("Brachygenys chrysargyrea") == "Brachygenys chrysargyrea"
    assert cat.accepted("Haemulon chrysargyreum") == "Brachygenys chrysargyrea"
    assert cat.accepted("Kyphosus") == "Kyphosus"


def test_common_names_for_the_new_names():
    from app.common_names import common

    assert common("Brachygenys chrysargyrea") == "Smallmouth Grunt"
    assert common("Kyphosus") == "Sea Chub"


def test_the_no_animal_class_is_not_offered_as_a_species(tmp_path):
    """A classifier trained on the verified dataset lists no_animal in classes.json (the dashboard
    fetches that file at start); the catalogue must not turn it into a fish to label."""
    info = write_review_assets(tmp_path)
    info["classes"].write_text(json.dumps([*json.loads(info["classes"].read_text()), "no_animal"]))
    cat = load_catalogue(catalogue_paths(info))
    assert not any("no" in n.lower() and "animal" in n.lower() for n in cat.names())
    assert cat.class_dir("Pomatomus saltatrix") == "Pomatomus_saltarix"  # the species still load
