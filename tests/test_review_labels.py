"""The one definition of "verified" (spec §8, Resolution) and the
agreement statistic (spec §8, Agreement)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.review_labels import (
    Verdict,
    agreement_summary,
    krippendorff_alpha_nominal,
    outcome_of,
    percent_agreement,
    resolve,
    resolve_all,
)
from review_fixtures import add_item, add_verdict, make_user, seed_frame

T = datetime(2026, 6, 1, tzinfo=UTC)


def V(
    id,
    user=1,
    t=0,
    *,
    name="Seriola zonata",
    no_animal=False,
    unid=False,
    stage=None,
    bbox=None,
    conf="certain",
    source=None,
    adj=False,
    item_pass=None,
    event=7,
):
    return Verdict(
        id=id,
        event_id=event,
        user_id=user,
        created_at=T + timedelta(seconds=t),
        corrected_name=None if (no_animal or unid) else name,
        not_a_fish=no_animal,
        unidentifiable=unid,
        life_stage=stage,
        corrected_bbox=bbox,
        confidence=conf,
        label_source=source,
        is_adjudication=adj,
        item_pass=item_pass,
    )


def test_outcomes():
    assert outcome_of(V(1)) == ("species", "Seriola zonata")
    assert outcome_of(V(1, no_animal=True)) == ("no_animal",)
    assert outcome_of(V(1, unid=True)) == ("unidentifiable",)


def test_a_single_verdict_is_verified():
    r = resolve([V(1)])
    assert r.status == "single" and r.species == "Seriola zonata" and r.is_verified


def test_the_latest_first_pass_verdict_wins_after_a_reopen():
    r = resolve([V(1, t=0, name="Aa bb"), V(2, t=60, name="Cc dd")])
    assert r.species == "Cc dd" and r.deciding == (2,)


def test_legacy_rows_without_a_user_are_ignored():
    assert resolve([V(1, user=None)]) is None
    r = resolve([V(1, user=None, no_animal=True), V(2, user=5)])
    assert r.outcome == ("species", "Seriola zonata")


def test_agreement_takes_the_less_certain_confidence():
    r = resolve([V(1, user=1), V(2, user=2, conf="uncertain", item_pass=2)])
    assert r.status == "agreed" and r.confidence == "uncertain" and r.user_ids == (1, 2)


def test_life_stage_and_box_differences_are_not_disagreements():
    r = resolve(
        [V(1, stage="adult", bbox=(1, 2, 30, 40)), V(2, user=2, stage="juvenile", item_pass=2)]
    )
    assert r.status == "agreed" and r.bbox == (1, 2, 30, 40)


def test_disagreement_is_disputed_and_not_verified():
    r = resolve([V(1), V(2, user=2, item_pass=2, no_animal=True)])
    assert r.status == "disputed" and not r.is_verified
    assert r.outcome == ("species", "Seriola zonata")


def test_adjudication_overrides_everything():
    r = resolve(
        [V(1), V(2, user=2, item_pass=2, no_animal=True), V(3, user=9, t=100, adj=True, unid=True)]
    )
    assert r.status == "adjudicated" and r.outcome == ("unidentifiable",)
    assert r.deciding == (3,) and r.is_verified


def test_a_spot_check_whose_first_verdict_was_legacy_counts_as_single():
    r = resolve([V(1, user=None), V(2, user=2, item_pass=2)])
    assert r.status == "single"


def test_the_box_falls_back_to_an_earlier_correction():
    # Reopened and re-answered without moving the box: the earlier fix stands.
    r = resolve([V(1, t=0, bbox=(5, 5, 50, 50)), V(2, t=10)])
    assert r.bbox == (5, 5, 50, 50)


def test_krippendorff_alpha_known_value():
    pairs = [("A", "A"), ("A", "A"), ("B", "B"), ("A", "B")]
    assert krippendorff_alpha_nominal(pairs) == pytest.approx(8 / 15)
    assert percent_agreement(pairs) == 0.75


def test_alpha_edge_cases():
    assert krippendorff_alpha_nominal([("A", "A"), ("B", "B")]) == 1.0
    assert krippendorff_alpha_nominal([("A", "B"), ("B", "A")]) == pytest.approx(-0.5)
    assert krippendorff_alpha_nominal([("A", "A")]) is None  # no variation at all
    assert krippendorff_alpha_nominal([]) is None
    assert percent_agreement([]) is None


def test_agreement_summary_splits_by_how_the_first_label_was_chosen():
    resolved = {
        1: resolve([V(1, event=1, source="model_guess"), V(2, event=1, user=2, item_pass=2)]),
        2: resolve(
            [
                V(3, event=2, source="model_guess"),
                V(4, event=2, user=2, item_pass=2, no_animal=True),
            ]
        ),
        3: resolve(
            [
                V(5, event=3, source="search", name="Xx yy"),
                V(6, event=3, user=2, item_pass=2, name="Xx yy"),
            ]
        ),
        4: resolve([V(7, event=4)]),  # never spot-checked
    }
    s = agreement_summary(resolved)
    assert s["overall"]["pairs"] == 3
    assert s["overall"]["percent"] == pytest.approx(2 / 3)
    assert s["by_label_source"]["model_guess"]["pairs"] == 2
    assert s["by_label_source"]["search"]["percent"] == 1.0
    assert s["min_pairs"] == 30


@pytest.mark.anyio
async def test_load_verdicts_reads_the_pass_from_the_review_item(rdb, tmp_path):
    frame = await seed_frame(
        rdb, tmp_path, boxes=[{"bbox": [10, 10, 100, 100], "species": "Seriola zonata"}]
    )
    one, two = await make_user(rdb, "one"), await make_user(rdb, "two")
    first_item = await add_item(rdb, frame, status="done")
    first = await add_verdict(
        rdb, frame["event_ids"][0], one, name="Seriola zonata", item_id=first_item
    )
    second_item = await add_item(
        rdb, frame, pass_=2, status="done", exclude_user_id=one, first_correction_id=first
    )
    await add_verdict(rdb, frame["event_ids"][0], two, name="Seriola zonata", item_id=second_item)
    async with rdb.connection() as conn:
        resolved = await resolve_all(conn)
    r = resolved[frame["event_ids"][0]]
    assert r.status == "agreed" and r.user_ids == (one, two)


def test_the_box_is_the_latest_one_the_deciding_verdicts_drew():
    r = resolve(
        [V(1, t=0, bbox=(1, 1, 10, 10)), V(2, user=2, t=5, item_pass=2, bbox=(2, 2, 20, 20))]
    )
    assert r.status == "agreed" and r.bbox == (2, 2, 20, 20)


def test_without_a_deciding_box_the_latest_box_anyone_drew_is_used():
    r = resolve([V(1, user=1, t=0, bbox=(5, 5, 50, 50)), V(2, user=2, t=10)])
    assert r.deciding == (2,) and r.bbox == (5, 5, 50, 50)


def test_a_box_fix_outlives_a_later_change_of_label():
    r = resolve([V(1, t=0, bbox=(5, 5, 50, 50)), V(2, t=10, no_animal=True)])
    assert r.outcome == ("no_animal",) and r.bbox == (5, 5, 50, 50)


def test_an_adjudication_box_wins():
    r = resolve(
        [
            V(1, t=0, bbox=(1, 1, 10, 10)),
            V(2, user=2, t=5, item_pass=2, unid=True, bbox=(2, 2, 20, 20)),
            V(3, user=9, t=9, adj=True, bbox=(3, 3, 30, 30)),
        ]
    )
    assert r.status == "adjudicated" and r.bbox == (3, 3, 30, 30)


def test_an_adjudication_without_a_box_falls_back_to_the_latest_box_drawn():
    r = resolve(
        [
            V(1, t=0, bbox=(1, 1, 10, 10)),
            V(2, user=2, t=5, item_pass=2, unid=True, bbox=(2, 2, 20, 20)),
            V(3, user=9, t=9, adj=True),
        ]
    )
    assert r.bbox == (2, 2, 20, 20)


def test_with_no_box_drawn_the_models_box_applies():
    assert resolve([V(1), V(2, user=2, t=5, item_pass=2)]).bbox is None
