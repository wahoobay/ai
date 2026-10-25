# Wahoo Bay on AWS — Services and Cost Plan

*Prepared 2026-09-25. Prices are AWS list prices for US East (N. Virginia), fetched on that date. System measurements were taken on the running system on 2026-09-25 unless a different date is given. All figures assume the optimizations in section 6.1 are completed before migration.*

## Summary

The Wahoo Bay system runs today on a shared FAU lab GPU server (the "DGX") and is reached through a temporary Cloudflare tunnel. Lab policy doesn't allow that server to host public services, so the whole system — both camera AI workers, the website, the annotation (review) tool, the database and the image archive — moves to AWS.

**Phase 1 (internal testing):** about 20 internal testers use the main site each week, and about 10 internal annotators start work in the review tool. Live video must play at the cameras' full speed and resolution for several viewers at once.

- **Recommended: about $826 a month on-demand, or $583 with 1-year commitments** (about $9,900 or $7,000 a year).
- **Leaner variant: about $585 on-demand ($427 committed),** if a quarter-GPU instance passes a one-day benchmark.
- **One-time costs** for testing, data upload and a two-week parallel run: about **$280–400**.

**Full-scale production** (50–100 concurrent viewers, 1,000 users a day) is about **$1,417 a month on-demand, or $1,090 committed**, and $2,004 or $1,473 with a warm-standby GPU host (section 10). That is below the grant proposal's $1,607 estimate, spent quite differently (section 11).

**Budget check:** every configuration stays within 15% of the grant's annual estimate ($19,284 a year, so a ceiling of $22,177), with one exception. That is full scale with a full warm-standby GPU paid on-demand, at $24,053 a year. A 1-year commitment ($17,676) or a quarter-GPU standby ($21,162) brings it back inside (section 11).

Five things drive the design:

1. **One modest GPU runs both cameras.** The two camera workers use about 4 GB of GPU memory together, a few percent of the DGX's GPU. Once the live video is passed through untouched rather than re-encoded, one L4 instance with 4 vCPUs carries both.
2. **Video is cheap to deliver if it is sent the right way.** CloudFront's flat-rate plans include 50 TB of transfer for $15 (Pro) or $200 (Business) a month. Today each viewer gets a separate, uncacheable MJPEG stream of about 12 Mbps. Passing the cameras' own H.264 video through as HLS instead cuts that to about 1.5–5 Mbps per viewer, and CloudFront shares it across viewers.
3. **The cameras, not AWS, set the video ceiling.** Today they deliver 1280×720 at about 15 frames per second. AWS can pass that on to any number of viewers at full quality. Getting 1080p at 30 fps needs changes on the camera side, which we will measure and raise with the City of Pompano Beach.
4. **Storage follows a tiered design.** New frames stay in fast storage for 14 days. They are then packed into ~1 GB chunks in an archive tier costing $4 per TB-month, from which single frames can still be read in milliseconds. Only the 2.8 TB of raw history moves; the annotated JPEG and COCO JSON copies (3.6 TB) are dropped, because nothing reads them and they can be rebuilt.
5. **The database is slimmed before it moves.** Incremental summaries replace a once-a-minute rebuild over all 695 million detection rows, and only 90 days of detections stay in PostgreSQL. That lets a modest managed database handle it (section 5).

## 1. What is being deployed

| Component | What it does | Needs a GPU? |
|---|---|---|
| Camera worker, Wahoo Bay (SEAHIVE) camera | Pulls the camera's video, detects and classifies fish on every frame, saves notable frames, writes detections to the database | Yes |
| Camera worker, Pompano Pier camera | Same, for the second camera | Yes |
| Dashboard | The public-facing site, the live video, reports, and the review (annotation) tool with its background task that queues boxes for review | No |
| Sensor poller | Water-quality and weather data every 5 minutes | No |
| PostgreSQL database | Detections, saved-frame records, sensor data, reviews and accounts | No |
| Frame archive | The saved camera frames (JPEG), used by the dashboard and the review tool | No |

**Usage assumptions for Phase 1:**

| Assumption | Value |
|---|---|
| Internal testers on the main site | ~20 per week |
| Annotators in the review tool | ~10 |
| Live-video viewing, light month | 500 viewer-hours |
| Live-video viewing, heavy month | 2,640 viewer-hours (30 viewers × 4 h × 22 days) |
| Frames read by annotators | ~110,000 per month (10 people × 500 a day × 22 days) |
| Cameras | 2, running 24/7 |

