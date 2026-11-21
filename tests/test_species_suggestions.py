"""Reviewers suggest species the list lacks; an admin decides (spec 2026-11-21,
"The species list")."""

from __future__ import annotations

import pytest
from app.review_catalogue import CatalogueStore
from app.review_queue import Conflict, Invalid, fetch_one
from app.species_suggestions import (
    SuggestionIn,
    clean_scientific,
    create_suggestion,
    list_mine,
    merged_catalogue,
)
from psycopg.pq import TransactionStatus
from review_fixtures import CSRF, as_user, catalogue_paths, login, make_user

pytestmark = pytest.mark.anyio


@pytest.fixture
def cat(review_assets):
    return CatalogueStore(catalogue_paths(review_assets)).get()


async def _suggest(rdb, uid, cat, *, role="reviewer", **fields):
    async with rdb.connection() as conn:
        return await create_suggestion(
            conn, user=as_user(uid, role=role), body=SuggestionIn(**fields), catalogue=cat
        )


async def _row(rdb, suggestion_id):
    async with rdb.connection() as conn:
        return await fetch_one(
            conn, "SELECT * FROM species_suggestions WHERE id = %s", (suggestion_id,)
        )


async def _count(rdb):
    async with rdb.connection() as conn:
        return (await fetch_one(conn, "SELECT count(*) AS n FROM species_suggestions"))["n"]


async def _approve(rdb, suggestion_id, name, stage, common):
    async with rdb.connection() as conn:
        await conn.execute(
            """UPDATE species_suggestions
                  SET status = 'approved', approved_name = %s, approved_stage = %s,
                      approved_common = %s, decided_at = now()
                WHERE id = %s""",
            (name, stage, common, suggestion_id),
        )


async def _merged(rdb, cat):
    async with rdb.connection() as conn:
        return await merged_catalogue(conn, cat)


async def test_a_suggestion_is_created_pending_with_its_provisional_name(rdb, cat):
    roy = await make_user(rdb, "roy")
    e = await _suggest(
        rdb,
        roy,
        cat,
        common_name="Juvenile Bluefish",
        scientific_name="Pomatomus  saltarix",
        life_stage="juvenile",
        note="small, near the pier",
    )
    # the accepted name, not the misspelling the suggester typed
    assert e["name"] == "Pomatomus saltatrix" and e["life_stage"] == "juvenile"
    assert e["pending"] is True and e["common"] == "Juvenile Bluefish (waiting for approval)"
    assert e["key"] == f"suggestion:{e['suggestion_id']}"
    assert e["in_species_log"] is False and e["in_classifier"] is False
    row = await _row(rdb, e["suggestion_id"])
    assert row["status"] == "pending" and row["suggested_by"] == roy
    assert row["scientific_name"] == "Pomatomus saltarix"  # what was suggested is kept
    assert row["note"] == "small, near the pier" and row["location_id"] == "wahoobay"

    plain = await _suggest(rdb, roy, cat, common_name="  Spotted   Moray ")
    assert plain["name"] == "Spotted Moray" and plain["life_stage"] is None
    assert plain["common"] == "Spotted Moray (waiting for approval)"


async def test_a_listed_choice_is_refused(rdb, cat):
    roy = await make_user(rdb, "roy")
    with pytest.raises(Conflict, match="^already on the list as Banded Rudderfish$"):
        await _suggest(rdb, roy, cat, common_name="Rudderfish", scientific_name="Seriola zonata")
    with pytest.raises(Conflict, match="^already on the list as Adult French Angelfish$"):
        await _suggest(
            rdb,
            roy,
            cat,
            common_name="Big Angel",
            scientific_name="Pomacanthus paru",
            life_stage="adult",
        )
    # the same common name, in any case and at any stage
    with pytest.raises(Conflict, match="^already on the list as Blue Tang$"):
        await _suggest(rdb, roy, cat, common_name="bLUE tANG")
    with pytest.raises(Conflict, match="^already on the list as Blue Tang$"):
        await _suggest(rdb, roy, cat, common_name="Blue Tang", life_stage="juvenile")
    assert await _count(rdb) == 0


