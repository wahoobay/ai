"""Reviewers add species the list lacks, checked against WoRMS and listed for everyone at once
(spec 2026-11-21-species-worms-check). Pending suggestions left over from ai #49 are still
answered with, hidden and decided by an admin as that spec ("The species list") says; the tests
of those paths make their pending rows with insert_pending."""

from __future__ import annotations

import csv
import io
import random
from datetime import UTC, datetime, timedelta

import pytest
from app.exports import export_corrections, export_labeled_corrections
from app.public_queries import LATEST_VERDICT
from app.review_browse import BrowseFilters, agreement, browse, progress_summary
from app.review_catalogue import CatalogueStore
from app.review_frames import MarkRestIn, frame_state, mark_rest
from app.review_labels import resolve_all
from app.review_pool import PoolSettings, ReviewPool
from app.review_queue import (
    Conflict,
    Forbidden,
    Invalid,
    NotFound,
    VerdictIn,
    fetch_all,
    fetch_one,
    submit_verdict,
)
from app.species_suggestions import (
    ApproveIn,
    RejectIn,
    SuggestionIn,
    approve,
    clean_scientific,
    create_suggestion,
    detach,
    list_mine,
    merged_catalogue,
    pending_species_events,
    reject,
)
from psycopg.pq import TransactionStatus
from review_fixtures import (
    CSRF,
    T0,
    add_item,
    add_verdict,
    as_user,
    catalogue_paths,
    login,
    make_user,
    seed_frame,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def cat(review_assets):
    return CatalogueStore(catalogue_paths(review_assets)).get()


async def _suggest(rdb, uid, cat, *, role="reviewer", **fields):
    async with rdb.connection() as conn:
        return await create_suggestion(
            conn, user=as_user(uid, role=role), body=SuggestionIn(**fields), catalogue=cat
        )


async def insert_pending(
    db,
    *,
    user_id: int,
    common_name: str,
    scientific_name: str | None = None,
    life_stage: str | None = None,
) -> int:
    """A pending suggestion, as ai #49 left them (an addition is approved at once); its id."""
    async with db.connection() as conn:
        row = await fetch_one(
            conn,
            """INSERT INTO species_suggestions (common_name, scientific_name, life_stage,
                                                suggested_by)
               VALUES (%s, %s, %s, %s) RETURNING id""",
            (common_name, scientific_name, life_stage, user_id),
        )
    return row["id"]


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


# The WoRMS records the tests add. 159855 is Pseudupeneus maculatus' AphiaID; the 9001xx ones,
# like the fixture taxonomy's 9000xx, are made up.
GOATFISH = {"aphia_id": 159855, "scientific_name": "Pseudupeneus maculatus", "family": "Mullidae"}
MORAY = {"aphia_id": 900101, "scientific_name": "Gymnothorax moringa", "family": "Muraenidae"}


async def test_an_addition_is_listed_for_everyone_at_once(rdb, rclient, cat):
    roy = await make_user(rdb, "roy")
    await make_user(rdb, "ann")
    e = await _suggest(rdb, roy, cat, **GOATFISH, common_name=None, note=" under the pier ")
    # no common name given: the scientific name stands in
    assert e == {
        "key": "Pseudupeneus maculatus|",
        "name": "Pseudupeneus maculatus",
        "life_stage": None,
        "common": "Pseudupeneus maculatus",
        "suggestion_id": e["suggestion_id"],
    }
    row = await _row(rdb, e["suggestion_id"])
    assert row["status"] == "approved" and row["suggested_by"] == roy
    assert row["common_name"] == row["approved_common"] == "Pseudupeneus maculatus"
    assert row["scientific_name"] == row["approved_name"] == "Pseudupeneus maculatus"
    assert row["life_stage"] is None and row["approved_stage"] is None
    assert (row["aphia_id"], row["family"]) == (159855, "Mullidae")
    assert row["decision_note"] == "WoRMS AphiaID 159855"
    assert row["decided_at"] is not None and row["decided_by"] is None
    assert row["note"] == "under the pier" and row["location_id"] == "wahoobay"

    added = (await _merged(rdb, cat)).entry("Pseudupeneus maculatus", None)
    assert added is not None and added.common_name == "Pseudupeneus maculatus"
    assert (added.aphia_id, added.family, added.rank, added.resolved) == (
        159855,
        "Mullidae",
        "Species",
        True,
    )

    login(rclient, "ann")  # another reviewer has it in her picker, as an ordinary choice
    species = rclient.get("/api/review/species").json()["species"]
    [listed] = [s for s in species if s["name"] == "Pseudupeneus maculatus"]
    assert listed["key"] == e["key"] and not listed.get("pending")
    assert (listed["aphia_id"], listed["family"], listed["resolved"]) == (159855, "Mullidae", True)


async def test_a_listed_choice_is_refused(rdb, cat):
    roy = await make_user(rdb, "roy")
    with pytest.raises(Conflict, match="^already on the list as Banded Rudderfish$"):
        await _suggest(
            rdb,
            roy,
            cat,
            aphia_id=900005,
            common_name="Rudderfish",
            scientific_name="Seriola zonata",
        )
    with pytest.raises(Conflict, match="^already on the list as Adult French Angelfish$"):
        await _suggest(
            rdb,
            roy,
            cat,
            aphia_id=900003,
            common_name="Big Angel",
            scientific_name="Pomacanthus paru",
            life_stage="adult",
        )
    # the same common name at the same stage, in any case
    with pytest.raises(Conflict, match="^already on the list as Blue Tang$"):
        await _suggest(rdb, roy, cat, **MORAY, common_name="bLUE tANG")
    assert await _count(rdb) == 0


async def test_a_common_name_is_judged_with_its_stage(rdb, cat):
    """Ruling X1, narrowed by Ruling W1 (Dan's option a): a stage for a species listed only
    without one is refused now (Roy types "Bluefish", juvenile); a listed species' plain common
    name at a stage it lacks is a new choice only when it is listed with stages. With a stage
    it has, the name is that choice."""
    roy = await make_user(rdb, "roy")
    assert cat.stages("Pomatomus saltatrix") == [None]  # Bluefish, stage-less only
    with pytest.raises(Conflict, match="^already on the list as Bluefish$"):
        await _suggest(
            rdb,
            roy,
            cat,
            aphia_id=900004,
            common_name="Bluefish",
            scientific_name="Pomatomus saltatrix",
            life_stage="juvenile",
        )
    with pytest.raises(Conflict, match="^already on the list as Blue Tang$"):
        await _suggest(
            rdb,
            roy,
            cat,
            aphia_id=900001,
            common_name="Blue Tang",
            scientific_name="Acanthurus coeruleus",
            life_stage="juvenile",
        )

    # Judged by the common name alone (the moray's scientific name is not listed):
    with pytest.raises(Conflict, match="^already on the list as Bluefish$"):
        await _suggest(rdb, roy, cat, **MORAY, common_name="bluefish")
    # French Angelfish is listed as adult and juvenile: the plain name at a listed stage
    with pytest.raises(Conflict, match="^already on the list as Adult French Angelfish$"):
        await _suggest(rdb, roy, cat, **MORAY, common_name="French Angelfish", life_stage="adult")
    # the name written with a stage word, at that same listed stage
    for typed in ("Juvenile French Angelfish", "french angelfish juvi"):
        with pytest.raises(Conflict, match="^already on the list as Juvenile French Angelfish$"):
            await _suggest(rdb, roy, cat, **MORAY, common_name=typed, life_stage="juvenile")
    assert await _count(rdb) == 0

    e = await _suggest(
        rdb,
        roy,
        cat,
        aphia_id=900003,
        common_name="French Angelfish",
        scientific_name="Pomacanthus paru",
        life_stage="intermediate",
    )
    assert e["key"] == "Pomacanthus paru|intermediate"
    assert await _count(rdb) == 1


async def test_staging_refusals(rdb, cat):
    """Dan's option (a): one reviewer can't change how a listed species is staged."""
    roy = await make_user(rdb, "roy")
    angel = {"aphia_id": 900003, "scientific_name": "Pomacanthus paru"}
    assert cat.stages("Pomacanthus paru") == ["adult", "juvenile"]  # only with stages
    with pytest.raises(Conflict) as plain:
        await _suggest(rdb, roy, cat, **angel, common_name="French Angelfish")
    assert plain.value.message == "pick a stage: adult, juvenile, or add a new one"
    assert cat.stages("Seriola zonata") == [None]  # only without one
    with pytest.raises(Conflict, match="^already on the list as Banded Rudderfish$"):
        await _suggest(
            rdb, roy, cat, aphia_id=900005, scientific_name="Seriola zonata", life_stage="adult"
        )
    assert await _count(rdb) == 0

    # still allowed: a new stage of a species listed with stages
    e = await _suggest(
        rdb,
        roy,
        cat,
        **angel,
        common_name="Initial Phase French Angelfish",
        life_stage="initial phase",
    )
    assert e == {
        "key": "Pomacanthus paru|initial phase",
        "name": "Pomacanthus paru",
        "life_stage": "initial phase",
        "common": "Initial Phase French Angelfish",
        "suggestion_id": e["suggestion_id"],
    }
    assert await _count(rdb) == 1


async def test_an_aphia_id_of_a_listed_species_is_a_duplicate(rdb, cat):
    """An addition carrying a listed entry's AphiaID is judged against that entry, whatever
    the name says (900005 is the listed Seriola zonata's, 900003 Pomacanthus paru's)."""
    roy = await make_user(rdb, "roy")
    assert cat.accepted("Seriola zonatus") == "Seriola zonatus"  # the name alone is not listed
    with pytest.raises(Conflict, match="^already on the list as Banded Rudderfish$"):
        await _suggest(rdb, roy, cat, aphia_id=900005, scientific_name="Seriola zonatus")
    with pytest.raises(Conflict, match="^already on the list as Juvenile French Angelfish$"):
        await _suggest(
            rdb,
            roy,
            cat,
            aphia_id=900003,
            scientific_name="Pomacanthus parus",
            life_stage="juvenile",
        )
    assert await _count(rdb) == 0


async def test_a_synonym_of_a_listed_choice_is_refused(rdb, cat):
    """Review Focus 3: the fixture taxonomy maps the raw 'Pomatomus saltarix' to the
    listed, stage-less 'Pomatomus saltatrix' (Bluefish)."""
    roy = await make_user(rdb, "roy")
    assert cat.accepted("Pomatomus saltarix") == "Pomatomus saltatrix"
    with pytest.raises(Conflict, match="^already on the list as Bluefish$"):
        await _suggest(
            rdb,
            roy,
            cat,
            aphia_id=900102,  # not a listed entry's: the name finds it
            common_name="Tailor",
            scientific_name="Pomatomus saltarix",
        )
    assert await _count(rdb) == 0


async def test_a_second_addition_of_the_same_choice_is_refused_even_with_a_stale_catalogue(
    rdb, cat
):
    """Review Focus 1: two reviewers add the same species at nearly the same moment. The
    second read its catalogue before the first committed, so only the check made under the
    lock sees the first; without it, removing one of the two would leave the species listed."""
    ann, bob = await make_user(rdb, "ann"), await make_user(rdb, "bob")
    await _suggest(rdb, ann, cat, **GOATFISH, common_name="Spotted Goatfish")
    assert cat.entry("Pseudupeneus maculatus", None) is None  # the file catalogue lacks it
    with pytest.raises(Conflict, match="^already on the list as Spotted Goatfish$"):
        await _suggest(rdb, bob, cat, **GOATFISH, common_name="Goatfish")
    assert await _count(rdb) == 1


async def test_the_staging_refusals_hold_even_with_a_stale_catalogue(rdb, cat):
    """Review Focus 1, for stages: an addition the catalogue has not seen yet still counts, or
    the species could end up listed both plain and staged, which option (a) forbids."""
    ann, bob = await make_user(rdb, "ann"), await make_user(rdb, "bob")
    # plain first: a stage for it is refused
    await _suggest(rdb, ann, cat, **GOATFISH, common_name="Spotted Goatfish")
    with pytest.raises(Conflict, match="^already on the list as Spotted Goatfish$"):
        await _suggest(rdb, bob, cat, **GOATFISH, life_stage="juvenile")
    # staged first: a plain entry is refused, and another stage is still a new choice
    await _suggest(
        rdb, ann, cat, **MORAY, common_name="Juvenile Spotted Moray", life_stage="juvenile"
    )
    assert cat.stages("Gymnothorax moringa") == []  # the file catalogue lacks it
    with pytest.raises(Conflict) as plain:
        await _suggest(rdb, bob, cat, **MORAY, common_name="Spotted Moray")
    assert plain.value.message == "pick a stage: juvenile, or add a new one"
    await _suggest(rdb, bob, cat, **MORAY, common_name="Adult Spotted Moray", life_stage="adult")
    with pytest.raises(Conflict) as plain:
        await _suggest(rdb, bob, cat, **MORAY, common_name="Spotted Moray")
    assert plain.value.message == "pick a stage: juvenile, adult, or add a new one"
    assert await _count(rdb) == 3


async def test_stage_and_scientific_name_are_checked(rdb, cat):
    roy = await make_user(rdb, "roy")
    with pytest.raises(Invalid) as bad_stage:
        await _suggest(rdb, roy, cat, **MORAY, life_stage="larva")
    assert bad_stage.value.message == "pick a stage from the list"
    for typed in ("seriola", "   "):
        with pytest.raises(Invalid) as bad_name:
            await _suggest(rdb, roy, cat, aphia_id=900101, scientific_name=typed)
        assert bad_name.value.message == "write the scientific name as Genus species", typed
    # the stage is checked first
    with pytest.raises(Invalid, match="^pick a stage from the list$"):
        await _suggest(
            rdb, roy, cat, aphia_id=900101, scientific_name="seriola", life_stage="larva"
        )
    assert await _count(rdb) == 0

    assert clean_scientific(None) is None and clean_scientific("   ") is None
    assert clean_scientific("  Gymnothorax   moringa ") == "Gymnothorax moringa"
    assert clean_scientific("Diplectrum formosum formosum") == "Diplectrum formosum formosum"
    assert clean_scientific("Scorpaena plumieri-like") == "Scorpaena plumieri-like"
    for bad in (
        "Gymnothorax sp.",
        "gymnothorax moringa",
        "Gymnothorax Moringa",
        "A b c d",
        "seriola",
        "Charybdis (charybdis) hellerii",
        "Charybdis (Charybdis)",
    ):
        with pytest.raises(Invalid, match="^write the scientific name as Genus species$"):
            clean_scientific(bad)


async def test_a_genus_and_a_subgenus_name(rdb, cat):
    roy = await make_user(rdb, "roy")
    with pytest.raises(Invalid, match="^pick a species, not a genus$"):
        clean_scientific(" Scarus ")
    with pytest.raises(Invalid, match="^pick a species, not a genus$"):
        await _suggest(rdb, roy, cat, aphia_id=900103, scientific_name="Scarus")
    assert await _count(rdb) == 0

    # WoRMS writes a subgenus in parentheses; the catalogue already uses such names
    crab = "Charybdis (Charybdis) hellerii"
    assert clean_scientific(" Charybdis  (Charybdis) hellerii") == crab
    e = await _suggest(rdb, roy, cat, aphia_id=900104, scientific_name=crab)
    assert e["key"] == f"{crab}|" and e["name"] == e["common"] == crab
    assert (await _row(rdb, e["suggestion_id"]))["approved_name"] == crab


async def test_a_blank_common_name_is_the_scientific_name(rdb, cat):
    roy = await make_user(rdb, "roy")
    e = await _suggest(rdb, roy, cat, **MORAY, common_name="   ")
    assert e["common"] == "Gymnothorax moringa"
    row = await _row(rdb, e["suggestion_id"])
    assert row["common_name"] == row["approved_common"] == "Gymnothorax moringa"
    assert row["family"] == "Muraenidae"


async def test_pending_entries_belong_to_their_suggester(rdb, rclient):
    ann = await make_user(rdb, "ann")
    bob = await make_user(rdb, "bob")
    await make_user(rdb, "boss", role="admin")
    keys = {
        "ann": f"suggestion:{await insert_pending(rdb, user_id=ann, common_name='Spotted Moray')}",
        "bob": f"suggestion:{await insert_pending(rdb, user_id=bob, common_name='Chain Moray')}",
    }

    def pending_keys(who):
        login(rclient, who)
        species = rclient.get("/api/review/species").json()["species"]
        assert any(s["name"] == "Seriola zonata" for s in species)  # the list is still there
        return {s["key"] for s in species if s.get("pending")}

    assert pending_keys("ann") == {keys["ann"]}
    assert pending_keys("bob") == {keys["bob"]}
    assert pending_keys("boss") == {keys["ann"], keys["bob"]}


async def test_an_approved_suggestion_joins_everyones_list(rdb, cat):
    """A pending suggestion left over from ai #49, approved by an admin; it has no AphiaID."""
    roy = await make_user(rdb, "roy")
    parrot = await insert_pending(
        rdb,
        user_id=roy,
        common_name="Juvenile Striped Parrotfish",
        scientific_name="Scarus iseri",
        life_stage="juvenile",
    )
    angel = await insert_pending(
        rdb,
        user_id=roy,
        common_name="Initial Phase French Angelfish",
        scientific_name="Pomacanthus paru",
        life_stage="initial phase",
    )
    assert (await _merged(rdb, cat)).entry("Scarus iseri", "juvenile") is None  # still waiting

    await _approve(rdb, parrot, "Scarus iseri", "juvenile", "Juvenile Striped Parrotfish")
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
        rdb, angel, "Pomacanthus paru", "initial phase", "Initial Phase French Angelfish"
    )
    rebuilt = await _merged(rdb, cat)
    assert rebuilt is not merged
    assert rebuilt.entry("Scarus iseri", "juvenile") is not None
    stage = rebuilt.entry("Pomacanthus paru", "initial phase")
    assert stage is not None
    # a new stage of a listed species joins it, with its taxonomy
    assert sorted(rebuilt.stages("Pomacanthus paru")) == ["adult", "initial phase", "juvenile"]
    assert (stage.aphia_id, stage.family, stage.resolved) == (900003, "Pomacanthidae", True)
    # Scarus iseri is not listed, and the leftover row has no WoRMS record
    assert added.aphia_id is None and added.resolved is False