## 2. Recommended architecture

Everything runs in one AWS region (us-east-1) and one availability zone. For an internal testing phase that is the right trade-off: a zone outage takes the site down until it returns, but nothing is lost, because the database has automated backups and the frames live in S3.

```text
 Cameras (City of Pompano Beach, RTSP)             Viewers and annotators
        |  one pull per camera                            |
        v                                                 v
 +-------------------------------+        +----------------------------------+
 | GPU host  (EC2 g6.xlarge)     |        | CloudFront (flat-rate Pro)       |
 |  - MediaMTX restreamer        |<-------|  TLS, WAF, CDN, DNS              |
 |  - worker: Wahoo Bay camera   |  HLS   |  caches video + public API       |
 |  - worker: Pier camera        |        +----------------------------------+
 +-------------------------------+                        |
        |  detections        | raw frames                 | site, API, review tool
        v                    v                            v
 +-------------+    +-------------------------+    +---------------------------+
 | RDS         |    | S3 "landing"  (14 days) |    | Web host (EC2 t4g.medium) |
 | PostgreSQL  |<---|  nightly packer ->      |<---|  dashboard + review tool  |
 | m7g.large   |    | S3 Glacier Instant      |    |  sensor poller            |
 +-------------+    |  "archive" (~1 GB tars) |    +---------------------------+
                    +-------------------------+
```

| Service | Role | Why this choice |
|---|---|---|
| **EC2 g6.xlarge** (NVIDIA L4 24 GB, 4 vCPU, 16 GiB) with a fixed public IP | Both camera workers, plus a MediaMTX restreamer that pulls each camera once and passes its H.264 video through unchanged | Measured, the two workers need about 4 GB of GPU memory and about 6 GB of RAM. They use about 1–1.5 CPU cores while still JPEG-encoding every frame for viewers. That encoding goes away with the pass-through video, and the L4 decodes video in hardware (NVDEC), so 4 vCPUs are enough. The L4 also supports the bf16 and `torch.compile` path the classifier uses. The fixed IP is what the City allowlists on the camera port forwards. |
| **EC2 t4g.medium** with a fixed public IP | Dashboard, review tool, sensor poller | Annotation and the site keep working while the GPU host restarts. Needs ARM (arm64) container images; an x86 t3.medium costs about $6 a month more. |
| **RDS for PostgreSQL, db.m7g.large** (8 GB RAM), 200 GB gp3 with storage autoscaling, Single-AZ | Database | With incremental summaries and a 90-day window it holds ~100–130 GB. It takes ~25 new detection rows a second and answers dashboard queries from small summary tables (section 5). Automated backups are free up to the allocated storage. |
| **S3 Standard ("landing")** | Each new raw frame for its first 14 days | Serves the dashboard's recent-event images |
| **S3 Glacier Instant Retrieval ("archive")** | Raw frames packed into ~1 GB tar shards, each with an index of where every frame sits | Archive price ($0.004/GB-month) with millisecond reads; a ranged read fetches one frame from inside a shard (section 3) |
| **S3 Glacier Deep Archive** | Gzipped event logs (~24 GB); older detection months as Parquet files (queryable with Athena) | Cold data that is rarely or never read; cents a month |
| **CloudFront, flat-rate Pro plan** | HTTPS, web firewall (WAF), CDN for the site, video and public API responses, DNS for a `wahoobay.org` subdomain | $15 a month with 50 TB of transfer, 10M requests, a TLS certificate, WAF and a 50 GB S3 credit, and no overage charges |
| Secrets Manager, CloudWatch, Systems Manager, S3 gateway endpoint | Camera and database credentials; logs and alarms; shell access without an open SSH port; free private path to S3 | Standard low-cost operations |

**Deliberately left out:** a load balancer and a NAT gateway (together $50+ a month, and nothing at this scale needs them), Multi-AZ database standbys, and warm-standby GPU instances. Section 10 adds the redundancy a public production service should have.

