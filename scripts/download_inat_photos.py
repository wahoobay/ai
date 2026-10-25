"""Bulk-pull research-grade iNaturalist photos for every species in the
manifest — independent observations of the same species, the missing
ingredient for cross-individual evaluation in train_baseline.py.

Reads:
  * ``assets/species_manifest.json`` (built by build_species_manifest.py)

Writes:
  * ``assets/inat_photos/<binomial_slug>/<photo_id>.jpg`` — one file per
    iNat photo, named by its iNat photo_id so re-runs are idempotent.
  * ``assets/inat_manifest.json`` — one record per downloaded photo with
    the iNat provenance the volunteer dataset needs to live alongside:
    photo_id, observation_id, binomial, observer, license code,
    attribution string (already in iNat's official "(c) X, CC ..." form),
    original URL. Feeds directly into the cropper as a second source of
    crops on top of ``assets/community_clips/`` files.

License policy: anything that is NOT All-Rights-Reserved is accepted, per
the user's choice. ARR-licensed photos have ``license_code = null`` on
the iNat API and are excluded server-side via the ``photo_license`` query
param. Each downloaded photo keeps its license + attribution in the
manifest so a future dataset card can credit observers correctly.

API etiquette: iNat asks for ≤ 60 requests/minute and a single
concurrent connection. We hit the API once per species (with the
``per_species`` results worth of photos in one call) and pace at 1
request/second. Image downloads come from a separate S3 host
(inaturalist-open-data.s3.amazonaws.com) so we can parallelise those.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

try:
    import httpx
except ImportError as e:
    sys.exit(f"httpx is required: {e}")


REPO_ROOT = Path(__file__).resolve().parent.parent
SPECIES_MANIFEST = REPO_ROOT / "assets" / "species_manifest.json"
OUT_DIR = REPO_ROOT / "assets" / "inat_photos"
OUT_MANIFEST = REPO_ROOT / "assets" / "inat_manifest.json"

INAT_API = "https://api.inaturalist.org/v1/observations"
# Anything except ARR. iNat exposes these license codes through the
# photo_license query param; passing them as a comma-separated list
# limits results to photos with that license, filtering out null
# (= All Rights Reserved) at the server.
PERMISSIVE_LICENSES = [
    "cc0",
    "cc-by",
    "cc-by-nc",
    "cc-by-sa",
    "cc-by-nc-sa",
    "cc-by-nd",
    "cc-by-nc-nd",
]
USER_AGENT = "wahoobay-dataset-pull (scoopnisker@gmail.com)"

API_RATE_LIMIT_SECONDS = 1.05  # iNat asks for ≤ 60 req/min on the API
DOWNLOAD_CONCURRENCY = 16  # S3 bucket can handle plenty in parallel
PHOTO_SIZE = "medium"  # ~500px max edge — good for ML crops


@dataclass
class InatRecord:
    source_id: str  # "inat_<photo_id>" — global unique key
    source_kind: str  # always "inat"
    photo_id: int
    observation_id: int
    binomial: str
    common_name: str | None
    family: str | None
    genus: str | None
    species: str | None
    observer: str | None
    license: str | None  # cc-by, cc-by-nc, cc0, ...
    attribution: str  # iNat's pre-formatted "(c) X, CC..." string
    observed_on: str | None
    original_url: str  # link back to the iNat photo page
    local_path: str  # relative to repo root


def slugify(s: str) -> str:
    s = re.sub(r"[^\w\s-]", "", s).strip()
    s = re.sub(r"[\s-]+", "_", s)
    return s or "unknown"


def _photo_url(p: dict, size: str = PHOTO_SIZE) -> str | None:
    """Swap the default `square` size token in the iNat photo URL for the
    one we want. iNat URLs look like
    ``.../photos/<id>/<size>.jpg`` where ``<size>`` ∈ {square, small,
    medium, large, original}. Some legacy URLs have no size token; we
    return the URL as-is in that case.
    """
    url = p.get("url")
    if not url:
        return None
    for sz in ("square", "small", "medium", "large", "original"):
        if f"/{sz}." in url:
            return url.replace(f"/{sz}.", f"/{size}.")
    return url


async def fetch_observations(
    client: httpx.AsyncClient,
    binomial: str,
    per_species: int,
) -> list[dict]:
    """Single iNat API call for a binomial. Returns the raw observations list."""
    params: list[tuple[str, str]] = [
        ("taxon_name", binomial),
        ("quality_grade", "research"),
        ("photos", "true"),
        ("photo_license", ",".join(PERMISSIVE_LICENSES)),
        ("per_page", str(min(200, per_species))),
        ("order_by", "votes"),
        ("order", "desc"),
    ]
    try:
        r = await client.get(
            INAT_API, params=params, timeout=30, headers={"User-Agent": USER_AGENT}
        )
    except Exception as e:
        print(f"  {binomial}: API error: {e}", file=sys.stderr)
        return []
    if r.status_code != 200:
        print(f"  {binomial}: HTTP {r.status_code}: {r.text[:120]}", file=sys.stderr)
        return []
    return (r.json() or {}).get("results", []) or []


async def download_photo(
    client: httpx.AsyncClient,
    url: str,
    dest: Path,
) -> tuple[bool, int]:
    """Download a single photo to disk. Returns (ok, bytes_written)."""
    if dest.exists() and dest.stat().st_size > 0:
        return True, dest.stat().st_size
    try:
        r = await client.get(url, timeout=60, follow_redirects=True)
        if r.status_code != 200 or not r.content:
            return False, 0
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(r.content)
        return True, len(r.content)
    except Exception:
        return False, 0


def collect_species(per_drive: bool) -> list[dict]:
    """Read species_manifest.json and dedupe by binomial. Returns
    {binomial, common_name, family, genus, species} records ready to query.
    When ``per_drive`` is False we keep one record per unique binomial;
    when True we keep one per (binomial, drive_id) so every Drive entry
    that already had a manual clip gets photos too.
    """
    records = json.loads(SPECIES_MANIFEST.read_text())
    seen: dict[str, dict] = {}
    for r in records:
        b = r.get("binomial")
        if not b or not r.get("genus"):
            continue
        # Skip placeholders ("TBC", non-Latin) — same filter as the WoRMS
        # enricher uses.
        if not re.match(r"^[A-Z][a-z]{2,}", r["genus"] or ""):
            continue
        if b in seen and not per_drive:
            continue
        seen.setdefault(
            b,
            {
                "binomial": b,
                "common_name": r.get("common_name"),
                "family": r.get("family"),
                "genus": r.get("genus"),
                "species": r.get("species"),
            },
        )
    return list(seen.values())


async def main_async(per_species: int, concurrency: int, max_species: int | None) -> None:
    species = collect_species(per_drive=False)
    if max_species:
        species = species[:max_species]
    print(f"querying iNat for {len(species)} unique binomials, ≤ {per_species} photos each")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    download_sem = asyncio.Semaphore(concurrency)
    records: list[InatRecord] = []
    pending_downloads: list[asyncio.Task] = []
    skipped_no_results: list[str] = []
    skipped_no_permissive: list[str] = []

    async with httpx.AsyncClient(http2=False) as client:
        t0 = time.monotonic()
        for i, sp in enumerate(species):
            obs_list = await fetch_observations(client, sp["binomial"], per_species)
            if not obs_list:
                skipped_no_results.append(sp["binomial"])
                await asyncio.sleep(API_RATE_LIMIT_SECONDS)
                continue
            slug = slugify(sp["binomial"])
            species_dir = OUT_DIR / slug
            taken_per_species = 0
            for obs in obs_list:
                if taken_per_species >= per_species:
                    break
                photos = obs.get("photos") or []
                for p in photos:
                    if taken_per_species >= per_species:
                        break
                    if p.get("license_code") is None:
                        continue
                    url = _photo_url(p)
                    if not url:
                        continue
                    photo_id = int(p["id"])
                    dest = species_dir / f"{photo_id}.jpg"
                    rec = InatRecord(
                        source_id=f"inat_{photo_id}",
                        source_kind="inat",
                        photo_id=photo_id,
                        observation_id=int(obs["id"]),
                        binomial=sp["binomial"],
                        common_name=sp.get("common_name"),
                        family=sp.get("family"),
                        genus=sp.get("genus"),
                        species=sp.get("species"),
                        observer=(obs.get("user") or {}).get("login"),
                        license=p.get("license_code"),
                        attribution=p.get("attribution") or "",
                        observed_on=obs.get("observed_on"),
                        original_url=f"https://www.inaturalist.org/photos/{photo_id}",
                        local_path=str(dest.relative_to(REPO_ROOT)),
                    )
                    records.append(rec)
                    pending_downloads.append(
                        asyncio.create_task(
                            _bounded_download(client, url, dest, download_sem),
                        )
                    )
                    taken_per_species += 1
                    break  # one photo per observation → maximises observer diversity
            if taken_per_species == 0:
                skipped_no_permissive.append(sp["binomial"])
            if (i + 1) % 20 == 0:
                elapsed = time.monotonic() - t0
                print(
                    f"  [{i+1:3d}/{len(species)}] api elapsed: {elapsed:.0f}s; "
                    f"records so far: {len(records)}"
                )
            await asyncio.sleep(API_RATE_LIMIT_SECONDS)

        # Wait for the photo downloads (they've been streaming in parallel)
        print(f"awaiting {len(pending_downloads)} photo downloads...")
        results = await asyncio.gather(*pending_downloads, return_exceptions=True)

    failed_downloads = sum(1 for r in results if not (isinstance(r, tuple) and r[0]))

    # Drop manifest records whose download failed so the cropper doesn't trip
    # over missing files on a partial network failure.
    records_kept = []
    for rec in records:
        if (REPO_ROOT / rec.local_path).exists():
            records_kept.append(rec)

    OUT_MANIFEST.write_text(json.dumps([asdict(r) for r in records_kept], indent=2))

    # License distribution + coverage summary
    by_license: dict[str, int] = {}
    by_class: dict[str, int] = {}
    for r in records_kept:
        by_license[r.license or "unknown"] = by_license.get(r.license or "unknown", 0) + 1
        by_class[r.binomial] = by_class.get(r.binomial, 0) + 1

    elapsed = time.monotonic() - t0
    print()
    print(f"done in {elapsed:.0f}s")
    print(f"records kept (download ok): {len(records_kept)}")
    print(f"failed downloads:           {failed_downloads}")
    print(f"species with no iNat hits:  {len(skipped_no_results)}")
    print(f"species w/ hits but ARR-only: {len(skipped_no_permissive)}")
    print()
    print("license distribution:")
    for lic, n in sorted(by_license.items(), key=lambda kv: -kv[1]):
        print(f"  {lic}: {n}")
    print()
    print(f"species coverage: {len(by_class)} classes received photos")
    if skipped_no_results[:8]:
        print()
        print("sample of species iNat had no research-grade hits for:")
        for s in skipped_no_results[:8]:
            print(f"  {s}")
    print()
    print(f"manifest: {OUT_MANIFEST.relative_to(REPO_ROOT)}")


async def _bounded_download(client, url, dest, sem) -> tuple[bool, int]:
    async with sem:
        return await download_photo(client, url, dest)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--per-species", type=int, default=20, help="max photos to keep per species (default 20)"
    )
    ap.add_argument(
        "--concurrency",
        type=int,
        default=DOWNLOAD_CONCURRENCY,
        help="parallel photo downloads (default 16)",
    )
    ap.add_argument(
        "--limit", type=int, default=None, help="cap number of species (for smoke testing)"
    )
    args = ap.parse_args()
    asyncio.run(main_async(args.per_species, args.concurrency, args.limit))


# `field` is kept for future schema extensions (e.g. per-record warnings list)
_ = field

if __name__ == "__main__":
    main()
