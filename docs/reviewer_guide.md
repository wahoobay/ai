# Reviewer guide

A short, non-technical guide for humans correcting fish identifications
at the Wahoo Bay dashboard (currently a Cloudflare quick-tunnel URL —
ask the team for the active link).

> **Why your corrections matter:** the model is good at *finding*
> fish in each frame, but only modestly good at *naming* them — it's
> been trained on Wahoo Bay community footage plus iNaturalist photos,
> and reviewer corrections are how it learns the things only humans
> who actually watch the cameras can teach it (e.g. that the model
> calls every juvenile angelfish a French Angelfish because it hasn't
> seen enough Gray Angelfish yet). Roughly 50 good corrections per
> species lifts that species's accuracy noticeably; 200 takes it close
> to expert-level. Every "Looks right" also counts — it tells the
> model "yes, you got this one."

---

## What you'll see

The dashboard's **Recent events** panel shows the most recent fish
the system spotted. Each row is one detection — typically you'll see
several new ones per minute when the cameras are busy.

Each event row has:

- **A thumbnail** of the cropped fish (left). Click it to enlarge.
- **The model's best guess** at the species, written as
  *Common Name (G. species)* with the genus abbreviated to its
  initial, e.g. **Sergeant Major (A. saxatilis)**. Species without a
  curated common name appear as italic Latin alone, e.g.
  *(C. arctifrons)*.
- **An accuracy chip** — the classifier's softmax confidence. The
  model only shows a species name when its confidence is ≥ 55 %;
  below that you'll see the box but the species reads **unknown**.
- **A "det" chip** — the *detector* confidence that the bounding box
  contains a fish at all. Independent of the species call.
- **Time · camera · frame ID** — when, which camera, which frame.
- **Two action buttons:**
  - **✓ Looks right** — one-click endorsement. Click this when the
    model got it right and you'd rather not retype.
  - **✏️ Correct** — opens the full correction form.