**How the servers are protected:** only CloudFront can reach the two EC2 hosts. Their security groups allow the CloudFront origin-facing address list and nothing else. (CloudFront's private "VPC origins" need the $200 Business plan.) The database has no public address.

## 3. Storage: the tiered frame archive

The frame archive is the largest store: about 6.4 TB after four months, growing about 19 GB a day, with nothing ever deleted. Only the raw JPEGs (2.8 TB) are ever read by the site. The annotated copies (3.2 TB) and COCO JSON sidecars (0.4 TB) are not read by anything and can be rebuilt from the raw frames plus the database. So the plan moves only the raw frames and stops writing the other two, which cuts frame growth to about 8 GB a day.

The design is a tiered archive fed by a queue. New data flows to cheap long-term storage continuously, and annotation reads from it:

1. **Landing (S3 Standard).** Each worker writes each new raw frame as its own S3 object. The dashboard reads recent frames from here.
2. **Nightly packing.** A job rolls the previous day's frames into ~1 GB tar shards (about 5,000 frames each) in **S3 Glacier Instant Retrieval**. It records each frame's shard, byte offset and length in the database. After 14 days the landing copies expire automatically.
3. **Reading old frames.** When the review tool or dashboard needs an archived frame, it asks S3 for just that frame's byte range inside the shard. Glacier Instant Retrieval answers in milliseconds, with no restore step. A small cache on the web host keeps recently viewed frames.

Packing into ~1 GB shards matters because per-object fees would otherwise cost more than the storage. At 47 million small files, lifecycle transitions, per-object archive overhead and "intelligent tiering" monitoring fees add up; at 1 GB per shard they are negligible.

*A colder alternative.* Glacier Flexible Retrieval or Deep Archive cost less per TB but take hours to restore. With those, the review tool would work in batches: restore a 1 GB chunk into fast storage, annotate it, and let the copy expire. At today's volume that saves under $10 a month over Glacier Instant Retrieval, which doesn't justify the extra machinery. It becomes worth revisiting if the archive grows past about 50 TB.

| Store | Size (month 1) | Monthly cost |
|---|---:|---:|
| Landing, S3 Standard (14 days × 8.2 GB) | 115 GB | $2.65 |
| Landing uploads (~1.34M frames a month) | — | $6.69 |
| Archive, Glacier Instant Retrieval | 2.8 TB (+0.25 TB a month) | $11.20 (≈ $22 by month 12) |
| Archive reads for annotation (~110,000 frames) | ~22 GB | $1.76 |
| Cold: gzipped logs + old detection months (Parquet) | ~85 GB | $0.26 |
| **Total** | | **≈ $22.56** |

## 4. Live video at full speed and resolution

**Today:** each camera sends H.264 video at 1280×720 and about 15 fps (about 1.1–1.2 Mbps on average). The worker decodes every frame, draws the detection boxes into it, and re-encodes it as a JPEG. Each viewer then gets their own MJPEG stream at roughly **12 Mbps**, through a separate chain of connections back to the worker. No CDN can cache or share that stream, and the worker's server tops out at around 32 simultaneous viewers per camera.

**On AWS:**

- A **MediaMTX restreamer** on the GPU host pulls each camera once. The camera link is the scarce resource, so the workers read from the restreamer rather than from the camera. They decode in hardware on the L4.
- The restreamer publishes each camera's own H.264 video as **HLS** (2-second segments) without re-encoding it. CloudFront caches the segments, so ten viewers or a hundred cost the origin the same.
- Each viewer receives exactly what the camera sends: about **1.5 Mbps at 720p/15 fps**, or about **5 Mbps at 1080p/30 fps** if the cameras are upgraded.
- **Detection boxes are drawn by the browser**, on a transparent layer over the video. They are fed by a WebSocket from the worker, which already publishes boxes with timestamps. The same WebSocket carries the live status the page now polls for twice a second.
- Expected delay behind real time: about 4–8 seconds with standard HLS. Low-latency HLS or WebRTC can bring that under 2 seconds, at the cost of many more CDN requests or paying normal egress rates.

**The camera limit.** Full resolution is 1920×1080. Pulling that over RTSP from the DGX managed only **3.7 fps**, and 1280×720 at 30 fps managed 7.1 fps. Yet the Wahoo Bay camera already broadcasts 1080p at 30 fps (about 4.6 Mbps) to YouTube, so the camera can produce it. The bottleneck is somewhere between the camera and the puller: the camera's encoder load, a bitrate cap, or the network path. Phase 0 (section 8) re-runs the test from AWS and asks the City's IT contact about the camera uplink and bitrate settings. AWS itself adds no limit.

**What video delivery costs.** Pay-as-you-go CloudFront charges $0.085 per GB, but the flat-rate Pro plan has 50 TB included for $15:

| Stream type | Light month (500 viewer-hours) | Heavy month (2,640 viewer-hours) | Pay-as-you-go, heavy | Flat-rate Pro |
|---|---:|---:|---:|---:|
| Current MJPEG (~12 Mbps) | 2.7 TB | 14.3 TB | ~$1,190 | $15, but every viewer still loads the worker |
| HLS 720p/15 fps (~1.5 Mbps) | 0.34 TB | 1.8 TB | ~$151 | $15 |
| HLS 1080p/30 fps (~5 Mbps) | 1.1 TB | 5.9 TB | ~$505 | $15 |

**The request allowance matters as much as the data allowance.** The Pro plan includes 10 million requests a month. HLS uses about one request per second per viewer: about 1.8M in a light month and 9.5M in a heavy one. Today's half-second status polling would add 7.2M–38M more; carrying that on the WebSocket instead keeps Phase 1 on the $15 plan.

## 5. Database

The database is 307 GB today. Nearly all of it is one table of detections: 695 million rows, 273 GB, growing by about 2–3 million rows (about 30 GB) a month. On the DGX, a summary of sightings per species is rebuilt from every row once a minute. That works only because the DGX keeps the whole table in its ~1.9 TB of memory.

Three changes, made before migration (section 8), let a modest managed database handle it:

- **Incremental summaries.** Each minute, only the new detections are added to hourly roll-up tables. The full rebuild disappears.
- **A 90-day hot window.** The detections table is split by month, and only the last 90 days stay in PostgreSQL. Older months are exported to Parquet files in S3, where they stay queryable with Athena. The summaries keep their full history.
- **Cached public responses.** Public API responses that every viewer sees alike, such as species counts, sensor readings and statistics, are cached at CloudFront for 5–30 seconds. A hundred viewers asking the same question cost one database query.

The result:
- **Size:** about 100–130 GB of data.
- **Load:** about 25 new detection rows a second, with dashboard queries served from small summary tables.
- **Phase 1 instance:** a db.m7g.large (2 vCPU, 8 GB) with 200 GB of gp3 storage is enough. That storage includes 3,000 IOPS, and autoscaling grows it if needed.
- **Full scale:** a Multi-AZ standby, and the 16 GB db.r7g.large for headroom (section 10).
- **Version:** production runs PostgreSQL 18.3. Confirm that RDS offers version 18; if not, move to 17.

## 6. Monthly cost

### 6.1 Optimizations assumed

These figures assume the following work is finished before migration (details in section 8):

1. **Database:** incremental summaries, a 90-day hot window and Parquet export (section 5).
2. **Live video passed through as HLS,** with boxes drawn in the browser (section 4). The GPU host no longer re-encodes every frame as JPEG for viewers.
3. **Hardware video decoding** (NVDEC on the L4) and pinned CPU thread counts in the camera workers.
4. **Raw frames only:** annotated JPEG and COCO copies are no longer written, and frames are archived in ~1 GB shards (section 3).
5. **Live status and boxes pushed over a WebSocket** instead of polled twice a second.
6. **Public API responses cached** at CloudFront for a few seconds.

Further savings are available but **not** assumed, because they change what data is collected or how flexible the budget is:

- Saving JPEGs at quality 85 instead of 92 makes each frame about a third smaller.
- Saving fewer frames per fish track; today the minimum gap is 1 second.
- A 30-day hot window instead of 90 days.
- 3-year commitments give larger discounts but lock in the instance choice.

### 6.2 Recommended configuration (Phase 1)

| Line | On-demand | With 1-year commitments |
|---|---:|---:|
| GPU host, EC2 g6.xlarge | $587.65 | $382.52 |
| Web host, EC2 t4g.medium | $24.82 | $15.33 |
| Database, RDS db.m7g.large | $122.64 | $94.90 |
| Database storage, gp3 200 GB | $23.00 | $23.00 |
| EC2 disks, gp3 130 GB, plus snapshots | $12.40 | $12.40 |
| Two fixed public IPv4 addresses | $7.30 | $7.30 |
| S3 frame storage and requests (section 3) | $22.56 | $22.56 |
| CloudFront flat-rate Pro | $15.00 | $15.00 |
| CloudWatch, Secrets Manager, incidental egress | $10.40 | $10.40 |
| **Monthly total** | **≈ $826** | **≈ $583** |
| **Annual total** | **≈ $9,900** | **≈ $7,000** |

The 1-year rates are AWS's listed no-upfront reserved rates. A Compute Savings Plan gives a similar discount and can move between instance types if the benchmark changes the choice.

Month-by-month growth is small. The archive adds about $1 a month (about $22 by month 12), and the database stays near a fixed size because of the 90-day window.

### 6.3 Other GPU options

| Instance | GPU | vCPU / RAM | On-demand per month | Notes |
|---|---|---|---:|---|
| **g6.xlarge** (recommended) | L4, 24 GB | 4 / 16 GiB | $587.65 | Fits once live video is passed through and decoding is on the GPU |
| g6.2xlarge | L4, 24 GB | 8 / 32 GiB | $713.94 | Fallback if the Phase 0 benchmark shows 4 vCPUs aren't enough (+$126 a month) |
| g6f.2xlarge | ¼ of an L4, 6 GB | 8 / 32 GiB | $346.75 | Cheapest option that keeps the L4. Unproven for two live cameras, and needs NVIDIA's GRID driver |
| g4dn.xlarge | T4, 16 GB | 4 / 16 GiB | $383.98 | Older GPU; needs the classifier switched to fp16 and re-validated |
| g5.xlarge | A10G, 24 GB | 4 / 16 GiB | $734.38 | The grant's choice; costs more than an L4 for this workload |

**Budget configuration:** g6f.2xlarge in place of g6.xlarge, with everything else as in 6.2. That is **about $585 a month on-demand, or about $427 with 1-year commitments** (the g6f 1-year rate is estimated from g6.xlarge's 34.9% listed discount). It depends on the Phase 0 benchmark showing that ¼ of an L4 keeps up with both cameras.

**Spot instances** (up to 60–70% cheaper) are not recommended for the camera workers. AWS can reclaim them at two minutes' notice, which drops live detection for the whole reclaim period. They are well suited to occasional model retraining.

### 6.4 FAU account notes

Ask FAU's Office of Information Technology to confirm:

- whether FAU's AWS agreement includes the **Data Egress Waiver** for research institutions (egress fees waived up to 15% of the monthly bill; this plan already keeps egress low);
- whether **CloudFront flat-rate plans** are available under FAU's billing arrangement (some reseller agreements differ);
- the account structure, SSO and tagging rules FAU requires.

## 7. One-time costs

| Item | Cost |
|---|---:|
| Phase 0 measurements: an 8-hour benchmark on each of three GPU types, plus a small instance testing the cameras for 3 days | ≈ $18 |
| Uploading 2.8 TB of raw frames as ~2,800 shards (inbound transfer is free) | < $1 |
| Two weeks running AWS in parallel with the DGX before cutover | ≈ $260–380 |
| **Total** | **≈ $280–400** |

The upload takes about half a day at 500 Mbps. The shards are built on the DGX first, so AWS receives a few thousand large files rather than millions of small ones.

## 8. Timeline and work required

### Phase 0 — measure and set up (about 1–2 weeks)

- FAU OIT creates the account (section 6.4).
- Run a small instance for a few days to test pulling the cameras at 720p and 1080p from AWS.
- Benchmark both cameras' workers on g6f.2xlarge, g6.xlarge and g6.2xlarge for a day each, and choose the instance.
- Ask the City of Pompano Beach IT contact to allowlist the fixed AWS address on the camera port forwards, and about the camera uplink and bitrate caps (for 1080p/30 fps).

### Phase 1 — changes to the system (no AWS cost; assumed complete in all figures)

1. **Database:** incremental summaries; monthly partitions with a 90-day hot window; export of older months to Parquet. Do this first: it also speeds up the dashboard on the DGX.
2. **Live video:** the MediaMTX restreamer; an HLS player in the dashboard (the hls.js library); boxes and live status over a WebSocket; hardware decoding and pinned thread counts in the workers.
3. **Frames to S3:** workers write raw frames to S3 and stop writing the annotated and COCO copies; the nightly shard packer; ranged reads in the dashboard and review tool; rewriting the 15.6 million stored frame paths.
4. **Caching:** short CloudFront cache lifetimes on public API responses.
5. **Deployment:**
   - containers for both cameras and the restreamer, with model weights fetched at build time;
   - ARM images for the web host;
   - the real client IP passed through CloudFront, so login rate limits work;
   - a permanent site address, which also fixes the email links;
   - credentials in Secrets Manager;
   - every service reachable only through CloudFront.

### Phase 2 — migrate and cut over (about 2 weeks)

1. Pack and upload the raw frame history. Gzip the event logs and move them to Deep Archive.
2. Load the last 90 days of detections into RDS, and export older months to Parquet.
3. Run AWS alongside the DGX for about two weeks, comparing detections and dashboard numbers.
4. Point the permanent address at CloudFront, then shut down the DGX services and the Cloudflare tunnel.

## 9. Risks and open questions

| Risk | Effect | Mitigation |
|---|---|---|
| Camera uplink can't carry 1080p/30 fps | Viewers get 720p/15 fps, the same as today | Phase 0 test; City IT; the camera's YouTube feed as a fallback source |
| City IT won't allowlist an AWS address | Workers can't pull the cameras | Raise it in Phase 0, before anything else |
| Phase 1 work runs long | Migration waits; these figures assume it is done | Start with the database changes; they pay off on the DGX too |
| 4 vCPUs not enough for both cameras | Detections drop frames | Phase 0 benchmark; step up to g6.2xlarge (+$126 a month) |
| Heavier use than planned | Requests exceed the Pro plan's 10M | The $200 Business tier (section 10) |
| Single availability zone | A zone outage takes the site down for its duration | Acceptable for internal testing; production adds standbys (section 10) |

## 10. Full-scale production: 50–100 concurrent viewers, 1,000 users a day

Most of the bill doesn't grow with viewers. The GPU host's size is set by the two cameras, and the frame archive by what the cameras capture. With HLS through CloudFront, the video servers send each segment once whether 5 or 100 people are watching, and cached public API responses keep database load nearly flat. What grows is the CDN plan, the web tier, and the redundancy a public service should have.

**Usage assumptions:** 1,000 users a day, with 50–100 watching at peak. That is 7,500 viewer-hours a month (15 minutes of video per visit) to 18,000 (an average of 50 viewers across 12 hours a day). Add about 150 page and API requests per visit.

| Scenario | Video data a month | Requests a month | Pay-as-you-go CloudFront | Flat-rate |
|---|---:|---:|---:|---:|
| 7,500 viewer-hours, 720p/15 fps | 5.1 TB | 31.5M | ≈ $462 | Business, $200 |
| 7,500 viewer-hours, 1080p/30 fps | 16.9 TB | 31.5M | ≈ $1,432 | Business, $200 |
| 18,000 viewer-hours, 720p/15 fps | 12.2 TB | 69.3M | ≈ $1,091 | Business, $200 |
| 18,000 viewer-hours, 1080p/30 fps | 40.5 TB | 69.3M | ≈ $3,359 | Business, $200 |

All four scenarios fit the Business plan's 50 TB and 125M requests. They exceed the Pro plan's 10M requests. Business also allows CloudFront to reach servers in private subnets ("VPC origins").

**Production configuration:**

- The same GPU host as Phase 1.
- Two web hosts behind a load balancer, so one can fail or be updated without an outage.
- A Multi-AZ database: a synchronous standby in a second zone, with automatic failover in 1–2 minutes.
- CloudFront Business.

| Line | On-demand | With 1-year commitments |
|---|---:|---:|
| GPU host, g6.xlarge (unchanged; set by the cameras) | $587.65 | $382.52 |
| Web hosts, 2 × m7g.large | $119.72 | $78.84 |
| Application Load Balancer | $28.11 | $28.11 |
| Database, RDS db.r7g.large Multi-AZ | $348.94 | $268.64 |
| Database storage, 200 GB Multi-AZ | $46.00 | $46.00 |
| EC2 disks and snapshots | $15.80 | $15.80 |
| Three fixed public IPv4 addresses | $10.95 | $10.95 |
| S3 (month 12, with twice the annotation reads) | $35.20 | $35.20 |
| CloudFront flat-rate Business | $200.00 | $200.00 |
| CloudWatch, Secrets Manager, health checks | $24.40 | $24.40 |
| **Monthly total** | **≈ $1,417** | **≈ $1,090** |
| **Annual total** | **≈ $17,000** | **≈ $13,100** |
| *With a warm-standby GPU host (as in the grant design)* | *≈ $2,004* | *≈ $1,473* |

**Is a warm standby GPU worth it?** Without one, a failure of the GPU host stops detection until EC2 auto-recovery replaces it, typically 5–15 minutes. The live video stops too, since the restreamer runs on the same host. A standby cuts that to about a minute, for about $383–588 a month. For a public service that records data for research, the standby is the main open choice; the rest of the production configuration is needed either way.

**Before launch, load-test** the web tier and database at 100 concurrent users. The summary tables and caching should make dashboard queries cheap, but that should be measured, not assumed. The next steps up are db.r7g.xlarge (about +$350 a month Multi-AZ) and larger web hosts.

## 11. What changed since the grant estimate

`docs/grant_writeup.md` (§6 and §8, May 2026) estimated **$1,607 a month**. That was a fully redundant full-scale design, priced with 3-year all-upfront reservations (roughly 64% off on-demand GPU prices). Several of its assumptions were estimates made before the system had run for months. The measurements now show:

| | Grant estimate | This plan, Phase 1 | This plan, full scale |
|---|---|---|---|
| Pricing basis | 3-year all-upfront reservations | On-demand / 1-year | On-demand / 1-year |
| GPU | 4 × g5.xlarge (one active and one standby per camera) | 1 × g6.xlarge for both cameras | 1 × g6.xlarge (+1 optional standby) |
| Web tier | 2 × t4g.medium + load balancer | 1 × t4g.medium | 2 × m7g.large + load balancer |
| Database | db.t4g.medium (4 GB) Multi-AZ, 100 GB | db.m7g.large (8 GB), 200 GB | db.r7g.large (16 GB) Multi-AZ, 200 GB |
| Frame volume | 2–4 GB a day, 3.3 TB after 3 years | 8.2 GB a day of raw frames (measured), ~11.8 TB after 3 years | same |
| Frame storage | S3 plus EFS ($87) | S3 landing + archive shards ($23) | $35 in year 1 |
| Viewer video | MJPEG at "2–4 Mbps", assumed cached by CloudFront ($300) | HLS pass-through, flat-rate Pro ($15) | flat-rate Business ($200) |
| NAT gateway | $10 | none | none |
| **Monthly total** | **$1,607** | **$826 / $583** | **$1,417 / $1,090** (with standby: $2,004 / $1,473) |

**What changed and why:**

1. **One GPU runs both cameras.** The grant assumed one GPU per camera plus a standby for each, 4 in all. Measured, each camera's worker uses about 2 GB of GPU memory and together they use 3–5% of an H200, so one L4 carries both. This is the biggest saving, and it holds at full scale.
2. **Video delivery would have cost far more than budgeted.** Each MJPEG viewer actually gets about 12 Mbps, not 2–4. CloudFront can't cache or share an MJPEG stream, so the grant's $300 a month would have been roughly $1,200 at Phase 1's heavy use and $3,300–6,900 at full scale (40–97 TB a month). HLS pass-through plus a flat-rate plan brings it to $15–200.
3. **There is 3–5 times more data than assumed, and storage costs about the same.** Frames are growing at 19 GB a day, not 2–4. The database is 307 GB, not 100. Dropping the unused copies, the shard archive, removing EFS and the database's 90-day window offset the difference.
4. **The database still needs more than the grant allowed.** Even after the summary and hot-window work, it holds 100–130 GB and needs 8–16 GB of memory. The grant's 4 GB, 100 GB instance is too small.
5. **The grant's NAT line was too low.** A NAT gateway costs about $33 a month before data. Routing 1.3 TB of camera video through it would add about $58 more. This plan gives the GPU host a fixed public address instead, which the City needs for its allowlist anyway.
6. **The pricing basis is different.** The grant's $1,607 assumes paying 3 years of reservations upfront (about $12,200). This plan quotes on-demand and 1-year no-upfront rates, which suit a testing phase. At 3-year terms the full-scale figures here would fall further.

**Net effect:** full-scale production costs less than the grant estimated: $1,090–1,417 a month against $1,607, or $1,473–2,004 with a warm-standby GPU. The money goes to different places: much less GPU, more database and CDN. The grant's own 3-year total, with only its storage and network lines corrected, falls from about $79,000 to about $75,000. Re-basing its compute on one GPU host would lower it further.

**Budget check: within 15% of the grant's annual estimate.** The grant's steady-state estimate is $1,607 a month, or **$19,284 a year**, so the ceiling is **$22,177 a year**. That matches the grant's own "annual budget with 15% buffer" of $22,176. Recurring costs only; one-time costs are in section 7.

| Configuration | Annual, on-demand | % of grant | Annual, 1-year commitments | % of grant |
|---|---:|---:|---:|---:|
| Phase 1 (recommended) | $9,909 | 51% | $7,001 | 36% |
| Phase 1 (budget, g6f.2xlarge) | $7,018 | 36% | $5,119 | 27% |
| Full scale | $17,001 | 88% | $13,086 | 68% |
| Full scale + quarter-GPU standby (g6f.2xlarge) | $21,162 | 110% | $15,794 | 82% |
| Full scale + full standby (g6.xlarge) | **$24,053** | **125%** | $17,676 | 92% |

Every configuration is inside the ceiling except one: full scale with a full standby GPU, paid on-demand. Three ways keep it inside:
- Commit for a year: $17,676, 92% of the grant.
- Use a quarter-GPU standby: $21,162 on-demand. It keeps detection running while the main host recovers, possibly at a reduced frame rate; this is untested until the Phase 0 benchmark.
- Keep a "pilot-light" standby: a stopped instance that is started only on failure. It costs only its disk, with a recovery time of a few minutes.

## 12. Price sources (fetched 2026-09-25, us-east-1)

| Item | Price |
|---|---|
| EC2 g6.xlarge / g6.2xlarge / g6f.2xlarge / g4dn.xlarge / g5.xlarge | $0.805 / $0.978 / $0.475 / $0.526 / $1.006 per hour (on-demand) |
| EC2 1-year no-upfront: g6.xlarge / g4dn.xlarge / g5.xlarge / t4g.medium / m7g.large | $0.524 / $0.331 / $0.634 / $0.021 / $0.054 per hour |
| EC2 t4g.medium / m7g.large | $0.034 / $0.082 per hour |
| Application Load Balancer | $0.0225 per hour + $0.008 per LCU-hour |
| RDS PostgreSQL db.m7g.large (1-year) | $0.168 ($0.130) per hour, Single-AZ |
| RDS PostgreSQL db.r7g.large (1-year) | $0.239 ($0.184) per hour, Single-AZ |
| RDS Multi-AZ | twice the Single-AZ instance and storage rates |
| RDS gp3 storage / EBS gp3 | $0.115 / $0.08 per GB-month |
| Public IPv4 address | $0.005 per hour |
| S3 Standard / Glacier Instant Retrieval / Deep Archive | $0.023 / $0.004 / $0.00099 per GB-month |
| S3 PUT (Standard / Glacier Instant) | $0.005 / $0.02 per 1,000 |
| Glacier Instant Retrieval reads | $0.03 per GB retrieved, $0.01 per 1,000 GETs |
| CloudFront pay-as-you-go (North America) | $0.085 per GB for the first 10 TB, then $0.080 |
| CloudFront flat-rate plans | Free $0, Pro $15, Business $200, Premium $1,000 per month. Pro includes 10M requests, 50 TB transfer and a 50 GB S3 credit; Business includes 125M requests, 50 TB and a 1 TB S3 credit |

Sources: instances.vantage.sh (EC2 and RDS rates), aws.amazon.com/s3/pricing, the Amazon CloudFront Developer Guide ("CloudFront flat-rate pricing plans"), and the AWS VPC, EBS, RDS and Elastic Load Balancing pricing pages. Check them again before committing to reserved capacity.

## Appendix: measured system, 2026-09-25

| Item | Measured value |
|---|---|
| Database size | 307 GB; detections table 273 GB, 695M rows |
| Detection growth | about 1.9–2.8M rows a day (7- and 30-day rates); falling month by month since June |
| Saved frames | ~6.4 TB (raw 2.8, annotated 3.2, COCO 0.4); 15.6M saves; 18.8 GB a day (Sep 18–24) |
| Event logs | 338 GB of JSONL (about 24 GB gzipped) |
| GPU memory | about 2 GB per camera worker |
| GPU use | 3–5% of an H200 |
| Worker CPU | about 1–1.5 cores for both cameras, sampled over 60 s |
| Worker RAM | about 3 GB unique memory per worker |
| Camera streams | H.264 1280×720 at ~15 fps; 1.23 and 1.06 Mbps average (Sep 18 measurement) |
| Inference rate | 13.7 fps (Wahoo Bay) and 15.1 fps (Pier) |
| Viewer stream today | MJPEG, 1280×720, JPEG quality 92, 78–128 KB per frame (~12 Mbps at 15 fps) |
| Summary rebuild | every 60 s; 47–144 s per rebuild on the DGX |