async def test_a_new_stage_of_a_listed_species_is_accepted(rdb, cat):
    roy = await make_user(rdb, "roy")
    assert cat.entry("Pomacanthus paru", "initial phase") is None
    e = await _suggest(
        rdb,
        roy,
        cat,
        common_name="Initial Phase French Angelfish",
        scientific_name="Pomacanthus paru",
        life_stage="initial phase",
    )
    assert e["name"] == "Pomacanthus paru" and e["life_stage"] == "initial phase"


async def test_a_synonym_of_a_listed_choice_is_refused(rdb, cat):
    """Review Focus 3: the fixture taxonomy maps the raw 'Pomatomus saltarix' to the
    listed, stage-less 'Pomatomus saltatrix' (Bluefish)."""
    roy = await make_user(rdb, "roy")
    assert cat.accepted("Pomatomus saltarix") == "Pomatomus saltatrix"
    with pytest.raises(Conflict, match="^already on the list as Bluefish$"):
        await _suggest(rdb, roy, cat, common_name="Tailor", scientific_name="Pomatomus saltarix")
    assert await _count(rdb) == 0


async def test_the_same_reviewer_cannot_suggest_it_twice_while_waiting(rdb, cat):
    roy, ann = await make_user(rdb, "roy"), await make_user(rdb, "ann")
    first = await _suggest(
        rdb, roy, cat, common_name="Spotted Moray", scientific_name="Gymnothorax moringa"
    )
    with pytest.raises(Conflict) as refused:
        await _suggest(rdb, roy, cat, common_name="spotted moray")
    assert refused.value.message == "you already suggested this; it is waiting for approval"
    # another stage is another choice; another reviewer may suggest the same fish
    await _suggest(rdb, roy, cat, common_name="Spotted Moray", life_stage="juvenile")
    await _suggest(
        rdb, ann, cat, common_name="Spotted Moray", scientific_name="Gymnothorax moringa"
    )
    # once decided, it is no longer waiting
    async with rdb.connection() as conn:
        await conn.execute(
            "UPDATE species_suggestions SET status = 'rejected', decided_at = now() WHERE id = %s",
            (first["suggestion_id"],),
        )
    await _suggest(rdb, roy, cat, common_name="Spotted Moray")
    assert await _count(rdb) == 4


async def test_stage_and_scientific_name_are_checked(rdb, cat):
    roy = await make_user(rdb, "roy")
    with pytest.raises(Invalid) as bad_stage:
        await _suggest(rdb, roy, cat, common_name="Spotted Moray", life_stage="larva")
    assert bad_stage.value.message == "pick a stage from the list"
    with pytest.raises(Invalid) as bad_name:
        await _suggest(rdb, roy, cat, common_name="Spotted Moray", scientific_name="seriola")
    assert bad_name.value.message == "write the scientific name as Genus species"
    # the stage is checked first
    with pytest.raises(Invalid, match="^pick a stage from the list$"):
        await _suggest(
            rdb,
            roy,
            cat,
            common_name="Spotted Moray",
            scientific_name="seriola",
            life_stage="larva",
        )
    assert await _count(rdb) == 0

    assert clean_scientific(None) is None and clean_scientific("   ") is None
    assert clean_scientific("  Gymnothorax   moringa ") == "Gymnothorax moringa"
    assert clean_scientific("Scarus") == "Scarus"
    assert clean_scientific("Diplectrum formosum formosum") == "Diplectrum formosum formosum"
    assert clean_scientific("Scorpaena plumieri-like") == "Scorpaena plumieri-like"
    for bad in ("Gymnothorax sp.", "gymnothorax moringa", "Gymnothorax Moringa", "A b c d"):
        with pytest.raises(Invalid):
            clean_scientific(bad)


async def test_a_blank_common_name_is_refused(rdb, cat):
    roy = await make_user(rdb, "roy")
    with pytest.raises(Invalid, match="^write the fish's common name$"):
        await _suggest(rdb, roy, cat, common_name="   ", scientific_name="Gymnothorax moringa")
    assert await _count(rdb) == 0


