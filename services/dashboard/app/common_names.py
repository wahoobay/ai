"""Latin → friendly common-name lookup.

Curated for SE-Florida / Caribbean reef fauna we expect to see at Wahoo Bay
or the Pompano pier. Anything not in this dict falls back to the Latin
binomial in the UI; users can still google it. Add entries here as the
classifier surfaces species we want to label nicely.
"""
from __future__ import annotations

COMMON_NAMES: dict[str, str] = {
    # Damselfishes
    "Abudefduf saxatilis":        "Sergeant Major",
    "Abudefduf sordidus":         "Black-spot Sergeant",
    "Stegastes partitus":         "Bicolor Damselfish",
    "Chromis chromis":            "Damselfish",
    # Angelfishes
    "Holacanthus bermudensis":    "Blue Angelfish",
    "Holacanthus tricolor":       "Rock Beauty",
    "Pomacanthus arcuatus":       "Gray Angelfish",
    "Pomacanthus paru":           "French Angelfish",
    # Butterflyfishes
    "Chaetodon ocellatus":        "Spotfin Butterflyfish",
    "Chaetodon striatus":         "Banded Butterflyfish",
    # Surgeonfishes / Tangs
    "Acanthurus chirurgus":       "Doctorfish",
    "Acanthurus coeruleus":       "Blue Tang",
    "Paracanthurus hepatus":      "Blue Tang (Indo-Pacific)",
    # Parrotfishes
    "Sparisoma viride":           "Stoplight Parrotfish",
    "Sparisoma aurofrenatum":     "Redband Parrotfish",
    "Scarus iseri":               "Striped Parrotfish",
    # Wrasses & hogfishes
    "Halichoeres bivittatus":     "Slippery Dick",
    "Bodianus rufus":              "Spanish Hogfish",
    "Lachnolaimus maximus":       "Hogfish",
    "Labroides dimidiatus":       "Cleaner Wrasse",
    # Snappers
    "Lutjanus griseus":           "Mangrove Snapper",
    "Lutjanus apodus":            "Schoolmaster Snapper",
    "Lutjanus cyanopterus":       "Cubera Snapper",
    "Lutjanus synagris":          "Lane Snapper",
    "Lutjanus purpureus":         "Caribbean Red Snapper",
    "Rhomboplites aurorubens":    "Vermilion Snapper",
    # Groupers
    "Mycteroperca microlepis":    "Gag Grouper",
    "Mycteroperca bonaci":        "Black Grouper",
    "Mycteroperca tigris":        "Tiger Grouper",
    "Epinephelus striatus":       "Nassau Grouper",
    "Epinephelus morio":          "Red Grouper",
    "Centropristis striata":      "Black Sea Bass",
    # Grunts & porgies
    "Haemulon parra":             "Sailor's Choice",
    "Haemulon flavolineatum":     "French Grunt",
    "Haemulon sciurus":           "Bluestriped Grunt",
    "Haemulon plumieri":          "White Grunt",
    "Haemulon chrysargyreum":     "Smallmouth Grunt",
    "Calamus bajonado":           "Jolthead Porgy",
    "Diplodus holbrookii":        "Spottail Pinfish",
    "Diplodus vulgaris":          "Common Two-banded Seabream",
    "Lagodon rhomboides":         "Pinfish",
    "Archosargus probatocephalus":"Sheepshead",
    # Drums / croakers
    "Sciaenops ocellatus":        "Red Drum",
    "Pogonias cromis":             "Black Drum",
    "Menticirrhus littoralis":    "Gulf Kingfish",
    # Jacks
    "Caranx ruber":               "Bar Jack",
    "Caranx hippos":              "Crevalle Jack",
    "Seriola dumerili":           "Greater Amberjack",
    "Seriola rivoliana":          "Almaco Jack",
    "Alectis ciliaris":           "African Pompano",
    "Pomatomus saltatrix":        "Bluefish",
    # Mullets / mojarras
    "Mugil cephalus":             "Striped Mullet",
    # Tarpon, snook, mahi
    "Megalops atlanticus":        "Tarpon",
    "Centropomus undecimalis":    "Common Snook",
    "Coryphaena hippurus":        "Mahi-mahi",
    "Elops saurus":               "Ladyfish",
    # Tunas, mackerel, billfish
    "Thunnus atlanticus":         "Blackfin Tuna",
    "Katsuwonus pelamis":         "Skipjack Tuna",
    "Acanthocybium solandri":     "Wahoo",
    "Sphyraena barracuda":        "Great Barracuda",
    "Istiophorus albicans":       "Atlantic Sailfish",
    # Filefish & pufferfish
    "Aluterus scriptus":          "Scrawled Filefish",
    "Aluterus monoceros":         "Unicorn Filefish",
    "Aluterus schoepfii":         "Orange Filefish",
    "Acanthostracion quadricornis":"Scrawled Cowfish",
    "Canthigaster rostrata":      "Sharpnose Puffer",
    "Diodon hystrix":             "Spot-fin Porcupinefish",
    "Arothron hispidus":          "White-spotted Puffer",
    # Lionfish (invasive)
    "Pterois volitans":           "Lionfish (invasive)",
    # Sharks & rays
    "Ginglymostoma cirratum":     "Nurse Shark",
    "Mustelus canis":             "Dusky Smooth-hound",
    "Mustelus mustelus":          "Smooth-hound Shark",
    "Rhizoprionodon terraenovae": "Atlantic Sharpnose Shark",
    "Carcharhinus plumbeus":      "Sandbar Shark",
    "Aetobatus narinari":         "Spotted Eagle Ray",
    "Gymnura altavela":           "Spiny Butterfly Ray",
    # Eels
    "Gymnothorax moringa":        "Spotted Moray Eel",
    "Gymnothorax funebris":       "Green Moray Eel",
    "Echiophis intertinctus":     "Spotted Spoon-nose Eel",
    # Chubs & damselfishes (continued)
    "Kyphosus sectatrix":         "Bermuda Chub",
    "Kyphosus vaigiensis":        "Brassy Chub",
    # Other
    "Echeneis naucrates":         "Live Sharksucker",
    "Aulostomus maculatus":       "Trumpetfish",
    "Cephalopholis cruentata":    "Graysby",
    "Anisotremus virginicus":     "Porkfish",
    "Anisotremus surinamensis":   "Black Margate",
    # The Wahoo Bay YouTube channel mentioned several specifically
    "Rachycentron canadum":       "Cobia",
}


def common(latin_name: str | None) -> str | None:
    """Return the friendly common name if known; else None."""
    if not latin_name:
        return None
    return COMMON_NAMES.get(latin_name)


def display(latin_name: str | None) -> str:
    """Return 'Common Name (Latinus binomialis)' or just the latin name."""
    if not latin_name:
        return "Unknown"
    cn = COMMON_NAMES.get(latin_name)
    return f"{cn} ({latin_name})" if cn else latin_name
