"""What a volunteer can pick as a label, and how raw names map to it.

Built from files that are only read here:

* the Species Log manifest (`species_manifest.json`, rebuilt daily by
  the sheet poller): common names and life stages;
* the classifier's `classes.json`: the training classes, some spelled
  the way the sheet once spelled them;
* `taxonomy.csv` (scripts/enrich_taxonomy_from_worms.py): every raw
  name above mapped to its WoRMS accepted name;
* `inat_manifest.json` and `celebrities.json`: reference photos with
  their attribution, and named individuals.

Labels are stored under the accepted name, so a misspelled entry and a
correctly spelled one are the same species. `class_dir` remembers the
training folder that species lives in, because folders keep the
original spelling. CatalogueStore rebuilds the catalogue whenever one of
the files changes, so a sheet edit reaches the page without a restart.
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("wahoobay.dashboard.review_catalogue")

_NO_STAGE = {"", "unknown", "none", "n/a"}


def normalize_raw(raw: str) -> str:
    """'Pomatomus_saltarix ' -> 'Pomatomus saltarix'."""
    return " ".join(raw.replace("_", " ").split())


def slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")


@dataclass(frozen=True)
class CataloguePaths:
    species_manifest: Path
    classes_json: Path
    taxonomy_csv: Path
    inat_manifest: Path
    celebrities_json: Path
    photo_root: Path  # iNat `local_path` values are relative to this

    def watched(self) -> tuple[Path, ...]:
        return (
            self.species_manifest,
            self.classes_json,
            self.taxonomy_csv,
            self.inat_manifest,
            self.celebrities_json,
        )


@dataclass
class CatalogueEntry:
    name: str
    life_stage: str | None
    common_name: str | None = None
    aphia_id: int | None = None
    rank: str | None = None
    family: str | None = None
    order: str | None = None
    resolved: bool = False
    raw_names: set[str] = field(default_factory=set)
    class_dir: str | None = None
    in_species_log: bool = False
    in_classifier: bool = False
    photo_path: Path | None = None
    photo_credit: str | None = None
    celebrity_ids: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.name}|{self.life_stage or ''}"


class Catalogue:
    def __init__(
        self, entries, accepted_by_raw: dict[str, str], celebrities_by_id: dict[str, dict]
    ) -> None:
        self.entries = sorted(entries, key=lambda e: ((e.common_name or e.name).lower(), e.key))
        self._accepted = dict(accepted_by_raw)
        self._by_key = {e.key: e for e in self.entries}
        self._by_name: dict[str, list[CatalogueEntry]] = {}
        for e in self.entries:
            self._by_name.setdefault(e.name, []).append(e)
        self.celebrities_by_id = dict(celebrities_by_id)
        self._photos = {slug(e.name): e for e in self.entries if e.photo_path}

    def accepted(self, raw: str | None) -> str | None:
        if raw is None:
            return None
        n = normalize_raw(raw)
        return self._accepted.get(n, n)

    def entry(self, name: str, life_stage: str | None) -> CatalogueEntry | None:
        return self._by_key.get(f"{name}|{life_stage or ''}")

    def has_name(self, name: str) -> bool:
        return name in self._by_name

    def stages(self, name: str) -> list[str | None]:
        """The life stages recorded for `name`; None is "no stage"."""
        return [e.life_stage for e in self._by_name.get(name, [])]

    def names(self) -> list[str]:
        return sorted(self._by_name)

    def taxon(self, name: str | None) -> CatalogueEntry | None:
        """An entry for `name` to read its taxonomy from (its life stages share it),
        one WoRMS resolved when there is one; None for a name the catalogue lacks."""
        found = self._by_name.get(name or "", [])
        return next((e for e in found if e.resolved), found[0] if found else None)

    def class_dir(self, name: str) -> str | None:
        return next((e.class_dir for e in self._by_name.get(name, []) if e.class_dir), None)

    def common_name(self, name: str | None) -> str | None:
        return next(
            (e.common_name for e in self._by_name.get(name or "", []) if e.common_name), None
        )

    def celebrity_ids(self, name: str | None) -> set[str]:
        return {c for e in self._by_name.get(name or "", []) for c in e.celebrity_ids}

    def photo(self, slug_: str) -> tuple[Path, str | None] | None:
        e = self._photos.get(slug_)
        return (e.photo_path, e.photo_credit) if e else None

    def to_json(self) -> list[dict]:
        out = []
        for e in self.entries:
            out.append(
                {
                    "key": e.key,
                    "name": e.name,
                    "life_stage": e.life_stage,
                    "common": e.common_name,
                    "aphia_id": e.aphia_id,
                    "rank": e.rank,
                    "family": e.family,
                    "resolved": e.resolved,
                    "in_species_log": e.in_species_log,
                    "in_classifier": e.in_classifier,
                    "photo_url": (
                        f"/api/review/species/{slug(e.name)}/photo.jpg" if e.photo_path else None
                    ),
                    "photo_credit": e.photo_credit,
                    "celebrities": [
                        {
                            "id": c,
                            "nickname": (self.celebrities_by_id.get(c) or {}).get("nickname") or c,
                        }
                        for c in e.celebrity_ids
                    ],
                }
            )
        return out


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        log.warning("catalogue: %s not found", path)
        return default


def _read_taxonomy(path: Path) -> dict[str, dict]:
    if not path.exists():
        log.warning("catalogue: %s not found; names will not be reconciled", path)
        return {}
    with path.open(newline="") as f:
        return {normalize_raw(r["raw_name"]): r for r in csv.DictReader(f) if r.get("raw_name")}


def load_catalogue(paths: CataloguePaths) -> Catalogue:
    taxonomy = _read_taxonomy(paths.taxonomy_csv)
    accepted_by_raw = {
        raw: row["accepted_name"] for raw, row in taxonomy.items() if row.get("accepted_name")
    }
    entries: dict[str, CatalogueEntry] = {}

    def entry_for(raw: str, stage: str | None) -> CatalogueEntry:
        raw_n = normalize_raw(raw)
        name = accepted_by_raw.get(raw_n, raw_n)
        key = f"{name}|{stage or ''}"
        e = entries.get(key)
        if e is None:
            e = entries[key] = CatalogueEntry(name=name, life_stage=stage)
            row = taxonomy.get(raw_n)
            if row and row.get("accepted_name"):
                e.resolved = True
                aphia = (row.get("aphia_id") or "").strip()
                e.aphia_id = int(aphia) if aphia.isdigit() else None
                e.rank = row.get("rank") or None
                e.family = row.get("family") or None
                e.order = row.get("order") or None
        e.raw_names.add(raw_n)
        return e

    for rec in _read_json(paths.species_manifest, []):
        raw = rec.get("binomial")
        if not raw or not normalize_raw(raw):
            continue
        stage = (rec.get("life_stage") or "").strip().lower()
        e = entry_for(raw, None if stage in _NO_STAGE else stage)
        e.in_species_log = True
        e.common_name = e.common_name or rec.get("common_name") or None
        e.family = e.family or rec.get("family") or None

    for cls in _read_json(paths.classes_json, []):
        name = accepted_by_raw.get(normalize_raw(cls), normalize_raw(cls))
        targets = [e for e in entries.values() if e.name == name] or [entry_for(cls, None)]
        for e in targets:
            e.in_classifier = True
            e.class_dir = cls
            e.raw_names.add(normalize_raw(cls))

    root = paths.photo_root.resolve()
    photos: dict[str, dict] = {}
    for p in sorted(
        _read_json(paths.inat_manifest, []),
        key=lambda r: (str(r.get("binomial")), r.get("photo_id") or 0),
    ):
        raw, local = p.get("binomial"), p.get("local_path")
        if not raw or not local:
            continue
        name = accepted_by_raw.get(normalize_raw(raw), normalize_raw(raw))
        if name in photos:
            continue
        path = (root / local).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            continue
        photos[name] = {
            "path": path,
            "credit": p.get("attribution"),
            "common": p.get("common_name"),
        }
    for e in entries.values():
        ph = photos.get(e.name)
        if ph:
            e.photo_path, e.photo_credit = ph["path"], ph["credit"]
            e.common_name = e.common_name or ph["common"]

    celebs = (_read_json(paths.celebrities_json, {}) or {}).get("celebrities") or []
    celebrities_by_id = {c["id"]: c for c in celebs if c.get("id")}
    for c in celebs:
        if not c.get("id") or not c.get("binomial"):
            continue
        name = accepted_by_raw.get(normalize_raw(c["binomial"]), normalize_raw(c["binomial"]))
        for e in entries.values():
            if e.name == name and c["id"] not in e.celebrity_ids:
                e.celebrity_ids.append(c["id"])

    return Catalogue(entries.values(), accepted_by_raw, celebrities_by_id)


class CatalogueStore:
    """The current catalogue, rebuilt when any of its source files changes."""

    def __init__(self, paths: CataloguePaths) -> None:
        self.paths = paths
        self._catalogue: Catalogue | None = None
        self._stamp: tuple | None = None

    def _current_stamp(self) -> tuple:
        out = []
        for p in self.paths.watched():
            try:
                out.append(p.stat().st_mtime_ns)
            except FileNotFoundError:
                out.append(None)
        return tuple(out)

    def get(self) -> Catalogue:
        stamp = self._current_stamp()
        if self._catalogue is None or stamp != self._stamp:
            try:
                self._catalogue = load_catalogue(self.paths)
            except Exception:
                log.exception("catalogue: rebuild failed; keeping the previous one")
                if self._catalogue is None:
                    self._catalogue = Catalogue([], {}, {})
            self._stamp = stamp
        return self._catalogue

    async def aget(self) -> Catalogue:
        """get() for async code (follow-up C6): its stat calls, and any
        rebuild's file reads, run on a worker thread, not the event loop."""
        return await asyncio.to_thread(self.get)

    def current(self) -> Catalogue:
        """The catalogue as last built, without looking at the files: for
        sync code on the event loop (ReviewPool.species_order). The pool's
        runs and every review route refresh it through aget(). Builds it
        once if nothing has yet."""
        return self._catalogue if self._catalogue is not None else self.get()
