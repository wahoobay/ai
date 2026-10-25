"""The one definition of "verified" (spec §8).

Every place that needs to know what a box's label is (the page, the
progress counts, the browse view, and phase 3's dataset builder) calls
resolve() here, so there is exactly one rule:

1. An administrator's adjudication, if any; the latest wins.
2. Otherwise the latest first-pass verdict, compared with the latest
   spot-check verdict if the box was spot-checked: the same outcome is
   "agreed", a different one is "disputed" and held out of the dataset.
3. Otherwise the latest first-pass verdict alone: "single".

Outcomes are compared as ("species", name), ("unidentifiable",) or
("no_animal",). Life stage and small box differences are not
disagreements. The 53 verdicts recorded before accounts existed have no
user_id and are ignored: anyone could write to that endpoint then.

The box (phase 3 crops with it): the latest corrected box among the
deciding verdicts -- the adjudication, or the first-pass and spot-check
pair -- and failing that, the latest corrected box from anyone who
answered the box. None means the model's own box. A box fix says where
the animal is, not what it is, so it outlives a later answer that changes
the label.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime

from psycopg.rows import dict_row

CONFIDENCE_RANK = {"uncertain": 0, "probable": 1, "certain": 2}
VERIFIED_STATUSES = ("single", "agreed", "adjudicated")


@dataclass(frozen=True)
class Verdict:
    id: int
    event_id: int
    user_id: int | None
    created_at: datetime
    corrected_name: str | None
    not_a_fish: bool
    unidentifiable: bool
    life_stage: str | None = None
    corrected_bbox: tuple[int, int, int, int] | None = None
    confidence: str = "certain"
    label_source: str | None = None
    is_adjudication: bool = False
    item_pass: int | None = None
    session_id: int | None = None

    @property
    def order(self) -> tuple:
        return (self.created_at, self.id)


@dataclass(frozen=True)
class Resolved:
    event_id: int
    status: str  # single | agreed | adjudicated | disputed
    outcome: tuple
    life_stage: str | None
    confidence: str
    bbox: tuple[int, int, int, int] | None  # None: use the model's box
    deciding: tuple[int, ...]
    user_ids: tuple[int, ...]
    first: Verdict | None = None
    second: Verdict | None = None

    @property
    def is_verified(self) -> bool:
        return self.status in VERIFIED_STATUSES

    @property
    def species(self) -> str | None:
        return self.outcome[1] if self.outcome[0] == "species" else None


def outcome_of(v: Verdict) -> tuple:
    if v.not_a_fish:
        return ("no_animal",)
    if v.unidentifiable:
        return ("unidentifiable",)
    return ("species", v.corrected_name)


def _latest(verdicts):
    return max(verdicts, key=lambda v: v.order) if verdicts else None


def _pick_bbox(deciding, everyone):
    for group in (deciding, everyone):
        for v in sorted(group, key=lambda v: v.order, reverse=True):
            if v.corrected_bbox:
                return tuple(v.corrected_bbox)
    return None


def resolve(verdicts) -> Resolved | None:
    eligible = [v for v in verdicts if v.user_id is not None]
    if not eligible:
        return None
    event_id = eligible[0].event_id
    adjudications = [v for v in eligible if v.is_adjudication]
    first = _latest([v for v in eligible if not v.is_adjudication and v.item_pass != 2])
    second = _latest([v for v in eligible if not v.is_adjudication and v.item_pass == 2])
    if adjudications:
        d = _latest(adjudications)
        return Resolved(
            event_id,
            "adjudicated",
            outcome_of(d),
            d.life_stage,
            d.confidence,
            _pick_bbox([d], eligible),
            (d.id,),
            (d.user_id,),
            first,
            second,
        )
    if first is None:
        first, second = second, None
    if second is None:
        return Resolved(
            event_id,
            "single",
            outcome_of(first),
            first.life_stage,
            first.confidence,
            _pick_bbox([first], eligible),
            (first.id,),
            (first.user_id,),
            first,
            None,
        )
    status = "agreed" if outcome_of(first) == outcome_of(second) else "disputed"
    confidence = min(first.confidence, second.confidence, key=lambda c: CONFIDENCE_RANK.get(c, 0))
    return Resolved(
        event_id,
        status,
        outcome_of(first),
        first.life_stage,
        confidence,
        _pick_bbox([first, second], eligible),
        (first.id, second.id),
        (first.user_id, second.user_id),
        first,
        second,
    )


def agreement_pairs(resolved: dict) -> list[tuple]:
    """(first outcome, spot-check outcome, how the first label was chosen)
    for every box that has both."""
    return [
        (outcome_of(r.first), outcome_of(r.second), r.first.label_source)
        for r in resolved.values()
        if r.first is not None and r.second is not None
    ]


def percent_agreement(pairs) -> float | None:
    if not pairs:
        return None
    return sum(1 for a, b in pairs if a == b) / len(pairs)


def krippendorff_alpha_nominal(pairs) -> float | None:
    """Krippendorff's alpha, nominal metric, for units that each carry two
    values from possibly different coders. Cohen's kappa assumes the same
    two raters for every unit; here each box's pair of volunteers differs.
    None when there is no variation to measure against."""
    if not pairs:
        return None
    coincidences: Counter = Counter()
    for a, b in pairs:
        coincidences[(a, b)] += 1
        coincidences[(b, a)] += 1
    marginals: Counter = Counter()
    for (c, _k), n in coincidences.items():
        marginals[c] += n
    total = sum(marginals.values())
    observed = sum(n for (c, k), n in coincidences.items() if c != k)
    expected = sum(marginals[c] * marginals[k] for c in marginals for k in marginals if c != k)
    if expected == 0:
        return None
    return 1.0 - (total - 1) * observed / expected


def agreement_summary(resolved: dict, min_pairs: int = 30) -> dict:
    pairs = agreement_pairs(resolved)

    def block(group):
        simple = [(a, b) for a, b, _source in group]
        return {
            "pairs": len(simple),
            "percent": percent_agreement(simple),
            "alpha": krippendorff_alpha_nominal(simple),
        }

    by_source: dict[str, list] = defaultdict(list)
    for p in pairs:
        by_source[p[2] or "none"].append(p)
    return {
        "overall": block(pairs),
        "min_pairs": min_pairs,
        "by_label_source": {k: block(v) for k, v in sorted(by_source.items())},
    }


_VERDICT_SQL = """
    SELECT c.id, c.event_id, c.user_id, c.created_at, c.corrected_name, c.not_a_fish,
           c.unidentifiable, c.life_stage, c.corrected_bbox, c.confidence, c.label_source,
           c.is_adjudication, c.session_id, ri.pass AS item_pass
      FROM detection_corrections c
      LEFT JOIN review_items ri ON ri.id = c.review_item_id