async def test_pending_entries_belong_to_their_suggester(rdb, rclient):
    await make_user(rdb, "ann")
    await make_user(rdb, "bob")
    await make_user(rdb, "boss", role="admin")
    keys = {}
    for who, common in (("ann", "Spotted Moray"), ("bob", "Goldentail Moray")):
        login(rclient, who)
        r = rclient.post(
            "/api/review/species-suggestions", json={"common_name": common}, headers=CSRF
        )
        assert r.status_code == 200, r.text
        keys[who] = r.json()["key"]

    def pending_keys(who):
        login(rclient, who)
        species = rclient.get("/api/review/species").json()["species"]
        assert any(s["name"] == "Seriola zonata" for s in species)  # the list is still there
        return {s["key"] for s in species if s.get("pending")}

    assert pending_keys("ann") == {keys["ann"]}
    assert pending_keys("bob") == {keys["bob"]}
    assert pending_keys("boss") == {keys["ann"], keys["bob"]}


async def test_an_approved_suggestion_joins_everyones_list(rdb, cat):
    roy = await make_user(rdb, "roy")
    parrot = await _suggest(
        rdb,
        roy,
        cat,
        common_name="Juvenile Striped Parrotfish",
        scientific_name="Scarus iseri",
        life_stage="juvenile",
    )
    angel = await _suggest(
        rdb,
        roy,
        cat,
        common_name="Initial Phase French Angelfish",
        scientific_name="Pomacanthus paru",
        life_stage="initial phase",
    )
    assert (await _merged(rdb, cat)).entry("Scarus iseri", "juvenile") is None  # still waiting

    await _approve(
        rdb, parrot["suggestion_id"], "Scarus iseri", "juvenile", "Juvenile Striped Parrotfish"
    )
    merged = await _merged(rdb, cat)
    added = merged.entry("Scarus iseri", "juvenile")
    assert added is not None and added.common_name == "Juvenile Striped Parrotfish"
    assert not added.in_species_log and not added.in_classifier
    assert any(s["key"] == "Scarus iseri|juvenile" for s in merged.to_json())
    assert merged.entry("Pomacanthus paru", "initial phase") is None  # not approved yet
    assert merged.entry("Seriola zonata", None) is not None  # the files' choices stay
    assert cat.entry("Scarus iseri", "juvenile") is None  # the file catalogue is untouched
    # the stamp is unchanged: the same object
    assert await _merged(rdb, cat) is merged

    await _approve(
        rdb,
        angel["suggestion_id"],
        "Pomacanthus paru",
        "initial phase",
        "Initial Phase French Angelfish",
    )
    rebuilt = await _merged(rdb, cat)
    assert rebuilt is not merged
    assert rebuilt.entry("Scarus iseri", "juvenile") is not None
    stage = rebuilt.entry("Pomacanthus paru", "initial phase")
    assert stage is not None
    # a new stage of a listed species joins it, with its taxonomy
    assert sorted(rebuilt.stages("Pomacanthus paru")) == ["adult", "initial phase", "juvenile"]
    assert (stage.aphia_id, stage.family, stage.resolved) == (900003, "Pomacanthidae", True)
    assert added.aphia_id is None and added.resolved is False  # Scarus iseri is not listed


async def test_merged_catalogue_leaves_the_callers_transaction_alone(rdb, cat):
    """Ruling P1: routes pass the connection they hold. On an idle connection the stamp
    read must not leave a transaction open (a later conn.transaction() would become a
    savepoint and its commit would move); inside one it must not end it."""
    roy = await make_user(rdb, "roy")
    e = await _suggest(rdb, roy, cat, common_name="Spotted Moray")
    async with rdb.connection() as conn:
        assert conn.info.transaction_status == TransactionStatus.IDLE
        await merged_catalogue(conn, cat)  # nothing approved
        assert conn.info.transaction_status == TransactionStatus.IDLE
        await _approve(rdb, e["suggestion_id"], "Gymnothorax moringa", None, "Spotted Moray")
        await merged_catalogue(conn, cat)  # rebuilt
        assert conn.info.transaction_status == TransactionStatus.IDLE
        await merged_catalogue(conn, cat)  # cached
        assert conn.info.transaction_status == TransactionStatus.IDLE

        async with conn.transaction():
            await conn.execute(
                "UPDATE species_suggestions SET note = 'kept' WHERE id = %s",
                (e["suggestion_id"],),
            )
            await merged_catalogue(conn, cat)
            assert conn.info.transaction_status == TransactionStatus.INTRANS
    assert (await _row(rdb, e["suggestion_id"]))["note"] == "kept"


