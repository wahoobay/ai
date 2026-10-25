# AI Processing of Survey Video: Cost Scenarios

*Prepared by Dan Zimmerman (FAU) for Paul Arena (NSU ECOTech Lab) and Wahoo Bay, 2 October 2026, for the 2027 Koons-Ayala pilot. AWS list prices (US East) as of 25 September 2026, to be rechecked before work starts. Speeds measured on our production models on 2 October 2026.*

## Summary

- **The cloud cost of testing our AI on your footage is about $55 for a pilot, $130 for a representative third, and $400–700 for the whole dataset.** Each fits well within the $3,475 AI/data line and leaves most of it for your other software and storage needs.
- **These are cloud costs only.** They don't include anyone's time for setup, running and reporting.
- **We recommend starting with the pilot.** It shows how the AI compares with your expert-reviewed identifications before you commit to a larger run.

## What the processing produces

For each video:

- every fish the AI detects, frame by frame, with its time, position, suggested species and confidence (CSV);
- a fish-count timeline, so analysts can skip empty stretches and go straight to the busiest moments;
- for each suggested species, the frames with the most individuals (MaxN candidates), with the fish boxed, so a person can confirm them quickly.

Not included: stereo length measurement (that needs software such as EventMeasure).

## Scenarios

| Scenario | Videos: stereo / BRUV / diver | Hours | Data | GPU | Storage | Total |
|---|---|---|---|---|---|---|
| **A. Pilot** | 3 / 3 / 6 | 7.5 | 0.3 TB | $34 | $21 | **about $55** |
| **B. A third** | 10 / 10 / 20 | 25 | 1.0 TB | $60 | $69 | **about $130** |
| **C. All, one camera** | 30 / 30 / 60 | 75 | 3.0 TB | $181 | $207 | **about $390** |
| **D. All, both cameras** | 30 / 30 / 60 | 105–135 | 4.2–5.4 TB | $254–326 | $290–373 | **about $540–700** |
| **E. Busiest 20%** | 30 / 30 / 60 | 75 + 15 | 3.0 TB | $42 | $207 | **about $250** |

*Hours: video hours processed. Data: video sent to us. Storage: a working copy kept for three months. The pilot's GPU cost includes $16 of test runs on your files. D: 105 hours if the BRUVs are single-camera, 135 if stereo. E: every video screened at one frame a second, then the busiest 20% (15 hours) analysed in full.*

- **One camera per stereo pair is enough for counting**; the second matters only for length measurement. That is why C processes 75 hours, not 105.
- **Who picks the subset.** In A–C you choose the videos (ideally ones your experts reviewed) and send only those. In E we pick, which needs the full dataset, a selection step we would build, and an agreed definition of "meaningful". Analysing only the busiest footage also skews comparisons between deployments; a fixed window from each one avoids that.

## Testing against your expert identifications

Our AI was trained on Wahoo Bay's fixed nearshore reef cameras and on photos of 223 South Florida species. Your offshore footage is from the same region, so many species should overlap. It differs in depth, habitat and camera: bait and bait arms in view, and moving diver cameras.

- **Finding and counting fish** should carry over better than naming species.
- **Species identification on footage the model has never seen is not reliable yet.** We expect the test to show this, and where it falls short.

Your expert-reviewed identifications make a two-step study possible:

1. **Baseline.** Run the current AI on the pilot videos and compare it with your experts: detections, counts and species accuracy (precision, recall, F1 and a confusion matrix).
2. **Retrain and re-test.** Retrain the species model on part of your expert-labelled footage, then measure again on videos held out from training. GPU time for retraining is under $10 a run.


## How the figures were worked out

- **Processing speed.** On one of our lab GPUs (NVIDIA H200), the full pipeline ran at 88–128 frames a second on 720p reef video, and about 30 on a crowded 4K frame with 25 fish (estimated from component timings). For costing we assume a much slower **10 frames a second** on an AWS g6.xlarge (NVIDIA L4, $0.805 an hour), so real costs should come in lower. A–D process every frame, at 30 frames a second.
- **Storage:** 40 GB per camera-hour (typical of 4K action cameras; 1080p is smaller), kept in S3 Standard ($0.023 per GB-month) for three months, then deleted. You keep the masters.
- **Turnaround:** C takes about 9 days on one cloud GPU, or 3 days on three at the same cost. Sending 3 TB takes about 3 days at 100 Mbps; a shipped drive is simpler.

## Questions before we finalise

1. **Cameras:** model, resolution, frame rate and file format. Are the BRUVs stereo too?
2. **Sites:** depth and habitat, and a draft species list if you have one.
3. **Outputs:** MaxN per species, time of first arrival, total counts, or anything else.
4. **Expert identifications:** what form they take (for example MaxN sheets with timestamps, or boxes drawn on frames), and how many videos will have them.
5. **Data and billing:** how you'd send the footage, whether we may keep it and your identifications to retrain our model, and who pays the cloud bill.
