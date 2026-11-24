"""One box's class scores as the species list the worker stores. Torch-free, so CI tests it.

A classifier trained on the verified dataset has a class `no_animal` (the reviewers' "no animal
here"). A box whose best answer is `no_animal` is not a fish, and the worker drops it: no
detection, no sighting, no saved frame, no box on the live picture (2026-11-22 spec "Handed to the
classifier project"). A reviewer who sees a fish the worker dropped can still draw its box on a
saved frame. Otherwise `no_animal` never appears among a fish's guesses.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
from common.class_renames import NO_ANIMAL

from .pipeline_types import Prediction


def no_animal_row(raw_classes: Sequence[str]) -> int | None:
    """The head row of `no_animal` in classes.json as written (before names gain spaces), or None
    for a model trained without it."""
    return raw_classes.index(NO_ANIMAL) if NO_ANIMAL in raw_classes else None


def ranked(
    probs: np.ndarray,
    names: Sequence[str],
    k: int,
    min_accept: float,
    no_animal: int | None,
) -> list[Prediction] | None:
    """The box's best `k` species scoring at least `min_accept`, best first; None when the box is
    not a fish (its best answer is `no_animal`, at least `min_accept`). A `no_animal` below the
    threshold leaves the box unnamed, like a species below it."""
    best = int(np.argmax(probs))
    if no_animal is not None and best == no_animal and float(probs[best]) >= min_accept:
        return None
    order = [int(i) for i in np.argsort(-probs, kind="stable") if int(i) != no_animal][:k]
    return [
        Prediction(name=names[i], species_id=names[i], accuracy=float(probs[i]))
        for i in order
        if float(probs[i]) >= min_accept
    ]
