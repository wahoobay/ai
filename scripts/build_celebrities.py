"""Parse the 'Fish named by Chatters' sheet from assets/species.xlsx
into assets/celebrities.json — the source-of-truth list for the
Celebrities dashboard feature.

Each celebrity is one named recurring animal (e.g. "Bobby the Blue
Tang with juvenile yellow coloring", "Marty the Black Margate with
a remora on its head") with their nickname, species type,
distinguishing visual marks, the camera operators / chat moderators
who named them, and the first / last dates they were seen.

The "Common name" column gets matched against our common-names
catalogue (curated dict + species_manifest fallback) to recover the
binomial where possible — so the Celebrities tab can group by
species, and the reviewer modal can pre-filter the "Tag as celebrity"
dropdown by the species the reviewer is correcting toward.

Run: python scripts/build_celebrities.py
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

import openpyxl

REPO_ROOT = Path(__file__).resolve().parent.parent
SHEET_NAME = "Fish named by Chatters"

# Reuse the dashboard's common-name lookup for binomial recovery.
sys.path.insert(0, str(REPO_ROOT / "services" / "dashboard"))
from app.common_names import _MANIFEST_NAMES, COMMON_NAMES  # noqa: E402


def _normalize(s: str) -> str:
    """Lowercased, single-spaced, suffix-stripped form for fuzzy match.
    Drops parentheticals ("Blue Tang (yellow juvi colours)" → "blue tang"),
    trailing "#N" variants, "poss." disjunctions, and leading qualifiers
    like "large", "Adult"."""
    if not s:
        return ""
    s = str(s)
    s = re.sub(r"\s*\([^)]*\)", "", s)  # drop "(...)" parentheticals
    s = re.sub(r"#\s*\d+\s*$", "", s).strip()  # trailing "#1", "#2"
    s = re.sub(r"\s+poss\..*$", "", s, flags=re.I).strip()  # ", poss. <other>"
    s = re.sub(r"^(large|huge|tiny|big|adult|juvi|juvenile)\s+", "", s, flags=re.I)
    s = re.sub(r"\s+", " ", s.lower()).strip()
    return s


# Reverse lookup: common-name (normalised) → binomial. Curated wins
# over manifest; within a layer the *cleanest* common-name (the one
# whose original form had no parenthetical qualifier) wins on collision.
# Otherwise "Blue Tang" (Acanthurus coeruleus, Caribbean) loses to
# "Blue Tang (Indo-Pacific)" (Paracanthurus hepatus) because both
# normalise to "blue tang" after the parenthetical strip.
def _build_reverse_lookup() -> dict[str, str]:
    """Returns normalised-common → binomial. Curated dict overrides
    manifest entirely. WITHIN each layer, entries whose source
    common-name had no parenthetical qualifier win — so the natural
    "Blue Tang" (Acanthurus coeruleus, Caribbean) sticks instead of
    being overwritten by the disambiguated "Blue Tang (Indo-Pacific)"
    (Paracanthurus hepatus) when both normalise to "blue tang"."""
    out: dict[str, str] = {}
    for layer, overwrite_prev_layer in [
        (_MANIFEST_NAMES, False),  # written first, kept where curated is silent
        (COMMON_NAMES, True),  # overwrites manifest entries entirely
    ]:
        seen_in_layer: set[str] = set()
        # Paren-free names sort first so they win the within-layer race.
        items = sorted(layer.items(), key=lambda kv: ("(" in (kv[1] or ""), kv[1]))
        for binomial, common in items:
            key = _normalize(common)
            if not key or key in seen_in_layer:
                continue
            seen_in_layer.add(key)
            if overwrite_prev_layer or key not in out:
                out[key] = binomial
    return out


COMMON_TO_LATIN: dict[str, str] = _build_reverse_lookup()


def _slug(nickname: str) -> str:
    """'Bobby Jr.' → 'bobby_jr'. Lowercased, ASCII-only, underscore-joined."""
    base = nickname.split(" - ")[0].split(" – ")[0].strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", base).strip("_")
    return s or "unnamed"


def _parse_date(v) -> str | None:
    """Try several formats from the sheet; return ISO date or the raw
    free-text (e.g. "daily", "Dec 11th") if we can't parse it."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    s = str(v).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return s  # keep human strings ("daily", "Dec 11th", "Mar 3rd/Apr 15th") as-is


def parse_row(row: tuple) -> dict | None:
    """Map a sheet row to a celebrity dict. Returns None if the row is
    too sparse to be meaningful (missing name+id or species)."""
    num, common, name_id, screenshot, first_seen, named_by, last_seen = (row + (None,) * 7)[:7]
    if not name_id or not common:
        return None
    text = str(name_id).strip()
    # Split "Bobby - adult Blue Tang, but still got juvi colours" into
    # nickname + id_mark. Both em-dash and hyphen are used in the sheet.
    parts = re.split(r"\s+[-–]\s+", text, maxsplit=1)
    nickname = parts[0].strip()
    id_mark = parts[1].strip() if len(parts) > 1 else ""

    common_clean = str(common).strip()
    binomial = COMMON_TO_LATIN.get(_normalize(common_clean))
    return {
        "id": _slug(nickname),
        "nickname": nickname,
        "species_common": common_clean,
        # Best-effort binomial. Null when the sheet's free-text species
        # name doesn't match anything we know about — that's fine; the
        # celebrity still gets a directory entry.
        "binomial": binomial,
        "id_mark": id_mark,
        "screenshot_label": str(screenshot).strip() if screenshot else None,
        "first_seen": _parse_date(first_seen),
        "last_seen": _parse_date(last_seen),
        "named_by": str(named_by).strip() if named_by else None,
    }


def main() -> None:
    xlsx = REPO_ROOT / "assets" / "species.xlsx"
    wb = openpyxl.load_workbook(xlsx, data_only=True)
    ws = wb[SHEET_NAME]

    celebrities: list[dict] = []
    seen_ids: dict[str, int] = {}  # id → suffix counter for collisions
    for row in ws.iter_rows(min_row=3, values_only=True):
        c = parse_row(row)
        if c is None:
            continue
        # De-collide slugs ("bobby" + "bobby_jr" should not collide, but
        # if two distinct entries hash to the same id, append "_2", etc.)
        base = c["id"]
        if base in seen_ids:
            seen_ids[base] += 1
            c["id"] = f"{base}_{seen_ids[base]}"
        else:
            seen_ids[base] = 1
        celebrities.append(c)

    out_path = REPO_ROOT / "assets" / "celebrities.json"
    payload = {
        "_about": (
            "Named recurring animals at Wahoo Bay, sourced from the "
            "'Fish named by Chatters' sheet inside assets/species.xlsx. "
            "Regenerate by re-running scripts/build_celebrities.py "
            "after editing the sheet. Each entry's `id` is the "
            "stable identifier used by detection_corrections.celebrity_id."
        ),
        "count": len(celebrities),
        "celebrities": celebrities,
    }
    out_path.write_text(json.dumps(payload, indent=2))
    matched = sum(1 for c in celebrities if c["binomial"])
    print(f"wrote {out_path} — {len(celebrities)} celebrities ({matched} with matched binomial)")


if __name__ == "__main__":
    main()