async def test_merged_catalogue_leaves_the_callers_transaction_alone(rdb, cat):
    """Ruling P1: routes pass the connection they hold. On an idle connection the stamp
    read must not leave a transaction open (a later conn.transaction() would become a
    savepoint and its commit would move); inside one it must not end it."""
    roy = await make_user(rdb, "roy")
    sid = await insert_pending(rdb, user_id=roy, common_name="Spotted Moray")
    async with rdb.connection() as conn:
        assert conn.info.transaction_status == TransactionStatus.IDLE
        await merged_catalogue(conn, cat)  # nothing approved
        assert conn.info.transaction_status == TransactionStatus.IDLE
        await _approve(rdb, sid, "Gymnothorax moringa", None, "Spotted Moray")
        await merged_catalogue(conn, cat)  # rebuilt
        assert conn.info.transaction_status == TransactionStatus.IDLE
        await merged_catalogue(conn, cat)  # cached
        assert conn.info.transaction_status == TransactionStatus.IDLE

        async with conn.transaction():
            await conn.execute(
                "UPDATE species_suggestions SET note = 'kept' WHERE id = %s",
                (sid,),
            )
            await merged_catalogue(conn, cat)
            assert conn.info.transaction_status == TransactionStatus.INTRANS
    assert (await _row(rdb, sid))["note"] == "kept"


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
    a = await insert_pending(rdb, user_id=roy, common_name="Spotted Moray")
    b = await insert_pending(rdb, user_id=roy, common_name="Goldentail Moray")
    c = await insert_pending(rdb, user_id=roy, common_name="Purplemouth Moray")
    await insert_pending(rdb, user_id=ann, common_name="Chain Moray")
    await _approve(rdb, a, "Gymnothorax moringa", None, "Spotted Moray")
    d = (await _suggest(rdb, roy, cat, **GOATFISH, common_name="Spotted Goatfish"))["suggestion_id"]
    async with rdb.connection() as conn:
        await conn.execute(
            """UPDATE species_suggestions
                  SET status = 'rejected', decided_at = now(), decision_note = 'a lizardfish',
                      rejected_to_name = 'Synodus intermedius', rejected_to_stage = NULL
                WHERE id = %s""",
            (b,),
        )
        mine = await list_mine(conn, user=as_user(roy))
    assert [m["id"] for m in mine] == [d, c, b, a]
    assert [m["status_text"] for m in mine] == [
        "added",
        "waiting for approval",  # a leftover from ai #49
        "removed",
        "added",
    ]
    assert (mine[0]["approved_common"], mine[0]["aphia_id"]) == ("Spotted Goatfish", 159855)
    assert mine[2]["decision_note"] == "a lizardfish"
    assert mine[2]["rejected_to_name"] == "Synodus intermedius"
    assert mine[3]["approved_common"] == "Spotted Moray" and mine[3]["aphia_id"] is None
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
        "aphia_id",
    }


