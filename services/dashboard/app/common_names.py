"""Latin → friendly common-name lookup.

Two layers feed the lookup:

1. The hand-curated `COMMON_NAMES` dict below — preferred labels we
   explicitly want (e.g. "Blue Tang" rather than the iNat-canonical
   "Atlantic Blue Tang"). Edit here when a class deserves a specific
   short label.

2. At module import we merge in any binomial → common_name pairs from
   the dataset manifests (assets/species_manifest.json for the Wahoo
   Bay spreadsheet, assets/inat_manifest.json for iNat photos).
   Curated entries always win; manifests fill the long tail of
   classes nobody bothered to hand-label.

If neither source has a name for a binomial, the dashboard falls
through to italic Latin alone — that's the "(G. species)" you see in
the UI.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

COMMON_NAMES: dict[str, str] = {
    # Damselfishes
    "Abudefduf saxatilis": "Sergeant Major",
    "Abudefduf sordidus": "Black-spot Sergeant",
    "Stegastes partitus": "Bicolor Damselfish",
    "Chromis chromis": "Damselfish",
    # Angelfishes
    "Holacanthus bermudensis": "Blue Angelfish",
    "Holacanthus tricolor": "Rock Beauty",
    "Pomacanthus arcuatus": "Gray Angelfish",
    "Pomacanthus paru": "French Angelfish",
    # Butterflyfishes
    "Chaetodon ocellatus": "Spotfin Butterflyfish",
    "Chaetodon striatus": "Banded Butterflyfish",
    # Surgeonfishes / Tangs
    "Acanthurus chirurgus": "Doctorfish",
    "Acanthurus coeruleus": "Blue Tang",
    "Paracanthurus hepatus": "Blue Tang (Indo-Pacific)",
    # Parrotfishes
    "Sparisoma viride": "Stoplight Parrotfish",
    "Sparisoma aurofrenatum": "Redband Parrotfish",
    "Scarus iseri": "Striped Parrotfish",
    # Wrasses & hogfishes
    "Halichoeres bivittatus": "Slippery Dick",
    "Bodianus rufus": "Spanish Hogfish",
    "Lachnolaimus maximus": "Hogfish",
    "Labroides dimidiatus": "Cleaner Wrasse",
    # Snappers
    "Lutjanus griseus": "Mangrove Snapper",
    "Lutjanus apodus": "Schoolmaster Snapper",
    "Lutjanus cyanopterus": "Cubera Snapper",
    "Lutjanus synagris": "Lane Snapper",
    "Lutjanus purpureus": "Caribbean Red Snapper",
    "Rhomboplites aurorubens": "Vermilion Snapper",
    # Groupers
    "Mycteroperca microlepis": "Gag Grouper",
    "Mycteroperca bonaci": "Black Grouper",
    "Mycteroperca tigris": "Tiger Grouper",
    "Epinephelus striatus": "Nassau Grouper",
    "Epinephelus morio": "Red Grouper",
    "Centropristis striata": "Black Sea Bass",
    # Grunts & porgies
    "Haemulon parra": "Sailor's Choice",
    "Haemulon flavolineatum": "French Grunt",
    "Haemulon sciurus": "Bluestriped Grunt",
    "Haemulon plumieri": "White Grunt",
    "Haemulon chrysargyreum": "Smallmouth Grunt",
    "Calamus bajonado": "Jolthead Porgy",
    "Diplodus holbrookii": "Spottail Pinfish",
    "Diplodus vulgaris": "Common Two-banded Seabream",
    "Lagodon rhomboides": "Pinfish",
    "Archosargus probatocephalus": "Sheepshead",
    # Drums / croakers
    "Sciaenops ocellatus": "Red Drum",
    "Pogonias cromis": "Black Drum",
    "Menticirrhus littoralis": "Gulf Kingfish",
    # Jacks
    "Caranx ruber": "Bar Jack",
    "Caranx hippos": "Crevalle Jack",
    "Seriola dumerili": "Greater Amberjack",
    "Seriola rivoliana": "Almaco Jack",
    "Alectis ciliaris": "African Pompano",
    "Pomatomus saltatrix": "Bluefish",
    # Mullets / mojarras
    "Mugil cephalus": "Striped Mullet",
    # Tarpon, snook, mahi
    "Megalops atlanticus": "Tarpon",
    "Centropomus undecimalis": "Common Snook",
    "Coryphaena hippurus": "Mahi-mahi",
    "Elops saurus": "Ladyfish",
    # Tunas, mackerel, billfish
    "Thunnus atlanticus": "Blackfin Tuna",
    "Katsuwonus pelamis": "Skipjack Tuna",
    "Acanthocybium solandri": "Wahoo",
    "Sphyraena barracuda": "Great Barracuda",
    "Istiophorus albicans": "Atlantic Sailfish",
    # Filefish & pufferfish
    "Aluterus scriptus": "Scrawled Filefish",
    "Aluterus monoceros": "Unicorn Filefish",
    "Aluterus schoepfii": "Orange Filefish",
    "Acanthostracion quadricornis": "Scrawled Cowfish",
    "Canthigaster rostrata": "Sharpnose Puffer",
    "Diodon hystrix": "Spot-fin Porcupinefish",
    "Arothron hispidus": "White-spotted Puffer",
    # Lionfish (invasive)
    "Pterois volitans": "Lionfish (invasive)",
    # Sharks & rays
    "Ginglymostoma cirratum": "Nurse Shark",
    "Mustelus canis": "Dusky Smooth-hound",
    "Mustelus mustelus": "Smooth-hound Shark",
    "Rhizoprionodon terraenovae": "Atlantic Sharpnose Shark",
    "Carcharhinus plumbeus": "Sandbar Shark",
    "Aetobatus narinari": "Spotted Eagle Ray",
    "Gymnura altavela": "Spiny Butterfly Ray",
    # Eels
    "Gymnothorax moringa": "Spotted Moray Eel",
    "Gymnothorax funebris": "Green Moray Eel",
    "Echiophis intertinctus": "Spotted Spoon-nose Eel",
    # Chubs & damselfishes (continued)
    "Kyphosus sectatrix": "Bermuda Chub",
    "Kyphosus vaigiensis": "Brassy Chub",
    # Other
    "Echeneis naucrates": "Live Sharksucker",
    "Aulostomus maculatus": "Trumpetfish",
    "Cephalopholis cruentata": "Graysby",
    "Anisotremus virginicus": "Porkfish",
    "Anisotremus surinamensis": "Black Margate",
    # The Wahoo Bay YouTube channel mentioned several specifically
    "Rachycentron canadum": "Cobia",
}


# ---- manifest-derived fallback layer --------------------------------------
#
# Populated once at import. Tried only when COMMON_NAMES has no entry for a
# binomial, so the hand-curated label always wins. Stored separately (not
# merged into COMMON_NAMES) so a future hot-reload could replace just this
# layer without dropping the curated overrides.
_MANIFEST_NAMES: dict[str, str] = {}


def _repo_root() -> Path:
    # services/dashboard/app/common_names.py → repo root is three parents up.
    return Path(__file__).resolve().parents[3]


def _ingest_manifest(path: Path, b_key: str, c_key: str) -> int:
    """Add (binomial → common_name) pairs from a JSON manifest. Returns the
    number of entries newly added. First non-empty common name per binomial
    wins; manifests are checked in order at module load."""
    added = 0
    try:
        rows = json.loads(path.read_text())
    except FileNotFoundError:
        return 0
    except Exception as e:
        log.warning("common_names: failed to read %s (%s); skipping", path, e)
        return 0
    if not isinstance(rows, list):
        return 0
    for r in rows:
        if not isinstance(r, dict):
            continue
        b = (r.get(b_key) or "").strip()
        c = (r.get(c_key) or "").strip()
        if not b or not c:
            continue
        if b in COMMON_NAMES or b in _MANIFEST_NAMES:
            continue
        _MANIFEST_NAMES[b] = c
        added += 1
    return added


def _load_manifest_names() -> None:
    root = _repo_root()
    # species_manifest.json first (spreadsheet-curated, generally the
    # cleanest labels). inat_manifest.json second to backfill the tail.
    n_sm = _ingest_manifest(root / "assets" / "species_manifest.json", "binomial", "common_name")
    n_in = _ingest_manifest(root / "assets" / "inat_manifest.json", "binomial", "common_name")
    if _MANIFEST_NAMES:
        log.info(
            "common_names: loaded %d manifest fallbacks (species_manifest=%d, inat=%d)",
            len(_MANIFEST_NAMES),
            n_sm,
            n_in,
        )


def reload_manifest_names() -> int:
    """Drop and rebuild the manifest fallback layer. Called by the
    sheet poller after species_manifest.json gets rewritten. Returns
    the new entry count for logging."""
    _MANIFEST_NAMES.clear()
    _load_manifest_names()
    return len(_MANIFEST_NAMES)


_load_manifest_names()


def common(latin_name: str | None) -> str | None:
    """Return the friendly common name if known; else None."""
    if not latin_name:
        return None
    return COMMON_NAMES.get(latin_name) or _MANIFEST_NAMES.get(latin_name)


def display(latin_name: str | None) -> str:
    """Return 'Common Name (Latinus binomialis)' or just the latin name."""
    if not latin_name:
        return "Unknown"
    cn = common(latin_name)
    return f"{cn} ({latin_name})" if cn else latin_name
