# Reviewer guide

A short, non-technical guide for humans correcting fish-identification
labels at <https://wahoobay.org> (or the current Cloudflare tunnel URL
during the demo phase).

> **Why your corrections matter:** the model is good at *finding* fish
> but only modestly good at *naming* them — the team trained it on
> fish from all over the world and we live in a very specific corner
> of the Atlantic. Each label you add or correct is a real
> contribution to making the model accurate at Wahoo Bay. Roughly 50
> good corrections per species lifts that species's accuracy
> noticeably; 200 takes it close to expert-level.

## What you'll see

The dashboard has a **Recent events** panel showing the most recent
fish the system spotted. Each row looks something like:

> **Pomacanthus arcuatus** 56.1 % det 75 %
> 14:08:25 · pier_cam · frame 1023
> [correct]

What the parts mean:

- **Pomacanthus arcuatus** — the model's best guess at the species
  (Latin / scientific name). For most species we have a friendly common
  name in the Reef explorer panel; the Recent events panel shows the
  scientific name because that's what the database holds.
- **56.1 %** — the model's classifier confidence in that species.
  Lower than 60 % is "model isn't sure"; higher than 80 % is "the model
  thinks it's clearly this." Confidences in the 40–60 % range are where
  human review helps the most.
- **det 75 %** — the model's *detector* confidence that the bounding
  box contains a fish at all. Below ~30 % the detection is iffy; above
  60 % is solid.
- **14:08:25 · pier_cam · frame 1023** — when, which camera, which
  frame number. Useful if you want to look back at the exact moment
  the fish was seen.

## How to correct one

1. **Set up your reviewer email once** at the top of the Recent events
   panel. This tags every correction you submit so we can attribute
   labels (and run inter-rater reliability later).

2. **Paste the write token** in the small password field next to the
   email. The badge will turn green and read **reviewer**. (Without
   the token you can browse and look at fish, but can't submit
   corrections — this prevents random internet visitors from
   polluting the data.)

3. **Click `correct`** on a row. A small form opens.

4. **Type the species** in the *correct species name* field. Use the
   Latin binomial when you can (e.g. `Abudefduf saxatilis`); common
   names are OK as a fallback (e.g. `Sergeant Major`) — the team
   normalises later. **Capitalise normally** for Latin (Genus species).

5. **Pick a confidence:**
   - **certain** — you'd bet money on it. Distinct markings, clear
     view, no ambiguity.
   - **probable** — you're pretty sure but the angle / lighting /
     similar species in the area gives you some doubt. **Default
     choice when in doubt.**
   - **uncertain** — your best guess but you'd defer to an expert.
     These corrections are still useful for training but get filtered
     out of the strictest training sets.

6. **Tick `not a fish`** if the system boxed something that isn't a
   fish — biofouling, a piece of debris, a swimmer's foot, a shadow.
   Those become "negative examples" for the detector. Leave the
   species field blank in this case.

7. **Add a note** if anything weird is happening — "this is one of
   three identical fish in a school", "model confused this with a
   Cleaner Wrasse, but it's actually a pilot fish," "video frame is
   blurry, my best guess only." The notes ride along with the
   correction in the database.

8. **Click `save`.** The form collapses into a green "✓ saved" mark.
   The correction is now in the database; the corrections-counter at
   the bottom of the panel ticks up.

That's the whole loop.

## How long to spend per fish

**Don't agonise over each one.** The training pipeline is robust to
some noise; what helps it most is *coverage* — a few labels for many
species rather than perfectly-curated labels for a handful. A
reasonable rate is 30–60 corrections per hour: enough time to
recognise the fish but not enough to obsess over the species you can't
name. **If you genuinely don't know, mark it `uncertain` and move on.**

## What to look for

The **most useful corrections** in priority order:

1. **High-confidence wrong calls.** The model is confidently saying
   "Pomatomus saltatrix" but you can clearly see it's a Sergeant Major.
   These are the highest-leverage corrections — they fix the model's
   misplaced certainty.