async def test_suggestion_routes_over_http(rdb, rclient):
    url = "/api/review/species-suggestions"
    body = {**MORAY, "common_name": "Spotted Moray", "note": "under the pier ladder"}
    assert rclient.post(url, json=body, headers=CSRF).status_code == 401
    await make_user(rdb, "watcher", role="viewer")
    login(rclient, "watcher")
    assert rclient.post(url, json=body, headers=CSRF).status_code == 403
    assert rclient.get(f"{url}/mine").status_code == 403

    await make_user(rdb, "roy")
    login(rclient, "roy")
    assert rclient.post(url, json=body).status_code == 403  # no CSRF header
    for refused in (  # no WoRMS record: nothing is added
        {k: v for k, v in body.items() if k != "aphia_id"},
        {k: v for k, v in body.items() if k != "scientific_name"},
        {**body, "aphia_id": 0},
        {**body, "aphia_id": 2**31},  # past the INTEGER column
        {**body, "scientific_name": ""},
    ):
        assert rclient.post(url, json=refused, headers=CSRF).status_code == 422, refused
    r = rclient.post(url, json=body, headers=CSRF)
    assert r.status_code == 200, r.text
    entry = r.json()
    assert entry == {
        "key": "Gymnothorax moringa|",
        "name": "Gymnothorax moringa",
        "life_stage": None,
        "common": "Spotted Moray",
        "suggestion_id": entry["suggestion_id"],
    }

    again = rclient.post(url, json=body, headers=CSRF)  # listed now
    assert again.status_code == 409
    assert again.json()["error"] == "already on the list as Spotted Moray"
    tang = {"aphia_id": 900001, "scientific_name": "Acanthurus coeruleus"}
    listed = rclient.post(url, json=tang, headers=CSRF)
    assert (
        listed.status_code == 409 and listed.json()["error"] == "already on the list as Blue Tang"
    )
    bad = rclient.post(url, json={**body, "life_stage": "larva"}, headers=CSRF)
    assert bad.status_code == 400 and bad.json()["error"] == "pick a stage from the list"
    genus = rclient.post(url, json={**body, "scientific_name": "Gymnothorax"}, headers=CSRF)
    assert genus.status_code == 400 and genus.json()["error"] == "pick a species, not a genus"

    [mine] = rclient.get(f"{url}/mine").json()["suggestions"]
    assert mine["id"] == entry["suggestion_id"]
    assert mine["common_name"] == "Spotted Moray" and mine["aphia_id"] == 900101
    assert mine["status_text"] == "added"


# ---- answering a box with a pending suggestion ----------------------------------------------

# The fixture taxonomy maps 'Pomatomus saltarix' to the listed, stage-less 'Pomatomus
# saltatrix', so this suggestion's provisional name is the accepted 'Pomatomus saltatrix'.
JUVENILE_BLUEFISH = {
    "common_name": "Juvenile Bluefish",
    "scientific_name": "Pomatomus saltarix",
    "life_stage": "juvenile",
}
NEEDS_ITS_SUGGESTION = "a new species answer needs its suggestion"


async def _box(rdb, tmp_path, n=0, *, claimed_by=None, **item_kw):
    """One saved box with a pass-1 item: served to `claimed_by`, or open."""
    f = await seed_frame(
        rdb,
        tmp_path,
        ts=T0 + timedelta(minutes=10 * n),
        frame_id=10 + n,
        boxes=[{"bbox": [100, 100, 300, 250], "species": "Seriola zonata"}],
    )
    if claimed_by is not None:
        item_kw.update(
            status="claimed",
            claimed_by=claimed_by,
            claimed_until=datetime.now(UTC) + timedelta(minutes=10),
        )
    item = await add_item(rdb, f, predicted="Seriola zonata", **item_kw)
    return f, f["event_ids"][0], item


async def _answer(rdb, cat, uid, *, role="reviewer", spot=0.0, bulk=False, **body):
    async with rdb.connection() as conn:
        return await submit_verdict(
            conn,
            user=as_user(uid, role=role),
            body=VerdictIn(**body),
            catalogue=cat,
            spot_rate=spot,
            rng=random.Random(0),
            bulk=bulk,
        )


def _suggested(sid, **body):
    return {
        "outcome": "species",
        "label_source": "suggested",
        "species_suggestion_id": sid,
        **body,
    }


async def _verdicts(rdb):
    async with rdb.connection() as conn:
        return await fetch_all(conn, "SELECT * FROM detection_corrections ORDER BY id")


async def _item_status(rdb, item_id):
    async with rdb.connection() as conn:
        row = await fetch_one(conn, "SELECT status FROM review_items WHERE id = %s", (item_id,))
    return row["status"]


async def _spot_check_events(rdb):
    async with rdb.connection() as conn:
        rows = await fetch_all(
            conn, "SELECT event_id FROM review_items WHERE pass = 2 ORDER BY event_id"
        )
    return [r["event_id"] for r in rows]


async def test_answering_with_my_suggestion_stores_the_provisional_name(rdb, tmp_path, cat):
    roy = await make_user(rdb, "roy")
    sid = await insert_pending(rdb, user_id=roy, **JUVENILE_BLUEFISH)
    _f, eid, item = await _box(rdb, tmp_path, claimed_by=roy)
    out = await _answer(
        rdb,
        cat,
        roy,
        # the suggestion names the fish; a label and stage sent with it are ignored
        **_suggested(
            sid,
            event_id=eid,
            review_item_id=item,
            label="Seriola zonata",
            life_stage="adult",
        ),
    )
    assert out["label"] == "Pomatomus saltatrix" and out["first_pass"] is True
    [row] = await _verdicts(rdb)
    assert row["id"] == out["id"] and row["review_item_id"] == item
    assert row["label_source"] == "suggested"
    assert row["species_suggestion_id"] == sid
    assert row["corrected_name"] == row["corrected_species_id"] == "Pomatomus saltatrix"
    assert row["life_stage"] == "juvenile"
    assert await _item_status(rdb, item) == "done"


async def test_a_suggested_answer_gets_no_spot_check(rdb, tmp_path, cat):
    roy = await make_user(rdb, "roy")
    sid = await insert_pending(rdb, user_id=roy, **JUVENILE_BLUEFISH)
    _f, eid, item = await _box(rdb, tmp_path, 0, claimed_by=roy)
    _g, control_eid, control_item = await _box(rdb, tmp_path, 1, claimed_by=roy)
    out = await _answer(
        rdb,
        cat,
        roy,
        spot=1.0,
        **_suggested(sid, event_id=eid, review_item_id=item),
    )
    assert out["spot_check_scheduled"] is False
    control = await _answer(
        rdb,
        cat,
        roy,
        spot=1.0,
        event_id=control_eid,
        review_item_id=control_item,
        outcome="species",
        label="Seriola zonata",
        label_source="search",
    )
    assert control["spot_check_scheduled"] is True  # a listed species at that rate gets one
    assert await _spot_check_events(rdb) == [control_eid]


