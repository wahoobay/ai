# Talking points: Katie and Rob Wyre, 30 Sept 2026

**One-line pitch:** Wahoo Bay's underwater cameras now run AI that spots and identifies fish around the clock. Trained volunteers check its work, and every record can be traced back to its source.

---

## 1. What people see

- **Live cameras with AI fish ID.** The SEAHIVE reef cam and the pier cam stream live. The AI boxes and names each fish as it swims by.
- **A living record of the reef:**
  - which species show up, and when;
  - trends over days and weeks;
  - species richness over time;
  - which species use which habitat (reef vs pier);
  - a profile page per species.
- **Named "celebrity" fish** that regulars recognise, with their own sighting history.
- **Environment next to the fish:**
  - Wahoo Bay's water sensors (temperature, oxygen, pH, turbidity) plus local weather;
  - charts that compare them with fish activity, e.g. rainfall or water temperature against fish counts.
- **Open data.** Anyone can download it. Volunteers' names are never published. The site explains its data sources, definitions and known limitations.
- **Volunteer review site.** Trained volunteers sign in and:
  - confirm or correct the AI's species guess;
  - identify fish the AI didn't know;
  - draw boxes around fish it missed;
  - note juvenile vs adult and named individuals.
- **Easy to run:**
  - volunteers sign up themselves and an admin approves them;
  - password resets arrive by email;
  - the admin page shows everyone at a glance;
  - the site comes in Wahoo Bay colours, with light and dark modes.

## 2. Why the data can be trusted

- **Provenance on every record.** Every sighting keeps:
  - its original camera frame, the time and the camera;
  - exactly which version of the AI identified it.

  Every volunteer answer records who gave it, when, and whether they took the AI's suggestion or chose the species themselves.
- **Built-in quality control:**
  - about 1 in 10 answers is re-checked "blind" by a second volunteer who can't see the first answer;
  - disagreements go to an admin to settle;
  - the system measures how often volunteers agree, the standard way to report labelling quality.
- **Scientifically correct names.** Species names are checked against WoRMS, the World Register of Marine Species. Wahoo Bay's own Species Log sheet syncs in automatically.
- **Private where it should be.** Volunteer identities and unfinished re-checks never appear in public views or downloads.
- **Backed up nightly** to two separate disks, so volunteers' work can't be lost.

## 3. How the AI gets smarter

- **Two steps:** the AI first finds each fish, then names the species.
- **Volunteers' time goes where it helps most.** The system queues the species the AI has the fewest confirmed examples of, working toward **200 verified examples per species**.
- **The scale is already large:** about **700 million** fish detections so far, with **2–3 million more each day**.
- **Next (planned, not built yet):**
  - retrain the AI on the verified examples and measure the improvement;
  - publish the dataset in standard biodiversity formats (Darwin Core, which feeds GBIF and OBIS).

## 4. What's next

- **Move to AWS.** It's costed under FAU's account and within the grant's budget. It brings a permanent web address and full-quality video for many viewers at once. Today the site runs on an FAU lab machine through a temporary link.
- **In-depth walkthrough** with Katie and the operations team.
- **Volunteer recruitment.** The review site is ready for more reviewers now.

---

## Numbers to have handy

| Figure | Value | Source |
|---|---|---|
| Cameras live | 2 (SEAHIVE reef, pier) | live system |
| Fish detections recorded | about 700 million | database check, 25 Sept 2026 |
| New detections per day | 2–3 million | same check |
| Species in the review catalogue | 240 names | species list, WoRMS-checked |
| Species Log (Wahoo Bay's sheet) | 235 species | synced sheet |
| Blind re-check rate | about 1 in 10 answers | setting, adjustable |
| Target per species | 200 verified examples | setting, adjustable |
| Backups | nightly, two disks, kept 30 days | backup job |

## If they ask

- **"How accurate is the AI?"** Volunteers are building the measurement now. The blind re-checks and agreement scores give the first solid numbers. Then retraining shows how much accuracy improves.
- **"What does hosting cost?"** The AWS plan is costed within the grant's annual budget. Full detail is in `docs/aws_cost_plan.md`.
- **"Is volunteer data private?"** Yes. Names, notes and admin actions are admin-only. Public views and downloads carry the labels, not the people.
- **"Can we use this for outreach or education?"** Yes. The live cameras, named fish and species pages are public-facing today.
- **"What do you need from us?"** Volunteers for the review site, and the operations-team walkthrough.

## If showing it live

1. **Dashboard home:** the live cameras with fish being named, then the species charts.
2. **A species detail page,** then the named-fish list.
3. **The review site (`/review`):** answer one fish, then show the whole-frame view.
4. **The admin accounts page:** approving a volunteer.

The site address is the temporary tunnel link. Check it's current before the meeting.
