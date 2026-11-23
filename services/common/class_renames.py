"""Classifier classes renamed to the names reviewers label them with.

The live classifier (classifier_v2, dgx-2026-10-24) names two classes the way the Species Log once
spelled them, while the review tool stores every answer under the WoRMS accepted name. Dan chose to
give the model the reviewers' names (2026-11-22, docs/superpowers/specs/2026-11-22-class-renames-design.md):
the bundle dgx-2026-10-24-r1 carries them, migration 0008 rewrote the stored detections, and
whatever builds a training set renames the two folders through this table. Keys and values are
spelled the way the worker writes them (classes.json's underscores become spaces).
"""

from __future__ import annotations

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