async def test_someone_elses_suggestion_is_refused_but_an_admin_may_use_it(rdb, tmp_path, cat):
    ann, roy = await make_user(rdb, "ann"), await make_user(rdb, "roy")
    boss = await make_user(rdb, "boss", role="admin")
    sid = await insert_pending(rdb, user_id=ann, **JUVENILE_BLUEFISH)
    _f, eid, item = await _box(rdb, tmp_path, 0, claimed_by=roy)
    with pytest.raises(Forbidden) as refused:
        await _answer(rdb, cat, roy, **_suggested(sid, event_id=eid, review_item_id=item))
    assert refused.value.message == "that suggestion isn't yours"
    assert await _verdicts(rdb) == [] and await _item_status(rdb, item) == "claimed"

    _g, boss_eid, boss_item = await _box(rdb, tmp_path, 1, claimed_by=boss)
    out = await _answer(
        rdb,
        cat,
        boss,
        role="admin",
        **_suggested(sid, event_id=boss_eid, review_item_id=boss_item),
    )
    [row] = await _verdicts(rdb)
    assert row["id"] == out["id"] and row["user_id"] == boss
    assert row["species_suggestion_id"] == sid
    assert row["label_source"] == "suggested"


async def test_a_decided_suggestion_cannot_be_used(rdb, tmp_path, cat):
    roy = await make_user(rdb, "roy")
    sid = await insert_pending(rdb, user_id=roy, **JUVENILE_BLUEFISH)
    _f, eid, item = await _box(rdb, tmp_path, claimed_by=roy)
    with pytest.raises(NotFound) as missing:
        await _answer(
            rdb,
            cat,
            roy,
            **_suggested(sid + 1000, event_id=eid, review_item_id=item),
        )
    assert missing.value.message == "no such species suggestion"
    for status in ("approved", "rejected"):
        async with rdb.connection() as conn:
            await conn.execute(
                "UPDATE species_suggestions SET status = %s, decided_at = now() WHERE id = %s",
                (status, sid),
            )
        with pytest.raises(Conflict) as decided:
            await _answer(
                rdb,
                cat,
                roy,
                **_suggested(sid, event_id=eid, review_item_id=item),
            )
        assert (
            decided.value.message
            == "that species suggestion was already decided; pick from the list"
        )
    assert await _verdicts(rdb) == [] and await _item_status(rdb, item) == "claimed"


async def test_suggested_needs_its_suggestion_and_no_bulk(rdb, tmp_path, cat):
    roy = await make_user(rdb, "roy")
    sid = await insert_pending(rdb, user_id=roy, **JUVENILE_BLUEFISH)
    f, eid, item = await _box(rdb, tmp_path, claimed_by=roy)
    box = {"event_id": eid, "review_item_id": item}
    for body in (
        {"outcome": "species", "label_source": "suggested"},
        {
            "outcome": "species",
            "label": "Pomatomus saltatrix",
            "life_stage": "juvenile",
            "label_source": "suggested",
        },
        {"outcome": "no_animal", "label_source": "suggested"},
        {"outcome": "species", "label_source": "search", "species_suggestion_id": sid},
        {"outcome": "species", "species_suggestion_id": sid},
        {"outcome": "accept", "species_suggestion_id": sid},
        {"outcome": "accept", "label_source": "suggested", "species_suggestion_id": sid},
        {"outcome": "unidentifiable", "label_source": "suggested", "species_suggestion_id": sid},
    ):
        with pytest.raises(Invalid) as refused:
            await _answer(rdb, cat, roy, **box, **body)
        assert refused.value.message == NEEDS_ITS_SUGGESTION, body

    for label_source in ("suggested", None):
        with pytest.raises(Invalid) as bulk:
            await _answer(
                rdb,
                cat,
                roy,
                bulk=True,
                **box,
                outcome="species",
                label_source=label_source,
                species_suggestion_id=sid,
            )
        assert bulk.value.message == "only a listed species can be marked with the rest"
    # Mark the rest carries no suggestion, and its label must be on the list.
    async with rdb.connection() as conn:
        with pytest.raises(Invalid, match="^pick a species from the list$"):
            await mark_rest(
                conn,
                saved_frame_id=f["saved_frame_id"],
                user=as_user(roy),
                body=MarkRestIn(label="Pomatomus saltatrix", life_stage="juvenile"),
                catalogue=cat,
                spot_rate=0.0,
                rng=random.Random(0),
            )
    assert await _verdicts(rdb) == [] and await _item_status(rdb, item) == "claimed"


async def test_a_second_opinion_cannot_use_a_suggestion(rdb, tmp_path, cat):
    one, roy = await make_user(rdb, "one"), await make_user(rdb, "roy")
    f, eid, _item = await _box(rdb, tmp_path, status="done")
    first = await add_verdict(rdb, eid, one, name="Seriola zonata")
    spot = await add_item(
        rdb,
        f,
        pass_=2,
        exclude_user_id=one,
        first_correction_id=first,
        status="claimed",
        claimed_by=roy,
        claimed_until=datetime.now(UTC) + timedelta(minutes=10),
    )
    sid = await insert_pending(rdb, user_id=roy, **JUVENILE_BLUEFISH)
    with pytest.raises(Invalid) as refused:
        await _answer(rdb, cat, roy, **_suggested(sid, event_id=eid, review_item_id=spot))
    assert refused.value.message == "a second opinion can't use a new species"
    assert [v["id"] for v in await _verdicts(rdb)] == [first]
    assert await _item_status(rdb, spot) == "claimed"


async def test_a_suggested_answer_without_a_queue_item(rdb, tmp_path, cat):
    """The whole-frame view answers a box that was not served to the reviewer."""
    roy = await make_user(rdb, "roy")
    sid = await insert_pending(rdb, user_id=roy, **JUVENILE_BLUEFISH)
    _f, eid, item = await _box(rdb, tmp_path)  # an open pass-1 item, nobody's claim
    body = _suggested(sid, event_id=eid)
    out = await _answer(rdb, cat, roy, spot=1.0, **body)
    assert out["label"] == "Pomatomus saltatrix" and out["spot_check_scheduled"] is False
    [row] = await _verdicts(rdb)
    assert row["review_item_id"] is None and row["label_source"] == "suggested"
    assert row["species_suggestion_id"] == sid
    assert row["corrected_name"] == "Pomatomus saltatrix" and row["life_stage"] == "juvenile"
    assert await _item_status(rdb, item) == "done"
    assert await _spot_check_events(rdb) == []
    # a double submit is the same answer, not a second row
    again = await _answer(rdb, cat, roy, spot=1.0, **body)
    assert again["duplicate"] is True and again["id"] == out["id"]
    assert again["outcome"] == "species" and again["label"] == "Pomatomus saltatrix"
    assert len(await _verdicts(rdb)) == 1


async def _spot_checks_on(rdb, event_id):
    async with rdb.connection() as conn:
        return await fetch_all(
            conn,
            "SELECT id, status, retired_reason FROM review_items WHERE event_id = %s AND pass = 2",
            (event_id,),
        )


async def test_changing_to_a_new_species_retires_the_boxs_spot_check(rdb, tmp_path, cat):
    """Final review I3: the first answer, from the list, was sampled for a second opinion; the
    reviewer then changes the box to their new species. The second reviewer could not pick
    it, so the check would be a dispute by construction; the admin's approval is its check."""
    roy, two = await make_user(rdb, "roy"), await make_user(rdb, "two")
    sid = await insert_pending(rdb, user_id=roy, **JUVENILE_BLUEFISH)
    listed = {"outcome": "species", "label": "Seriola zonata", "label_source": "search"}
    events = []
    for n in (0, 1):
        _f, eid, item = await _box(rdb, tmp_path, n, claimed_by=roy)
        first = await _answer(rdb, cat, roy, spot=1.0, event_id=eid, review_item_id=item, **listed)
        assert first["spot_check_scheduled"] is True
        events.append(eid)
    changed, control = events

    out = await _answer(rdb, cat, roy, spot=1.0, **_suggested(sid, event_id=changed))
    assert out["first_pass"] is True and out["spot_check_scheduled"] is False
    [spot] = await _spot_checks_on(rdb, changed)  # retired, and no new one
    assert (spot["status"], spot["retired_reason"]) == ("retired", "new_species")
    with pytest.raises(Conflict) as retired:
        await _answer(rdb, cat, two, event_id=changed, review_item_id=spot["id"], **listed)
    assert retired.value.message == "this box was retired: an admin checks its answer instead"

    # Changed to another listed species: the second opinion still stands, as before.
    await _answer(
        rdb,
        cat,
        roy,
        spot=1.0,
        event_id=control,
        outcome="species",
        label="Acanthurus coeruleus",
        label_source="search",
    )
    [kept] = await _spot_checks_on(rdb, control)
    assert (kept["status"], kept["retired_reason"]) == ("open", None)


async def test_suggested_answer_over_http(rdb, rclient, tmp_path):
    roy = await make_user(rdb, "roy")
    login(rclient, "roy")
    sid = await insert_pending(rdb, user_id=roy, **JUVENILE_BLUEFISH)
    _f, eid, _item = await _box(rdb, tmp_path)
    url = "/api/review/verdicts"
    missing = rclient.post(url, json=_suggested(sid + 1000, event_id=eid), headers=CSRF)
    assert missing.status_code == 404
    assert missing.json()["error"] == "no such species suggestion"
    r = rclient.post(url, json=_suggested(sid, event_id=eid), headers=CSRF)
    assert r.status_code == 200, r.text
    assert r.json()["label"] == "Pomatomus saltatrix"
    [row] = await _verdicts(rdb)
    assert row["species_suggestion_id"] == sid


