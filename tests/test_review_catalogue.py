"""The species catalogue (spec §9): Species Log + classifier classes +
WoRMS names + photos + named individuals."""

from __future__ import annotations

import json
import os
import threading
import time

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