Reviewed events disappear from the list immediately after you submit
(they're filtered server-side too on the next refresh).

---

## The big modal — what each tab does

Clicking the thumbnail opens an enlarged view with three tabs:

### ROI

The cropped fish region, with the bbox drawn. This is what you'll
spend most time on — it's a tight zoom on just the animal so you can
recognise it.

### Full frame

The whole camera frame, with the detection's bbox drawn over it.
Useful when:

- The ROI is ambiguous and you want to see context (is it near the
  surface? near a hole? next to another animal?).
- You want to see whether the bbox actually contains the right fish
  (versus a piece of another animal next to it).

### ✎ Edit bbox

An SVG editor that lets you redraw the bounding box. You'll only need
this when:

- The detector clipped the fish (caught only the head, only the tail).
- The detector boxed two fish as one and the centre is off.
- The bbox is too generous and includes a lot of background.

Drag the corners or edges to resize; drag inside the box to move it.
"Reset" puts it back to the model's original box. The corrected box
travels with whatever correction you submit next (endorse, correct,
or "not a fish") — so you don't have to do anything special; just
redraw, then submit the species + confidence as usual.

The status line under the editor turns amber when the bbox has been
edited; that's how you know the redraw will be saved.

---

## Setting yourself up — once

At the top of the Recent events panel:

1. **Reviewer name field** — type your name (or initials, or an
   email). Whatever you put here gets attached to every correction
   you submit so we can attribute labels and run inter-rater
   reliability later. Saved in your browser's localStorage.

2. **Write token field** — a small password input. The dashboard's
   write endpoints require this to prevent random visitors from
   polluting the data. Ask the team for the current token; you only
   have to paste it once.

3. The status chip should now read **"reviewer: <name>"** in green.
   Without it, you can browse and look at fish, but Save buttons will
   reject your submissions.

---

## The correction form — field by field

After clicking **✏️ Correct** (or after submitting via the modal's
action panel — same form), you'll see:

### Top-K alternatives

If the model considered other species likely, you'll see chips like:

> *Or pick a top-K alternative:*
> **French Angelfish (P. paru) 43.2%** · **Gray Angelfish (P. arcuatus) 30.1%**

Click the chip if the model's #2 or #3 guess is actually right —
that's a one-click correction with no typing.

### Type the correct species name

Free-text input with autocomplete from the loaded species catalogue
(~225 species). Start typing the common name or the Latin binomial;
matches appear in a dropdown below the field.

**Format tips:**

- Latin binomials: *Genus species* with normal capitalisation
  (e.g. `Abudefduf saxatilis`).
- Common names are fine too; we normalise them server-side
  (e.g. `Sergeant Major` resolves to *Abudefduf saxatilis*).
- For species not in the catalogue (you spotted something new!), type
  the full Latin name; the team will fold it into the catalogue on
  the next sheet refresh.

### Tag as celebrity (optional)

A dropdown of named recurring animals filtered to the species you're
correcting toward. Most detections are anonymous fish — leave this
on **"— not a known individual —"**. But if you're confident this is
**Bobby** the Blue Tang with juvenile yellow coloring, or **Marty**
the Black Margate with the remora hat, or **Stumpy** the Southern
Stingray missing most of its tail — tag it. The celebrity's photo
gallery + sightings count updates immediately.

### ✗ Not a fish

Click this when the detector boxed something that isn't an animal —
biofouling, debris, a swimmer's foot, a shadow, a hole in the reef
structure. Leave the species field blank; the "Not a fish" click is
the whole correction.

These become **hard negatives** for future detector retraining — the
more "Not a fish" clicks we collect on a particular kind of artifact,
the more weight that pattern gets at the next training, and the less
often the detector will fire on it.

### Confidence

Three options:

- **certain** — you'd bet money on it. Distinct markings, clear view,
  no ambiguity.
- **probable** — you're pretty sure but the angle / lighting /
  similar species in the area gives you some doubt. **Default
  choice when in doubt.**
- **uncertain** — your best guess but you'd defer to an expert.
  Still useful for training, just gets filtered out of the strictest
  training sets.

### Notes (optional)

Anything strange that doesn't fit elsewhere. Examples:

> "this is one of three identical fish in a school"

> "model confused this with a Cleaner Wrasse, but it's actually a pilot fish"

> "video frame is blurry, my best guess only"

> "PTZ rotation in progress — fish is partially occluded by frame edge"

Notes ride along with the correction in the database. Reviewers
looking at the same event later can read them.

---

## How long to spend per fish

**Don't agonise over each one.** The training pipeline is robust to
some noise; what helps it most is *coverage* — a few labels for many
species rather than perfectly-curated labels for a handful. A
reasonable rate is **30-60 corrections per hour**: enough time to
recognise the fish but not enough to obsess over the species you
can't name. **If you genuinely don't know, mark it `uncertain` and
move on.** "✓ Looks right" should be ~5 seconds per click.

---

## What to look for

The **most useful corrections**, in priority order:

1. **High-confidence wrong calls.** The model is confidently saying
   *Pomatomus saltatrix* but you can clearly see it's a Sergeant
   Major. These are the highest-leverage corrections — they fix the
   model's misplaced certainty.
2. **"unknown" detections that you DO recognise.** When the model's
   top-1 is below 55 %, the species shows as **unknown**. If you can
   identify the fish, that's pure new training signal.
3. **Low-confidence right calls.** The model says *Lutjanus griseus
   62 %* and it actually IS a Mangrove Snapper. Confirming with
   "✓ Looks right" gives positive reinforcement on a borderline case.
4. **New species.** Anything you haven't seen the model name before.
   Check the Reef Explorer's "Most-spotted fish" — anything not in
   that list is novel data.
5. **Celebrity sightings.** If you recognise Bobby / Marty / Stumpy
   / Spot / etc., tagging them keeps the celebrity galleries fresh
   and gives the team a hook for the public-facing storytelling.

The **least useful** corrections (don't waste time):

- Re-confirming labels the model is already getting right with high
  confidence (>85 %) and that you've already endorsed before. The
  model already learned these.
- Endless variations on the same lingering fish. The system tracks
  them; one or two corrections per fish-track is plenty.
- Anything where the picture is too blurry / dark / occluded to be
  sure. Skip rather than guess.

---

## A short cheat-sheet of common Wahoo Bay species

(For a full list, the Fish Explorer tab's table is always current.)

| Common name | Scientific | Distinctive feature |
|---|---|---|
| Sergeant Major | *Abudefduf saxatilis* | Yellow back, 5 black vertical bars |
| Bermuda Chub | *Kyphosus sectatrix* | Silvery-blue, oval body, no obvious markings |
| Great Barracuda | *Sphyraena barracuda* | Long torpedo body, sharp teeth, silver |
| Cleaner Wrasse | *Labroides dimidiatus* | Tiny, pale, dark horizontal stripe |
| Bluefish | *Pomatomus saltatrix* | Robust silver body, forked tail |
| Scrawled Filefish | *Aluterus scriptus* | Thin profile, blue squiggle pattern |
| Lionfish (invasive!) | *Pterois volitans* | Reddish-brown stripes, big "fan" fins |
| Nurse Shark | *Ginglymostoma cirratum* | Brown/grey, two stubby barbels by mouth |
| Mahi-mahi (Dolphinfish) | *Coryphaena hippurus* | Bright yellow + green, blue accents, blunt head |
| Atlantic Sailfish | *Istiophorus albicans* | Huge sail-like dorsal fin, pointed bill |
| Snapper varieties | *Lutjanus spp.* | Reddish/grey body, single dorsal fin, sharp teeth |
| Grouper varieties | *Mycteroperca / Epinephelus* | Heavy bodies, big mouths, often near structure |
| Goliath Grouper | *Epinephelus itajara* | Massive (1–2 m+), brownish, square tail, often near holes |
| Spotted Moray Eel | *Gymnothorax moringa* | Long body, white-on-purple spots, often in holes |
| French Angelfish | *Pomacanthus paru* | Black with yellow scale edges (adult); juvenile is black with 4 vertical yellow bars |
| Goldspotted Grunt | *Anisotremus chrysargyreum* | Striped grunt with yellow spots |

When in doubt, [iNaturalist](https://www.inaturalist.org/) and
[FishBase](https://www.fishbase.se/) are excellent free
identification references. iNaturalist's "Suggest an identification"
feature on a saved frame can give you a good starting point.

---

## A note on lionfish

If you're confident you see a **lionfish (*Pterois volitans*)** and
the model labelled it differently, please correct it explicitly with
**`certain`** confidence. Lionfish are an invasive species in the
Atlantic; reliably tracking sightings is a real conservation use of
this dataset.

---

## A note on the SEAHIVE holes

The SEAHIVECAM looks down at an artificial reef structure with dark
oval cut-outs (the "SEAHIVE holes"). The detector reliably emits
false-positive boxes on these because they look fish-shaped from above.

There's a per-camera filter that drops most of these automatically
(it suppresses any detection whose box closely matches a configured
hole polygon). But the filter is conservative on purpose — it keeps
real fish that swim *through* a hole as detections. So you might
still see the occasional hole-detection that slipped through.

If you see one: **✗ Not a fish**, `certain` confidence. Every one
of those clicks helps refine the detector retraining set.

---

## What happens to your corrections

Every correction lands in the `detection_corrections` database table
with:

- the species name you typed (or the **not_a_fish** flag, or the
  `is_endorsement` flag for "Looks right"),
- your reviewer name,
- your confidence level (certain / probable / uncertain),
- your notes,
- the timestamp,
- the optional `corrected_bbox` if you redrew the box,
- the optional `celebrity_id` if you tagged a known individual,
- a reference back to the original detection event (which has the
  bounding box, the saved frame, and the model's original guess).

Periodically the team exports those corrections, crops the
corresponding fish from the saved frames, and uses the result to
fine-tune the classifier. After a few hundred corrections per common
species, a new model checkpoint goes live, the dashboard's species
calls become more accurate, and your share of any future model card
will read "thanks to volunteer reviewers."

---

## Privacy

We don't track you. Your reviewer name is stored on the corrections
you submit (so we can credit you and run inter-rater agreement) but
we don't log views, IPs, or anything else. Your local browser
remembers your name and write token in localStorage; nothing else.

The cameras themselves are pointed at underwater reef and
pier-substructure — there's no PII collected from the visual feed.

---

## Questions

For technical questions email <dzimmerman2021@fau.edu>. For ecology
/ species-ID questions, ping the Wahoo Bay team via the program's
usual channels.