# ---- keeping pending answers out ------------------------------------------------------------

# A new stage of a listed species: its provisional name is the listed 'Seriola zonata', so a
# pending answer would land in that species' training rows and counts unless it is kept out.
JUVENILE_RUDDERFISH = {
    "common_name": "Juvenile Banded Rudderfish",
    "scientific_name": "Seriola zonata",
    "life_stage": "juvenile",
}


async def _pending_and_control(rdb, tmp_path, cat):
    """Two boxes roy answered 'Seriola zonata': `pending` with his pending suggestion,
    `control` from the list."""
    roy = await make_user(rdb, "roy")
    sid = await insert_pending(rdb, user_id=roy, **JUVENILE_RUDDERFISH)
    f, pending, item = await _box(rdb, tmp_path, 0, claimed_by=roy)
    out = await _answer(rdb, cat, roy, **_suggested(sid, event_id=pending, review_item_id=item))
    g, control, control_item = await _box(rdb, tmp_path, 1, claimed_by=roy)
    control_out = await _answer(
        rdb,
        cat,
        roy,
        event_id=control,
        review_item_id=control_item,
        outcome="species",
        label="Seriola zonata",
        label_source="search",
    )
    return {
        "roy": roy,
        "sid": sid,
        "pending": pending,
        "control": control,
        "frames": {pending: f, control: g},
        "first": {pending: out["id"], control: control_out["id"]},
    }


async def _export_rows(stream) -> list[dict]:
    return list(csv.DictReader(io.StringIO("".join([line async for line in stream]))))


async def _export_events(stream) -> list[int]:
    return [int(r["event_id"]) for r in await _export_rows(stream)]


async def _latest_verdict(rdb, event_id):
    async with rdb.connection() as conn:
        return await fetch_all(
            conn,
            f"""SELECT v.* FROM detection_events de CROSS JOIN LATERAL ({LATEST_VERDICT}) v
                 WHERE de.id = %s""",
            (event_id,),
        )


async def _verified(rdb, review_assets, name):
    """`name`'s verified count as the review pool's refresh_progress computes it."""
    store = CatalogueStore(catalogue_paths(review_assets))
    pool = ReviewPool(rdb, PoolSettings(target=200), store, review_assets["baseline"])
    await pool.refresh_progress()
    return pool, pool.verified(name)


async def test_pending_boxes_are_left_out_of_the_training_export(rdb, tmp_path, cat):
    w = await _pending_and_control(rdb, tmp_path, cat)
    for include_identity in (False, True):  # administrators' downloads too
        rows = await _export_events(export_labeled_corrections(rdb, include_identity))
        assert rows == [w["control"]], include_identity
    # The whole box waits: a later answer from the list on it stays out as well.
    boss = await make_user(rdb, "boss", role="admin")
    await add_verdict(rdb, w["pending"], boss, name="Seriola zonata", label_source="search")
    for include_identity in (False, True):
        rows = await _export_events(export_labeled_corrections(rdb, include_identity))
        assert rows == [w["control"]], include_identity


async def test_pending_boxes_are_left_out_of_public_and_counts(rdb, tmp_path, cat, review_assets):
    w = await _pending_and_control(rdb, tmp_path, cat)
    public = await _export_events(export_corrections(rdb, hours=None, reviewer=None))
    assert public == [w["control"]]
    # Final review I2 (ruling): an administrator's corrections file leaves it out too. It has no
    # column saying an answer is provisional, and the spec's anti-goal names this file with no
    # admin exception; the New species panel is where admins see these answers.
    admin = await _export_events(
        export_corrections(rdb, hours=None, reviewer=None, include_identity=True)
    )
    assert admin == [w["control"]]

    assert await _latest_verdict(rdb, w["pending"]) == []
    [control] = await _latest_verdict(rdb, w["control"])
    assert control["corrected_name"] == "Seriola zonata"

    pool, verified = await _verified(rdb, review_assets, "Seriola zonata")
    assert verified == 1  # the control only
    async with rdb.connection() as conn:
        p = await progress_summary(conn, pool_obj=pool, catalogue=cat, session_id=None, target=200)
    assert p["kpis"]["verified_boxes"] == 1
    seriola = next(s for s in p["species"] if s["name"] == "Seriola zonata")
    assert seriola["verified"] == 1


async def test_pending_answers_are_left_out_of_the_live_pool_count(
    rdb, rclient, tmp_path, monkeypatch
):
    """Until the next refresh_progress, the pool counts what the verdict route reports
    (`counts`); a pending answer must not be noted."""
    calls = []
    monkeypatch.setattr(
        "app.review_pool.ReviewPool.note_verified", lambda self, name: calls.append(name)
    )
    roy = await make_user(rdb, "roy")
    login(rclient, "roy")
    sid = await insert_pending(rdb, user_id=roy, **JUVENILE_RUDDERFISH)
    _f, pending, _i = await _box(rdb, tmp_path, 0)
    _g, control, _j = await _box(rdb, tmp_path, 1)
    url = "/api/review/verdicts"
    body = _suggested(sid, event_id=pending)
    r = rclient.post(url, json=body, headers=CSRF)
    assert r.status_code == 200, r.text
    assert r.json()["label"] == "Seriola zonata" and r.json()["counts"] is False
    again = rclient.post(url, json=body, headers=CSRF).json()  # a double submit
    assert again["duplicate"] is True and again["counts"] is False
    listed = {
        "event_id": control,
        "outcome": "species",
        "label": "Seriola zonata",
        "label_source": "search",
    }
    r = rclient.post(url, json=listed, headers=CSRF)
    assert r.status_code == 200 and r.json()["counts"] is True
    assert calls == ["Seriola zonata"]


async def test_pending_boxes_are_left_out_of_agreement(rdb, tmp_path, cat):
    w = await _pending_and_control(rdb, tmp_path, cat)
    two = await make_user(rdb, "two")
    for eid in (w["pending"], w["control"]):
        spot = await add_item(
            rdb,
            w["frames"][eid],
            pass_=2,
            status="done",
            exclude_user_id=w["roy"],
            first_correction_id=w["first"][eid],
        )
        await add_verdict(rdb, eid, two, name="Seriola zonata", item_id=spot)
    async with rdb.connection() as conn:
        a = await agreement(conn)
    assert a["overall"]["pairs"] == 1  # the control's pair only
    assert set(a["by_label_source"]) == {"search"}


async def test_after_approval_the_box_is_back(rdb, tmp_path, cat, review_assets):
    w = await _pending_and_control(rdb, tmp_path, cat)
    await _approve(rdb, w["sid"], "Seriola zonata", "juvenile", "Juvenile Banded Rudderfish")
    for include_identity in (False, True):
        rows = {
            int(r["event_id"]): r
            for r in await _export_rows(export_labeled_corrections(rdb, include_identity))
        }
        assert set(rows) == {w["pending"], w["control"]}, include_identity
        back = rows[w["pending"]]
        assert back["corrected_name"] == back["corrected_species_id"] == "Seriola zonata"
        assert back["life_stage"] == "juvenile" and back["label_source"] == "suggested"
    assert [v["corrected_name"] for v in await _latest_verdict(rdb, w["pending"])] == [
        "Seriola zonata"
    ]
    _pool, verified = await _verified(rdb, review_assets, "Seriola zonata")
    assert verified == 2


async def test_pending_species_events_names_each_waiting_box(rdb, tmp_path, cat):
    w = await _pending_and_control(rdb, tmp_path, cat)
    named = {w["pending"]: "Juvenile Banded Rudderfish"}
    async with rdb.connection() as conn:
        assert await pending_species_events(conn) == named
        assert await pending_species_events(conn, [w["pending"], w["control"]]) == named
        assert await pending_species_events(conn, [w["control"]]) == {}
        assert await pending_species_events(conn, []) == {}
    await _approve(rdb, w["sid"], "Seriola zonata", "juvenile", "Juvenile Banded Rudderfish")
    async with rdb.connection() as conn:
        assert await pending_species_events(conn) == {}


# ---- an admin decides -----------------------------------------------------------------------


async def _decide(rdb, fn, boss, sid, **kw):
    """`fn` (approve, reject or detach) as admin `boss`, on suggestion `sid`."""
    async with rdb.connection() as conn:
        return await fn(conn, user=as_user(boss, role="admin"), suggestion_id=sid, **kw)


async def _answered_with(rdb, tmp_path, cat, uid, sid, n, **body):
    """Box `n`, served to `uid` and answered with suggestion `sid`; its event id."""
    _f, eid, item = await _box(rdb, tmp_path, n, claimed_by=uid)
    await _answer(rdb, cat, uid, **_suggested(sid, event_id=eid, review_item_id=item, **body))
    return eid


async def _linked(rdb, sid):
    async with rdb.connection() as conn:
        return await fetch_all(
            conn,
            "SELECT * FROM detection_corrections WHERE species_suggestion_id = %s ORDER BY id",
            (sid,),
        )


async def _training_rows(rdb, event_id):
    """`event_id`'s rows in the training export; none while the box is kept out."""
    rows = await _export_rows(export_labeled_corrections(rdb))
    return [r for r in rows if int(r["event_id"]) == event_id]