"""


def _row_to_verdict(r: dict) -> Verdict:
    bbox = r["corrected_bbox"]
    return Verdict(
        id=r["id"],
        event_id=r["event_id"],
        user_id=r["user_id"],
        created_at=r["created_at"],
        corrected_name=r["corrected_name"],
        not_a_fish=r["not_a_fish"],
        unidentifiable=r["unidentifiable"],
        life_stage=r["life_stage"],
        corrected_bbox=tuple(bbox) if bbox else None,
        confidence=r["confidence"],
        label_source=r["label_source"],
        is_adjudication=r["is_adjudication"],
        item_pass=r["item_pass"],
        session_id=r["session_id"],
    )


async def load_verdicts(conn, event_ids: list[int] | None = None) -> dict[int, list[Verdict]]:
    sql, params = _VERDICT_SQL, None
    if event_ids is not None:
        sql, params = _VERDICT_SQL + " WHERE c.event_id = ANY(%s)", (list(event_ids),)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, params)
        rows = await cur.fetchall()
    out: dict[int, list[Verdict]] = defaultdict(list)
    for r in rows:
        out[r["event_id"]].append(_row_to_verdict(r))
    return dict(out)


async def resolve_all(conn, event_ids: list[int] | None = None) -> dict[int, Resolved]:
    out = {}
    for event_id, verdicts in (await load_verdicts(conn, event_ids)).items():
        r = resolve(verdicts)
        if r is not None:
            out[event_id] = r
    return out