async def test_species_photos_read_the_file_catalogue(rdb, rclient, monkeypatch):
    """Ruling P1a: approved suggestions carry no photos, so a thumbnail costs no database
    round trip for the merge."""
    import app.routes_review as routes_review

    async def no_merge(conn, base):
        raise AssertionError("the photo route merged the catalogue")

    await make_user(rdb, "roy")
    login(rclient, "roy")
    monkeypatch.setattr(routes_review, "merged_catalogue", no_merge)
    r = rclient.get("/api/review/species/Seriola_zonata/photo.jpg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"


async def test_my_list_says_what_happened(rdb, cat):
    roy, ann = await make_user(rdb, "roy"), await make_user(rdb, "ann")
    a = await _suggest(rdb, roy, cat, common_name="Spotted Moray")
    b = await _suggest(rdb, roy, cat, common_name="Goldentail Moray")
    c = await _suggest(rdb, roy, cat, common_name="Purplemouth Moray")
    await _suggest(rdb, ann, cat, common_name="Chain Moray")
    await _approve(rdb, a["suggestion_id"], "Gymnothorax moringa", None, "Spotted Moray")
    async with rdb.connection() as conn:
        await conn.execute(
            """UPDATE species_suggestions
                  SET status = 'rejected', decided_at = now(), decision_note = 'a lizardfish',
                      rejected_to_name = 'Synodus intermedius', rejected_to_stage = NULL
                WHERE id = %s""",
            (b["suggestion_id"],),
        )
        mine = await list_mine(conn, user=as_user(roy))
    assert [m["id"] for m in mine] == [c["suggestion_id"], b["suggestion_id"], a["suggestion_id"]]
    assert [m["status_text"] for m in mine] == ["waiting for approval", "not added", "added"]
    assert mine[1]["decision_note"] == "a lizardfish"
    assert mine[1]["rejected_to_name"] == "Synodus intermedius"
    assert mine[2]["approved_common"] == "Spotted Moray"
    assert set(mine[0]) == {
        "id",
        "common_name",
        "scientific_name",
        "life_stage",
        "status_text",
        "decision_note",
        "rejected_to_name",
        "rejected_to_stage",
        "approved_common",
    }


async def test_suggestion_routes_over_http(rdb, rclient):
    url = "/api/review/species-suggestions"
    body = {
        "common_name": "Spotted Moray",
        "scientific_name": "Gymnothorax moringa",
        "note": "under the pier ladder",
    }
    assert rclient.post(url, json=body, headers=CSRF).status_code == 401
    await make_user(rdb, "watcher", role="viewer")
    login(rclient, "watcher")
    assert rclient.post(url, json=body, headers=CSRF).status_code == 403
    assert rclient.get(f"{url}/mine").status_code == 403

    await make_user(rdb, "roy")
    login(rclient, "roy")
    assert rclient.post(url, json=body).status_code == 403  # no CSRF header
    r = rclient.post(url, json=body, headers=CSRF)
    assert r.status_code == 200, r.text
    entry = r.json()
    assert entry["name"] == "Gymnothorax moringa" and entry["pending"] is True
    assert entry["common"] == "Spotted Moray (waiting for approval)"

    again = rclient.post(url, json=body, headers=CSRF)
    assert again.status_code == 409
    assert again.json()["error"] == "you already suggested this; it is waiting for approval"
    listed = rclient.post(url, json={"common_name": "Blue Tang"}, headers=CSRF)
    assert (
        listed.status_code == 409 and listed.json()["error"] == "already on the list as Blue Tang"
    )
    bad = rclient.post(url, json={"common_name": "Eel", "life_stage": "larva"}, headers=CSRF)
    assert bad.status_code == 400 and bad.json()["error"] == "pick a stage from the list"

    mine = rclient.get(f"{url}/mine").json()["suggestions"]
    assert len(mine) == 1
    assert mine[0]["id"] == entry["suggestion_id"]
    assert mine[0]["common_name"] == "Spotted Moray"
    assert mine[0]["status_text"] == "waiting for approval"