async def test_approve_settles_every_linked_answer(rdb, tmp_path, cat):
    roy, boss = await make_user(rdb, "roy"), await make_user(rdb, "boss", role="admin")
    sid = await insert_pending(rdb, user_id=roy, common_name="Almaco Jack")
    events = [await _answered_with(rdb, tmp_path, cat, roy, sid, n) for n in (0, 1)]
    before = await _linked(rdb, sid)
    assert [r["corrected_name"] for r in before] == ["Almaco Jack", "Almaco Jack"]
    assert [r["is_endorsement"] for r in before] == [False, False]

    body = ApproveIn(
        scientific_name=" Seriola  zonata ",
        common_name="Juvenile Banded Rudderfish",
        life_stage="Juvenile",
        note="a young rudderfish",
    )
    assert await _decide(rdb, approve, boss, sid, body=body, catalogue=cat) == {"answers": 2}
    after = await _linked(rdb, sid)
    assert [r["event_id"] for r in after] == events
    for r in after:
        assert r["corrected_name"] == r["corrected_species_id"] == "Seriola zonata"
        assert r["life_stage"] == "juvenile" and r["label_source"] == "suggested"
        assert r["is_endorsement"] is True  # the model guessed Seriola zonata on these boxes
    row = await _row(rdb, sid)
    assert row["status"] == "approved"
    assert row["decided_by"] == boss and row["decided_at"] is not None
    assert row["decision_note"] == "a young rudderfish"
    assert (row["approved_name"], row["approved_common"], row["approved_stage"]) == (
        "Seriola zonata",
        "Juvenile Banded Rudderfish",
        "juvenile",
    )
    # the row keeps what was suggested
    assert (row["common_name"], row["scientific_name"], row["life_stage"]) == (
        "Almaco Jack",
        None,
        None,
    )
    added = (await _merged(rdb, cat)).entry("Seriola zonata", "juvenile")
    assert added is not None and added.common_name == "Juvenile Banded Rudderfish"


async def test_approve_checks_the_names_and_stage(rdb, tmp_path, cat):
    roy, boss = await make_user(rdb, "roy"), await make_user(rdb, "boss", role="admin")
    sid = await insert_pending(rdb, user_id=roy, common_name="Almaco Jack")
    await _answered_with(rdb, tmp_path, cat, roy, sid, 0)
    before = await _verdicts(rdb)
    for body, message in (
        ({"scientific_name": "seriola"}, "write the scientific name as Genus species"),
        ({"scientific_name": "   "}, "write the scientific name as Genus species"),
        ({"common_name": "   "}, "write the fish's common name"),
        ({"life_stage": "larva"}, "pick a stage from the list"),
    ):
        fields = {"scientific_name": "Seriola rivoliana", "common_name": "Almaco Jack", **body}
        with pytest.raises(Invalid) as refused:
            await _decide(rdb, approve, boss, sid, body=ApproveIn(**fields), catalogue=cat)
        assert refused.value.message == message, body
    ok = ApproveIn(scientific_name="Seriola rivoliana", common_name="Almaco Jack")
    with pytest.raises(NotFound, match="^no such species suggestion$"):
        await _decide(rdb, approve, boss, sid + 1000, body=ok, catalogue=cat)
    assert await _verdicts(rdb) == before
    assert (await _row(rdb, sid))["status"] == "pending"


async def test_approving_into_a_listed_choice_is_refused(rdb, tmp_path, cat):
    """Review Focus 1: two reviewers suggest the same species, and the admin approves both."""
    ann, bob = await make_user(rdb, "ann"), await make_user(rdb, "bob")
    boss = await make_user(rdb, "boss", role="admin")
    first = await insert_pending(
        rdb, user_id=ann, common_name="Spotted Moray", scientific_name="Gymnothorax moringa"
    )
    second = await insert_pending(rdb, user_id=bob, common_name="Spotted Moray")
    await _answered_with(rdb, tmp_path, cat, ann, first, 0)
    eid = await _answered_with(rdb, tmp_path, cat, bob, second, 1)
    body = ApproveIn(scientific_name="Gymnothorax moringa", common_name="Spotted Moray")
    out = await _decide(rdb, approve, boss, first, body=body, catalogue=cat)
    assert out == {"answers": 1}

    merged = await _merged(rdb, cat)
    with pytest.raises(Conflict) as refused:
        await _decide(rdb, approve, boss, second, body=body, catalogue=merged)
    assert refused.value.message == (
        "already listed as Spotted Moray; reject it as 'it's actually Spotted Moray' instead"
    )
    assert (await _row(rdb, second))["status"] == "pending"

    as_first = RejectIn(as_label="Gymnothorax moringa", note="the same fish as Ann's")
    # Ruling P3: the approved species is a choice only in the merged list
    with pytest.raises(Invalid, match="^pick a species from the list$"):
        await _decide(rdb, reject, boss, second, body=as_first, catalogue=cat)
    out = await _decide(rdb, reject, boss, second, body=as_first, catalogue=merged)
    assert out == {"answers": 1}
    [moved] = await _linked(rdb, second)
    assert moved["event_id"] == eid and moved["user_id"] == bob
    assert moved["corrected_name"] == moved["corrected_species_id"] == "Gymnothorax moringa"
    row = await _row(rdb, second)
    assert row["status"] == "rejected" and row["rejected_to_name"] == "Gymnothorax moringa"
    [back] = await _training_rows(rdb, eid)
    assert back["corrected_name"] == "Gymnothorax moringa"


async def test_approval_leaves_a_newer_answer_alone(rdb, tmp_path, cat):
    """Review Focus 2: the suggester changed the box to a listed species while it waited."""
    roy, boss = await make_user(rdb, "roy"), await make_user(rdb, "boss", role="admin")
    sid = await insert_pending(rdb, user_id=roy, common_name="Almaco Jack")
    eid = await _answered_with(rdb, tmp_path, cat, roy, sid, 0)
    newer = await _answer(
        rdb,
        cat,
        roy,
        event_id=eid,
        outcome="species",
        label="Seriola zonata",
        label_source="search",
    )
    body = ApproveIn(scientific_name="Seriola rivoliana", common_name="Almaco Jack")
    assert await _decide(rdb, approve, boss, sid, body=body, catalogue=cat) == {"answers": 1}
    rows = await _verdicts(rdb)
    assert [(r["corrected_name"], r["species_suggestion_id"]) for r in rows] == [
        ("Seriola rivoliana", sid),
        ("Seriola zonata", None),
    ]
    assert rows[1]["id"] == newer["id"] and rows[1]["label_source"] == "search"
    async with rdb.connection() as conn:
        resolved = (await resolve_all(conn, [eid]))[eid]
    assert resolved.species == "Seriola zonata" and resolved.life_stage is None


async def test_reject_as_a_listed_species_moves_the_answers(rdb, tmp_path, cat):
    roy, boss = await make_user(rdb, "roy"), await make_user(rdb, "boss", role="admin")
    sid = await insert_pending(rdb, user_id=roy, **JUVENILE_BLUEFISH)
    eid = await _answered_with(rdb, tmp_path, cat, roy, sid, 0)
    assert await _training_rows(rdb, eid) == []  # waiting

    body = RejectIn(as_label="Seriola zonata", note="a juvenile rudderfish")
    out = await _decide(rdb, reject, boss, sid, body=body, catalogue=await _merged(rdb, cat))
    assert out == {"answers": 1}
    [row] = await _linked(rdb, sid)
    assert row["event_id"] == eid
    assert row["corrected_name"] == row["corrected_species_id"] == "Seriola zonata"
    assert row["life_stage"] is None and row["unidentifiable"] is False
    assert row["is_endorsement"] is True  # the model's guess, now
    assert row["label_source"] == "suggested"
    s = await _row(rdb, sid)
    assert s["status"] == "rejected" and s["decided_by"] == boss and s["decided_at"] is not None
    assert (s["rejected_to_name"], s["rejected_to_stage"]) == ("Seriola zonata", None)
    assert s["decision_note"] == "a juvenile rudderfish" and s["approved_name"] is None
    assert (s["common_name"], s["scientific_name"], s["life_stage"]) == (
        "Juvenile Bluefish",
        "Pomatomus saltarix",
        "juvenile",
    )
    # the box is back in training, under the listed species
    [back] = await _training_rows(rdb, eid)
    assert back["corrected_name"] == "Seriola zonata" and back["life_stage"] == ""
    async with rdb.connection() as conn:
        assert await pending_species_events(conn) == {}
        [mine] = await list_mine(conn, user=as_user(roy))
    assert mine["status_text"] == "removed" and mine["rejected_to_name"] == "Seriola zonata"


async def test_reject_as_cant_identify(rdb, tmp_path, cat):
    roy, boss = await make_user(rdb, "roy"), await make_user(rdb, "boss", role="admin")
    sid = await insert_pending(rdb, user_id=roy, **JUVENILE_RUDDERFISH)
    eid = await _answered_with(rdb, tmp_path, cat, roy, sid, 0)
    assert (await _linked(rdb, sid))[0]["is_endorsement"] is True  # Seriola zonata, as guessed

    body = RejectIn(unidentifiable=True, note="too blurry to tell")
    out = await _decide(rdb, reject, boss, sid, body=body, catalogue=await _merged(rdb, cat))
    assert out == {"answers": 1}
    [row] = await _linked(rdb, sid)
    assert row["unidentifiable"] is True and row["not_a_fish"] is False
    assert row["corrected_name"] is None and row["corrected_species_id"] is None
    assert row["life_stage"] is None and row["is_endorsement"] is False
    assert row["confidence"] == "certain" and row["label_source"] == "suggested"
    s = await _row(rdb, sid)
    assert s["status"] == "rejected" and s["decision_note"] == "too blurry to tell"
    assert s["rejected_to_name"] is None and s["rejected_to_stage"] is None
    # the box is back in training, as can't identify
    [back] = await _training_rows(rdb, eid)
    assert back["unidentifiable"] == "True" and back["corrected_name"] == ""
    async with rdb.connection() as conn:
        assert await pending_species_events(conn) == {}
        assert (await resolve_all(conn, [eid]))[eid].outcome == ("unidentifiable",)


