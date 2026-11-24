"""Classifier classes renamed to the names reviewers label them with.

The live classifier (classifier_v2, dgx-2026-10-24) names two classes the way the Species Log once
spelled them, while the review tool stores every answer under the WoRMS accepted name. Dan chose to
give the model the reviewers' names (2026-11-22, docs/superpowers/specs/2026-11-22-class-renames-design.md):
the bundle dgx-2026-10-24-r1 carries them, migration 0008 rewrote the stored detections, and
whatever builds a training set renames the two folders through this table. Keys and values are
spelled the way the worker writes them (classes.json's underscores become spaces).
"""

from __future__ import annotations

# Not a species: the class a classifier trained on the verified dataset learns from the reviewers'
# "no animal here" answers (holes in the SeaHIVEs, rock). classes.json spells it this way (lower
# case, so it cannot collide with a binomial); the worker drops a box it wins and the review
# catalogue never offers it as a fish.
NO_ANIMAL = "no_animal"

CLASS_RENAMES: dict[str, str] = {
    "Haemulon chrysargyreum": "Brachygenys chrysargyrea",
    "Kyphosus sectatrixbigibbus": "Kyphosus",
}


def renamed(name: str | None) -> str | None:
    """The current name for a renamed class; any other name, or None, as given."""
    if name is None:
        return None
    return CLASS_RENAMES.get(name, name)


def folder(name: str) -> str:
    """A class name as classes.json and the training folders spell it: spaces become underscores."""
    return name.replace(" ", "_")


def class_names(entries: list[str]) -> list[str]:
    """classes.json's entries as detections store them: underscores become spaces, and a renamed
    class takes its current name whichever bundle is on disk (the GPU host keeps its model volume
    across deploys, so the worker may still hold the old list)."""
    return [renamed(e.replace("_", " ")) for e in entries]