2. **Low-confidence right calls.** The model says "Lutjanus griseus
   12 %" and it actually IS a Mangrove Snapper. Confirming gives the
   classifier positive reinforcement on a confused case.
3. **New species.** Anything the model has never seen on this camera
   before. Look at the Reef explorer's "Most-spotted fish" — anything
   not in that list is novel data.
4. **Common species you can name with high confidence.** Sergeant
   Major, Bermuda Chub, Great Barracuda, Snapper varieties, Grouper
   varieties. The bread-and-butter of fine-tuning.

The **least useful** corrections (don't waste time):

- Re-confirming labels the model is already getting right with high
  confidence (>85 %). The model already learned these.
- Endless variations on the same lingering fish. The system tracks
  them; one or two corrections per fish-track is plenty.
- Anything where the picture is too blurry / dark / occluded to be
  sure. Skip rather than guess.

## Common species at Wahoo Bay

Here's a non-exhaustive cheat-sheet of species you're likely to see at
the pier and SEAHIVECAM, so you can recognise them quickly. (For a
full list, the Reef explorer's "Most-spotted fish (last 24 h)" panel
is always current.)

| Common name | Scientific | Distinctive feature |
|---|---|---|
| Sergeant Major | Abudefduf saxatilis | Yellow back, 5 black vertical bars |
| Bermuda Chub | Kyphosus sectatrix | Silvery-blue, oval body, no obvious markings |
| Great Barracuda | Sphyraena barracuda | Long torpedo body, sharp teeth, silver |
| Cleaner Wrasse | Labroides dimidiatus | Tiny, pale, dark horizontal stripe |
| Bluefish | Pomatomus saltatrix | Robust silver body, forked tail |
| Scrawled Filefish | Aluterus scriptus | Thin profile, blue squiggle pattern |
| Lionfish (invasive!) | Pterois volitans | Reddish-brown stripes, big "fan" fins |
| Nurse Shark | Ginglymostoma cirratum | Brown/grey, two stubby barbels by mouth |
| Mahi-mahi (Dolphinfish) | Coryphaena hippurus | Bright yellow + green, blue accents, blunt head |
| Atlantic Sailfish | Istiophorus albicans | Huge sail-like dorsal fin, pointed bill |
| Snapper varieties | Lutjanus spp. | Reddish/grey body, single dorsal fin, sharp teeth |
| Grouper varieties | Mycteroperca / Epinephelus | Heavy bodies, big mouths, often near structure |

When in doubt, [iNaturalist](https://www.inaturalist.org/) and
[FishBase](https://www.fishbase.se/) are excellent free identification
references. iNaturalist's "Suggest an identification" feature on a
saved frame can give you a starting point.

## A note on lionfish

If you're confident you see a **lionfish (Pterois volitans)** and the
model labelled it differently, please correct it explicitly with
**`certain`** confidence. Lionfish are an invasive species in the
Atlantic; reliably tracking sightings is a real conservation use of
this dataset.

## What happens to your corrections

Every correction goes into the `detection_corrections` database with:

- the species name you typed (or the not-a-fish flag),
- your reviewer email,
- your confidence level (certain / probable / uncertain),
- your notes,
- the timestamp,
- a reference back to the original detection event (which has the
  bounding box, the saved frame, and the model's original guess).

Periodically the team exports those corrections, crops the
corresponding fish from the saved frames, and uses the result to
fine-tune the classifier. After a few hundred corrections per common
species, a new model checkpoint goes live, the dashboard's species
calls become more accurate, and your share of any future model card
will read "thanks to volunteer reviewers."

## Privacy

We don't track you. Your reviewer email is stored on the corrections
you submit (so we can credit you and run inter-rater agreement) but
we don't log views, IPs, or anything else. Your local browser
remembers your email and write token; nothing else.

## Questions

For technical questions email <dzimmerman2021@fau.edu>. For ecology /
species-ID questions, ping the Wahoo Bay team or post in whatever
volunteer-reviewer Slack/Discord exists by the time you're reading
this.