async def test_reject_as_a_staged_species_needs_its_stage(rdb, tmp_path, cat):
    """Review Focus 4: French Angelfish is listed only as adult and juvenile."""
    roy, boss = await make_user(rdb, "roy"), await make_user(rdb, "boss", role="admin")
    sid = await insert_pending(rdb, user_id=roy, common_name="Angel")
    await _answered_with(rdb, tmp_path, cat, roy, sid, 0)
    before = await _verdicts(rdb)
    merged = await _merged(rdb, cat)
    with pytest.raises(Invalid) as refused:
        await _decide(
            rdb, reject, boss, sid, body=RejectIn(as_label="Pomacanthus paru"), catalogue=merged
        )
    assert refused.value.message == "pick a species from the list"
    assert await _verdicts(rdb) == before
    assert (await _row(rdb, sid))["status"] == "pending"

    staged = RejectIn(as_label="Pomacanthus paru", as_life_stage="juvenile")
    assert await _decide(rdb, reject, boss, sid, body=staged, catalogue=merged) == {"answers": 1}
    [row] = await _linked(rdb, sid)
    assert (row["corrected_name"], row["life_stage"]) == ("Pomacanthus paru", "juvenile")
    assert (await _row(rdb, sid))["rejected_to_stage"] == "juvenile"


async def test_reject_says_what_it_is_or_that_it_cant_be_identified(rdb, tmp_path, cat):
    roy, boss = await make_user(rdb, "roy"), await make_user(rdb, "boss", role="admin")
    sid = await insert_pending(rdb, user_id=roy, common_name="Angel")
    await _answered_with(rdb, tmp_path, cat, roy, sid, 0)
    before = await _verdicts(rdb)
    for body in (
        RejectIn(),
        RejectIn(as_label="   "),
        RejectIn(as_label="Seriola zonata", unidentifiable=True),
    ):
        with pytest.raises(Invalid) as refused:
            await _decide(rdb, reject, boss, sid, body=body, catalogue=cat)
        assert refused.value.message == "say what it is, or that it can't be identified", body
    assert await _verdicts(rdb) == before
    assert (await _row(rdb, sid))["status"] == "pending"


async def test_cant_identify_clears_not_sure(rdb, tmp_path, cat):
    """Review Focus 5: a can't-identify answer is 'certain', and names no individual."""
    roy, boss = await make_user(rdb, "roy"), await make_user(rdb, "boss", role="admin")
    tang = await insert_pending(
        rdb,
        user_id=roy,
        common_name="Juvenile Blue Tang",
        scientific_name="Acanthurus coeruleus",
        life_stage="juvenile",
    )
    moray = await insert_pending(rdb, user_id=roy, common_name="Spotted Moray")
    rejected = await _answered_with(
        rdb, tmp_path, cat, roy, tang, 0, not_sure=True, celebrity_id="bobby"
    )
    detached = await _answered_with(rdb, tmp_path, cat, roy, moray, 1, not_sure=True)
    rows = {r["event_id"]: r for r in await _verdicts(rdb)}
    assert {r["confidence"] for r in rows.values()} == {"uncertain"}
    assert rows[rejected]["celebrity_id"] == "bobby"

    unknown = RejectIn(unidentifiable=True)
    await _decide(rdb, reject, boss, tang, body=unknown, catalogue=cat)
    await _decide(rdb, detach, boss, moray, event_id=detached)
    rows = {r["event_id"]: r for r in await _verdicts(rdb)}
    for eid in (rejected, detached):
        assert rows[eid]["unidentifiable"] is True and rows[eid]["confidence"] == "certain"
    assert rows[rejected]["celebrity_id"] is None


async def test_a_decision_keeps_a_named_individual_only_under_its_species(rdb, tmp_path, cat):
    """Final review I4: a new stage of Blue Tang may name Bobby, a Blue Tang. Moved to another
    species, the answer must not keep him, or the public gallery lists that box under him."""
    roy, boss = await make_user(rdb, "roy"), await make_user(rdb, "boss", role="admin")
    tang = {"scientific_name": "Acanthurus coeruleus"}
    kept = await insert_pending(
        rdb, user_id=roy, common_name="Juvenile Blue Tang", life_stage="juvenile", **tang
    )
    moved = await insert_pending(
        rdb, user_id=roy, common_name="Intermediate Blue Tang", life_stage="intermediate", **tang
    )
    boxes = {
        kept: await _answered_with(rdb, tmp_path, cat, roy, kept, 0, celebrity_id="bobby"),
        moved: await _answered_with(rdb, tmp_path, cat, roy, moved, 1, celebrity_id="bobby"),
    }
    rows = {r["event_id"]: r for r in await _verdicts(rdb)}
    assert {r["celebrity_id"] for r in rows.values()} == {"bobby"}

    as_rudderfish = RejectIn(as_label="Seriola zonata")
    await _decide(rdb, reject, boss, moved, body=as_rudderfish, catalogue=cat)
    same = ApproveIn(
        scientific_name="Acanthurus coeruleus",
        common_name="Juvenile Blue Tang",
        life_stage="juvenile",
    )
    await _decide(rdb, approve, boss, kept, body=same, catalogue=cat)
    rows = {r["event_id"]: r for r in await _verdicts(rdb)}
    assert rows[boxes[moved]]["corrected_name"] == "Seriola zonata"
    assert rows[boxes[moved]]["celebrity_id"] is None
    assert rows[boxes[kept]]["corrected_name"] == "Acanthurus coeruleus"
    assert rows[boxes[kept]]["celebrity_id"] == "bobby"


async def test_not_this_species_unlinks_one_box(rdb, tmp_path, cat):
    roy, boss = await make_user(rdb, "roy"), await make_user(rdb, "boss", role="admin")
    sid = await insert_pending(rdb, user_id=roy, common_name="Spotted Moray")
    kept, wrong = [await _answered_with(rdb, tmp_path, cat, roy, sid, n) for n in (0, 1)]
    assert await _decide(rdb, detach, boss, sid, event_id=wrong) == {"answers": 1}
    rows = {r["event_id"]: r for r in await _verdicts(rdb)}
    w = rows[wrong]
    assert w["species_suggestion_id"] is None and w["unidentifiable"] is True
    assert w["corrected_name"] is None and w["life_stage"] is None
    assert w["label_source"] == "suggested"  # how it was answered
    assert rows[kept]["species_suggestion_id"] == sid
    assert (await _row(rdb, sid))["status"] == "pending"
    async with rdb.connection() as conn:
        assert await pending_species_events(conn) == {kept: "Spotted Moray"}
    [back] = await _training_rows(rdb, wrong)  # no longer waiting
    assert back["unidentifiable"] == "True"
    with pytest.raises(NotFound):  # nothing on that box uses the suggestion any more
        await _decide(rdb, detach, boss, sid, event_id=wrong)

    body = ApproveIn(scientific_name="Gymnothorax moringa", common_name="Spotted Moray")
    assert await _decide(rdb, approve, boss, sid, body=body, catalogue=cat) == {"answers": 1}
    rows = {r["event_id"]: r for r in await _verdicts(rdb)}
    assert rows[kept]["corrected_name"] == "Gymnothorax moringa"
    assert rows[wrong]["corrected_name"] is None and rows[wrong]["unidentifiable"] is True


async def test_a_decided_suggestion_cannot_be_decided_again(rdb, tmp_path, cat):
    roy, boss = await make_user(rdb, "roy"), await make_user(rdb, "boss", role="admin")
    added = await insert_pending(rdb, user_id=roy, common_name="Spotted Moray")
    dropped = await insert_pending(rdb, user_id=roy, common_name="Goldentail Moray")
    boxes = {added: await _answered_with(rdb, tmp_path, cat, roy, added, 0)}
    boxes[dropped] = await _answered_with(rdb, tmp_path, cat, roy, dropped, 1)
    moray = ApproveIn(scientific_name="Gymnothorax moringa", common_name="Spotted Moray")
    await _decide(rdb, approve, boss, added, body=moray, catalogue=cat)
    await _decide(rdb, reject, boss, dropped, body=RejectIn(unidentifiable=True), catalogue=cat)
    merged = await _merged(rdb, cat)
    verdicts = await _verdicts(rdb)
    decided = {sid: await _row(rdb, sid) for sid in boxes}

    for sid, eid in boxes.items():
        for fn, kw in (
            (approve, {"body": moray, "catalogue": merged}),
            (approve, {"body": moray, "catalogue": cat}),
            (reject, {"body": RejectIn(as_label="Seriola zonata"), "catalogue": merged}),
            (reject, {"body": RejectIn(unidentifiable=True), "catalogue": merged}),
            (detach, {"event_id": eid}),
        ):
            with pytest.raises(Conflict) as refused:
                await _decide(rdb, fn, boss, sid, **kw)
            assert refused.value.message == "this suggestion was already decided", fn
    assert await _verdicts(rdb) == verdicts
    assert {sid: await _row(rdb, sid) for sid in boxes} == decided


