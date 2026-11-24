"""How one box's class scores become its species list (services/worker/app/class_scores.py).

A classifier trained on the verified dataset has a class `no_animal` (the reviewers' "no animal
here": holes in the SeaHIVEs, rock, shadows). A box whose best answer is `no_animal` is not a fish:
the worker drops it, so it never becomes a detection, a sighting, a saved frame or a box on the live
picture (2026-11-22 spec "Handed to the classifier project"; chosen 2026-11-23). Otherwise
`no_animal` never appears among a fish's guesses."""

from __future__ import annotations

import numpy as np
from common.class_renames import NO_ANIMAL, class_names

from services.worker.app.class_scores import no_animal_row, ranked

RAW = ["Abudefduf_saxatilis", "Haemulon_chrysargyreum", "Kyphosus", NO_ANIMAL]
NAMES = class_names(RAW)  # as the worker holds them: spaces, current names
ROW = no_animal_row(RAW)


def _names(preds):
    return [p.name for p in preds]


def test_the_no_animal_row_is_found_by_its_raw_name_and_absent_from_older_models():
    assert ROW == 3
    assert no_animal_row(RAW[:3]) is None


def test_a_box_the_classifier_calls_no_animal_is_dropped():
    assert ranked(np.array([0.1, 0.2, 0.1, 0.6]), NAMES, 3, 0.0, ROW) is None


def test_no_animal_is_never_among_a_fishs_guesses():
    preds = ranked(np.array([0.1, 0.5, 0.05, 0.35]), NAMES, 3, 0.0, ROW)
    assert _names(preds) == ["Brachygenys chrysargyrea", "Abudefduf saxatilis", "Kyphosus"]
    assert preds[0].species_id == "Brachygenys chrysargyrea" and preds[0].accuracy == 0.5


def test_a_no_animal_below_the_accept_threshold_leaves_the_box_unnamed_not_dropped():
    # Like a species below MIN_ACCEPT_ACCURACY: the box stays, with no name.
    assert ranked(np.array([0.2, 0.2, 0.2, 0.4]), NAMES, 3, 0.5, ROW) == []


def test_a_model_without_no_animal_ranks_as_before():
    probs = np.array([0.15, 0.6, 0.25])
    preds = ranked(probs, NAMES[:3], 2, 0.2, None)
    assert _names(preds) == ["Brachygenys chrysargyrea", "Kyphosus"]
    assert [p.accuracy for p in preds] == [0.6, 0.25]
    assert ranked(probs, NAMES[:3], 5, 0.0, None) == ranked(probs, NAMES[:3], 3, 0.0, None)
