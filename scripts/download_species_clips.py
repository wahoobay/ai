"""Bulk-download the Google Drive image / video clips referenced in
``assets/species_manifest.json`` to local disk.

The manifest is produced by ``scripts/build_species_manifest.py`` and
carries a ``drive_id`` + ``drive_url`` pair for each volunteer-labeled
example. All files are shared with ``usp=sharing`` access, which means
they're public-readable and the download is anonymous — no OAuth, no
service account.

Downloads land in ``assets/community_clips/{drive_id}.{ext}`` so the
file's provenance survives later renames. Extension comes from the
Drive ``Content-Disposition`` header when present, otherwise from
sniffed magic bytes. A run report is written to
``assets/community_clips/_download_report.json`` summarising successes,
skips, and failures.

The script is idempotent — running it a second time will skip every
file already on disk (matched by the drive_id stem), so it's safe to
re-run after a partial network failure.

Drive interstitials: for files >100 MB Drive interrupts the download
with a "virus scan can't be performed" confirmation page. We parse the
``confirm`` token out of the response and replay the request with it
appended. Files under 100 MB stream straight through.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

try:
    import httpx
except ImportError as e:
    sys.exit(f"httpx is required: {e}")


REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "assets" / "species_manifest.json"
OUT_DIR = REPO_ROOT / "assets" / "community_clips"
REPORT_PATH = OUT_DIR / "_download_report.json"


# Drive's first response is a 303 redirect from drive.google.com/uc → the
# usercontent host. httpx follow_redirects=True handles that. The only
# case requiring extra logic is the >100MB virus-scan interstitial.
DRIVE_DOWNLOAD_URL = "https://drive.google.com/uc?export=download&id={id}"


@dataclass
class DownloadResult:
    drive_id: str
    binomial: str | None
    common_name: str
    status: str             # "ok" | "skipped" | "error"
    local_path: str | None  # relative to repo root
    bytes: int
    mime: str | None
    error: str | None


def sniff_extension(head: bytes, content_type: str | None) -> str:
    """Pick a sane file extension from response magic bytes / Content-Type.

    Most files are JPEG (community screenshots) or MP4 (short clips); a
    handful might be PNG / WEBP / MOV. We fall back to ``.bin`` if we
    can't tell — the cropper will skip those and they get flagged in
    the report.
    """
    if len(head) >= 12:
        if head[:3] == b"\xff\xd8\xff":
            return ".jpg"
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            return ".png"
        if head[:6] in (b"GIF87a", b"GIF89a"):
            return ".gif"
        if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            return ".webp"
        if head[4:8] == b"ftyp":
            return ".mp4"
        if head[:4] == b"\x1aE\xdf\xa3":
            return ".mkv"
        if head[:3] == b"ID3" or head[:2] == b"\xff\xfb":
            return ".mp3"
    if content_type:
        if content_type.startswith("image/jpeg"):
            return ".jpg"
        if content_type.startswith("image/png"):
            return ".png"
        if content_type.startswith("video/mp4"):
            return ".mp4"
        if content_type.startswith("video/quicktime"):
            return ".mov"
    return ".bin"


def extract_form_params(text: str) -> dict[str, str]:
    """Pull all hidden ``<input name=X value=Y>`` pairs out of Drive's
    virus-scan interstitial. The modern interstitial uses a POST form
    on ``drive.usercontent.google.com/download`` with fields id, export,
    confirm (literally "t"), and uuid — older clients that just appended
    ``&confirm=<token>`` to the original URL fail because they're missing
    the uuid.
    """
    params: dict[str, str] = {}
    for name, value in re.findall(r'name="([^"]+)"\s+value="([^"]*)"', text):
        params[name] = value
    return params


async def download_one(
    client: httpx.AsyncClient,
    rec: dict,
    sem: asyncio.Semaphore,
) -> DownloadResult:
    drive_id = rec["drive_id"]
    binomial = rec.get("binomial")
    common = rec.get("common_name", "")
    # Already downloaded? Skip — match by drive_id prefix on disk.
    for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".mov", ".mkv", ".bin"):
        p = OUT_DIR / f"{drive_id}{ext}"
        if p.exists() and p.stat().st_size > 0:
            return DownloadResult(
                drive_id=drive_id, binomial=binomial, common_name=common,
                status="skipped", local_path=str(p.relative_to(REPO_ROOT)),
                bytes=p.stat().st_size, mime=None, error=None,
            )

    async with sem:
        url = DRIVE_DOWNLOAD_URL.format(id=drive_id)
        try:
            r = await client.get(url, follow_redirects=True, timeout=120)
        except Exception as e:
            return DownloadResult(
                drive_id=drive_id, binomial=binomial, common_name=common,
                status="error", local_path=None, bytes=0, mime=None, error=str(e),
            )

        # Detect the virus-scan interstitial: response is HTML, not the file.
        ct = r.headers.get("content-type", "")
        if "text/html" in ct and len(r.content) < 64 * 1024:
            params = extract_form_params(r.text)
            if params and "uuid" in params:
                # Modern interstitial — POST-style download URL on the
                # usercontent host with id/export/confirm/uuid query params.
                try:
                    r = await client.get(
                        "https://drive.usercontent.google.com/download",
                        params={k: params.get(k, "") for k in ("id", "export", "confirm", "uuid")},
                        follow_redirects=True, timeout=300,
                    )
                    ct = r.headers.get("content-type", "")
                except Exception as e:
                    return DownloadResult(
                        drive_id=drive_id, binomial=binomial, common_name=common,
                        status="error", local_path=None, bytes=0, mime=ct,
                        error=f"confirm-step: {e}",
                    )
            elif params.get("confirm"):
                # Legacy interstitial — just re-fetch original URL with token.
                try:
                    r = await client.get(
                        f"{url}&confirm={params['confirm']}",
                        follow_redirects=True, timeout=300,
                    )
                    ct = r.headers.get("content-type", "")
                except Exception as e:
                    return DownloadResult(
                        drive_id=drive_id, binomial=binomial, common_name=common,
                        status="error", local_path=None, bytes=0, mime=ct,
                        error=f"confirm-step: {e}",
                    )
            else:
                return DownloadResult(
                    drive_id=drive_id, binomial=binomial, common_name=common,
                    status="error", local_path=None, bytes=0, mime=ct,
                    error="HTML response with no confirm form (file may be private or deleted)",
                )

        if r.status_code != 200:
            return DownloadResult(
                drive_id=drive_id, binomial=binomial, common_name=common,
                status="error", local_path=None, bytes=0, mime=ct,
                error=f"HTTP {r.status_code}",
            )

        ext = sniff_extension(r.content[:32], ct)
        out_path = OUT_DIR / f"{drive_id}{ext}"
        out_path.write_bytes(r.content)
        return DownloadResult(
            drive_id=drive_id, binomial=binomial, common_name=common,
            status="ok", local_path=str(out_path.relative_to(REPO_ROOT)),
            bytes=len(r.content), mime=ct, error=None,
        )


async def main_async(concurrency: int, limit: int | None) -> None:
    if not MANIFEST_PATH.exists():
        sys.exit(f"missing manifest: {MANIFEST_PATH} — run build_species_manifest.py first")
    records = json.loads(MANIFEST_PATH.read_text())
    with_links = [r for r in records if r.get("drive_id")]
    if limit:
        with_links = with_links[:limit]
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    sem = asyncio.Semaphore(concurrency)
    t0 = time.monotonic()
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            *[download_one(client, r, sem) for r in with_links],
            return_exceptions=False,
        )
    elapsed = time.monotonic() - t0

    by_status: dict[str, int] = {"ok": 0, "skipped": 0, "error": 0}
    by_ext: dict[str, int] = {}
    for r in results:
        by_status[r.status] = by_status.get(r.status, 0) + 1
        if r.local_path:
            ext = Path(r.local_path).suffix
            by_ext[ext] = by_ext.get(ext, 0) + 1

    REPORT_PATH.write_text(json.dumps({
        "elapsed_seconds": round(elapsed, 1),
        "total": len(results),
        "by_status": by_status,
        "by_extension": by_ext,
        "results": [asdict(r) for r in results],
    }, indent=2))

    print(f"downloaded in {elapsed:.1f}s — {by_status}")
    print(f"by extension: {by_ext}")
    if by_status.get("error", 0):
        errs = [r for r in results if r.status == "error"][:8]
        print()
        print("sample errors:")
        for r in errs:
            print(f"  {r.drive_id}  {r.binomial}  → {r.error}")
    print(f"report: {REPORT_PATH.relative_to(REPO_ROOT)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", type=int, default=8,
                    help="parallel downloads (default 8)")
    ap.add_argument("--limit", type=int, default=None,
                    help="stop after N files (for smoke-testing)")
    args = ap.parse_args()
    asyncio.run(main_async(args.concurrency, args.limit))


if __name__ == "__main__":
    main()


# Suppress unused-import warning on hashlib — kept for future sha256 dedup
_ = hashlib