async def test_admin_routes_over_http(rdb, rclient, tmp_path):
    roy = await make_user(rdb, "roy")
    await make_user(rdb, "boss", role="admin")
    url = "/api/review/species-suggestions"
    login(rclient, "roy")
    sid = await insert_pending(rdb, user_id=roy, common_name="Spotted Moray")
    async with rdb.connection() as conn:
        await conn.execute(
            "UPDATE species_suggestions SET note = 'under the ladder' WHERE id = %s", (sid,)
        )
    _f, eid, _item = await _box(rdb, tmp_path)
    r = rclient.post("/api/review/verdicts", json=_suggested(sid, event_id=eid), headers=CSRF)
    assert r.status_code == 200, r.text

    approve_body = {"scientific_name": "Gymnothorax moringa", "common_name": "Spotted Moray"}
    calls = (
        ("GET", f"{url}?status=pending", None),
        ("GET", f"{url}/{sid}/boxes", None),
        ("POST", f"{url}/{sid}/approve", approve_body),
        ("POST", f"{url}/{sid}/reject", {"unidentifiable": True}),
        ("POST", f"{url}/{sid}/detach", {"event_id": eid}),
    )
    for method, path, body in calls:  # roy reviews; deciding is an admin's
        r = rclient.request(method, path, json=body, headers=CSRF)
        assert r.status_code == 403, path

    login(rclient, "boss")
    for _method, path, body in calls[2:]:
        assert rclient.post(path, json=body).status_code == 403, path  # no CSRF header
    assert (await _row(rdb, sid))["status"] == "pending"

    listed = rclient.get(url, params={"status": "pending"}).json()
    assert listed["pending"] == 1
    [one] = listed["suggestions"]
    assert one["id"] == sid and one["status"] == "pending" and one["answers"] == 1
    assert one["common_name"] == "Spotted Moray" and one["note"] == "under the ladder"
    assert one["suggested_by_name"] == "Roy" and one["created_at"]
    assert rclient.get(url).json() == listed  # pending is the default
    assert rclient.get(url, params={"status": "everything"}).status_code == 422

    boxes = rclient.get(f"{url}/{sid}/boxes", params={"page": 0}).json()
    zoom = f"/api/review/boxes/{eid}/zoom.jpg"
    assert boxes == {"total": 1, "boxes": [{"event_id": eid, "zoom_url": zoom, "by": "Roy"}]}
    assert rclient.get(zoom).headers["content-type"] == "image/jpeg"
    assert rclient.get(f"{url}/{sid}/boxes", params={"page": 1}).json() == {
        "total": 1,
        "boxes": [],
    }

    refused = rclient.post(f"{url}/{sid}/reject", json={}, headers=CSRF)
    assert refused.status_code == 400
    assert refused.json()["error"] == "say what it is, or that it can't be identified"
    missing = rclient.post(f"{url}/{sid}/detach", json={"event_id": eid + 1000}, headers=CSRF)
    assert missing.status_code == 404

    r = rclient.post(f"{url}/{sid}/approve", json=approve_body, headers=CSRF)
    assert r.status_code == 200 and r.json() == {"answers": 1}
    again = rclient.post(f"{url}/{sid}/approve", json=approve_body, headers=CSRF)
    assert again.status_code == 409
    assert again.json()["error"] == "this suggestion was already decided"
    assert rclient.get(url).json() == {"suggestions": [], "pending": 0}
    decided = rclient.get(url, params={"status": "decided"}).json()
    assert decided["pending"] == 0
    [done] = decided["suggestions"]
    assert done["id"] == sid and done["status"] == "approved" and done["decided_at"]
    assert done["approved_name"] == "Gymnothorax moringa" and done["answers"] == 1

    login(rclient, "roy")
    [mine] = rclient.get(f"{url}/mine").json()["suggestions"]
    assert mine["status_text"] == "added" and mine["approved_common"] == "Spotted Moray"
    species = rclient.get("/api/review/species").json()["species"]
    # on everyone's list now, without the suffix
    assert [e["common"] for e in species if e["name"] == "Gymnothorax moringa"] == ["Spotted Moray"]


# ---- what the review UI shows ---------------------------------------------------------------

WAITING = " (new, waiting for approval)"


async def test_the_frame_names_a_pending_species(rdb, tmp_path, cat):
    w = await _pending_and_control(rdb, tmp_path, cat)
    user = as_user(w["roy"])

    async def boxes(catalogue=cat):
        async with rdb.connection() as conn:
            state = await frame_state(
                conn,
                saved_frame_id=w["frames"][w["pending"]]["saved_frame_id"],
                user=user,
                catalogue=catalogue,
            )
        return {b["event_id"]: b for b in state["boxes"]}

    shown = await boxes()
    assert shown[w["pending"]]["common"] == "Juvenile Banded Rudderfish" + WAITING
    assert shown[w["pending"]]["pending_species"] is True
    assert shown[w["pending"]]["state"] == "answered"
    await _approve(rdb, w["sid"], "Seriola zonata", "juvenile", "Juvenile Banded Rudderfish")
    # approved: the box shows the species' own plain name (the stage is not in it)
    shown = await boxes(await _merged(rdb, cat))
    assert shown[w["pending"]]["common"] == "Banded Rudderfish"
    assert shown[w["pending"]]["pending_species"] is False
    # a box answered from the list never carries the flag
    async with rdb.connection() as conn:
        other = await frame_state(
            conn,
            saved_frame_id=w["frames"][w["control"]]["saved_frame_id"],
            user=user,
            catalogue=cat,
        )
    assert [b["pending_species"] for b in other["boxes"]] == [False]


async def test_browse_names_a_pending_species(rdb, tmp_path, cat):
    w = await _pending_and_control(rdb, tmp_path, cat)

    async def commons(catalogue=cat):
        async with rdb.connection() as conn:
            result = await browse(
                conn,
                catalogue=catalogue,
                user=as_user(w["roy"]),
                filters=BrowseFilters(status="any"),
                page=0,
            )
        return {c["event_id"]: c["common"] for c in result["cells"]}

    before = await commons()
    assert before[w["pending"]] == "Juvenile Banded Rudderfish" + WAITING
    assert WAITING not in before[w["control"]]
    await _approve(rdb, w["sid"], "Seriola zonata", "juvenile", "Juvenile Banded Rudderfish")
    assert (await commons(await _merged(rdb, cat)))[w["pending"]] == "Banded Rudderfish"


async def test_browse_never_calls_a_box_waiting_on_a_new_species_verified(rdb, tmp_path, cat):
    """Final review I1: the grid said "✓ verified" on such a box, and the Verified filter listed
    it, under counts that leave it out. It has a status of its own until the decision."""
    w = await _pending_and_control(rdb, tmp_path, cat)

    async def statuses(status, catalogue=cat, page_size=48):
        async with rdb.connection() as conn:
            result = await browse(
                conn,
                catalogue=catalogue,
                user=as_user(w["roy"]),
                filters=BrowseFilters(status=status),
                page=0,
                page_size=page_size,
            )
        return {c["event_id"]: c["status"] for c in result["cells"]}, result["total"]

    shown, total = await statuses("any")
    assert shown == {w["pending"]: "awaiting_species", w["control"]: "single"} and total == 2
    # the filter runs before paging: the waiting box is out of every page and of the total
    assert await statuses("verified") == ({w["control"]: "single"}, 1)
    assert await statuses("verified", page_size=1) == ({w["control"]: "single"}, 1)
    for status in ("agreed", "adjudicated", "disputed", "unidentifiable", "no_animal"):
        assert await statuses(status) == ({}, 0), status
    await _approve(rdb, w["sid"], "Seriola zonata", "juvenile", "Juvenile Banded Rudderfish")
    approved = await statuses("verified", await _merged(rdb, cat))
    assert approved == ({w["pending"]: "single", w["control"]: "single"}, 2)


async def test_browse_names_a_pending_species_that_is_not_listed_yet(rdb, tmp_path, cat):
    """Browser walk 2026-11-21: a species new to the list has no listed common name, so
    the cell showed its bare provisional name, "Pseudupeneus maculatus", unmarked."""
    roy = await make_user(rdb, "roy")
    sid = await insert_pending(
        rdb,
        user_id=roy,
        common_name="Spotted Goatfish",
        scientific_name="Pseudupeneus maculatus",
    )
    eid = await _answered_with(rdb, tmp_path, cat, roy, sid, 0)
    async with rdb.connection() as conn:
        result = await browse(
            conn, catalogue=cat, user=as_user(roy), filters=BrowseFilters(status="any"), page=0
        )
    [cell] = result["cells"]
    assert cell["event_id"] == eid
    assert cell["common"] == "Spotted Goatfish" + WAITING


async def test_a_box_changed_away_from_its_pending_species_shows_the_new_answer(rdb, tmp_path, cat):
    w = await _pending_and_control(rdb, tmp_path, cat)
    await _answer(
        rdb,
        cat,
        w["roy"],
        event_id=w["pending"],
        outcome="species",
        label="Acanthurus coeruleus",
        label_source="search",
    )
    async with rdb.connection() as conn:
        state = await frame_state(
            conn,
            saved_frame_id=w["frames"][w["pending"]]["saved_frame_id"],
            user=as_user(w["roy"]),
            catalogue=cat,
        )
        [box] = state["boxes"]
        result = await browse(
            conn,
            catalogue=cat,
            user=as_user(w["roy"]),
            filters=BrowseFilters(status="any"),
            page=0,
        )
    assert box["label"] == "Acanthurus coeruleus"
    assert box["common"] == "Blue Tang" and box["pending_species"] is False
    cells = {c["event_id"]: c for c in result["cells"]}
    assert cells[w["pending"]]["common"] == "Blue Tang"
