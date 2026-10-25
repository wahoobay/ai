#!/usr/bin/env python3
"""Fetch the model weights a worker needs from MODELS_URL into MODELS_DIR, once.

    MODELS_URL      s3://platform-<location>-models/ (AWS) | file:///data/models | https://host/prefix
    MODELS_DIR      where the worker reads them (default /data/models)
    MODEL_VERSIONS  "detector_v26=n3,classifier_v0_10_2=v0.10.2,classifier_v2=dgx-2026-10-24"

Layout, as published by the infra repository's scripts/publish_models.py:
<MODELS_URL>/<model>/<version>/<files> plus a SHA256SUMS file. A model directory
that already holds files is left alone (a bind-mounted data/models wins); each
fetched file is checked against SHA256SUMS before it is kept. For a laptop
without bucket access, scripts/fetch_models.sh still downloads the two public
Fishial bundles.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
from pathlib import Path

try:
    from common.storage import join, open_read
except ImportError:  # run from the repository rather than an image
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
    from common.storage import join, open_read

DEFAULT_VERSIONS = {
    "detector_v26": "n3",
    "classifier_v0_10_2": "v0.10.2",
    "classifier_v2": "dgx-2026-10-24",
}


def versions(spec: str | None = None) -> dict[str, str]:
    spec = spec if spec is not None else os.environ.get("MODEL_VERSIONS", "")
    out = dict(DEFAULT_VERSIONS)
    for part in filter(None, (p.strip() for p in spec.split(","))):
        name, _, version = part.partition("=")
        if not version:
            raise ValueError(f"MODEL_VERSIONS entry {part!r} is not name=version")
        out[name.strip()] = version.strip()
    return out


def fetch_model(models_url: str, dest: Path, model: str, version: str) -> int:
    """Copy <models_url>/<model>/<version>/* into <dest>/<model>, verified; the
    number of files written (0 when the directory already holds files)."""
    target = dest / model
    if target.is_dir() and any(p.is_file() for p in target.rglob("*")):
        print(f"{model}: present in {target}, left alone")
        return 0
    # Whole or not at all: files land in <model>.partial/ and the directory is renamed into place at the
    # end, so a download interrupted by a deploy (the models volume persists on the GPU host) is redone,
    # never mistaken for a complete model by the next start.
    staging = dest / f"{model}.partial"
    if staging.exists():
        shutil.rmtree(staging)
    base = join(models_url, f"{model}/{version}")
    sums = open_read(join(base, "SHA256SUMS")).decode()
    written = 0
    for line in filter(None, (ln.strip() for ln in sums.splitlines())):
        digest, _, rel = line.partition("  ")
        data = open_read(join(base, rel))
        actual = hashlib.sha256(data).hexdigest()
        if actual != digest:
            raise ValueError(f"{model}/{version}/{rel}: sha256 {actual} does not match SHA256SUMS")
        path = staging / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        written += 1
    if target.exists():
        shutil.rmtree(target)  # an empty or stale directory; a filled one returned above
    staging.rename(target)
    print(f"{model}@{version}: {written} file(s) into {target}")
    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--models-url", default=os.environ.get("MODELS_URL", "file:///data/models"))
    ap.add_argument("--dest", default=os.environ.get("MODELS_DIR", "/data/models"))
    ap.add_argument("--only", action="append", help="fetch only this model (repeatable)")
    args = ap.parse_args(argv)
    dest = Path(args.dest)
    wanted = versions()
    if args.only:
        wanted = {name: wanted[name] for name in args.only}
    if (
        dest.resolve() == Path(args.models_url.removeprefix("file://")).resolve()
        if args.models_url.startswith("file://")
        else False
    ):
        print(f"MODELS_URL is {dest}: the models are read in place")
        return 0
    failed = 0
    for model, version in wanted.items():
        try:
            fetch_model(args.models_url, dest, model, version)
        except (FileNotFoundError, ValueError, OSError) as e:
            failed += 1
            print(f"{model}@{version}: not fetched: {e}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
