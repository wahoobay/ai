"""The dashboard code runs from two layouts: a checkout (services/dashboard/app, the
repository root three levels up) and the image (/app/app, with assets/ and db/
copied beside app/). Paths must resolve in both; the image layout is shallow."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _image_like(tmp_path: Path) -> Path:
    root = tmp_path / "app"  # /app in the image: app/, db/, assets/, common/ beside each other
    shutil.copytree(REPO / "services" / "dashboard" / "app", root / "app")
    shutil.copytree(REPO / "services" / "common", root / "common")
    shutil.copytree(REPO / "db", root / "db")
    (root / "assets").mkdir()
    return root


def test_paths_resolve_in_the_image_layout(tmp_path):
    root = _image_like(tmp_path)
    code = (
        "from app import migrate, seed, main, common_names; "
        "print(migrate.alembic_ini()); print(main.REPO_ROOT); print(common_names._repo_root()); "
        "print(seed.REPO_SAMPLE)"
    )
    r = subprocess.run(
        [sys.executable, "-c", code],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root)},
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert lines[0] == str(root / "db" / "alembic.ini")
    assert lines[1] == str(root) and lines[2] == str(root)
    assert lines[3] == str(root / "data" / "sample")


def test_paths_resolve_in_a_checkout():
    from app import common_names, main, migrate, seed

    assert migrate.alembic_ini() == REPO / "db" / "alembic.ini"
    assert main.REPO_ROOT == REPO and common_names._repo_root() == REPO
    assert seed.REPO_SAMPLE == REPO / "data" / "sample"


def test_worker_image_carries_boto3_for_s3_weights_and_frames():
    # On the GPU host MODELS_URL and FRAMES_URL are s3://: fetch_models.py and the frame writer
    # import boto3 through common.storage, so the worker image must have it (both targets).
    requirements = (REPO / "services" / "worker" / "requirements.txt").read_text()
    assert "boto3==" in requirements


def test_dashboard_image_carries_boto3_for_s3_storage():
    # On AWS FRAMES_URL and SAMPLE_URL are s3:// and common.storage imports boto3 for them;
    # the dashboard image (service and lambda stages) must have it, or every thumbnail 500s.
    import importlib.util

    requirements = (REPO / "services" / "dashboard" / "requirements.txt").read_text()
    assert "boto3==" in requirements
    assert importlib.util.find_spec("boto3") is not None


def test_worker_image_carries_what_its_code_imports():
    """The GPU host's first boot crash-looped on `ModuleNotFoundError: httpx` (and boto3 before it):
    the DGX's conda environment had them, the image did not. Every third-party module the worker
    and the shared code import must be named in the worker's requirements."""
    import ast
    import sys

    aliases = {
        "cv2": "opencv",
        "PIL": "pillow",
        "yaml": "pyyaml",
        "psycopg_pool": "psycopg-pool",
        "sklearn": "scikit-learn",
    }
    local = {
        "app",
        "common",
        "services",
        "fetch_models",
        "inference",
    }  # `inference`: the legacy classifier module loaded from /data/models
    modules: set[str] = set()
    for folder in ("services/worker/app", "services/common"):
        for path in (REPO / folder).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Import):
                    modules.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    modules.add(node.module.split(".")[0])
    third_party = {m for m in modules if m not in sys.stdlib_module_names and m not in local}
    requirements = "\n".join(
        (REPO / "services" / "worker" / name).read_text().lower()
        for name in ("requirements.txt", "requirements-cuda.txt")
    )
    pinned: set[str] = set()
    for line in requirements.splitlines():
        if "==" not in line or line.startswith("#"):
            continue
        name, _, extras = line.split("==")[0].strip().partition("[")
        pinned.add(name)
        pinned.update(
            f"{name}-{extra.strip()}" for extra in extras.rstrip("]").split(",") if extra.strip()
        )  # psycopg[pool] -> psycopg-pool
    missing = sorted(
        m for m in third_party if not any(p.startswith(aliases.get(m, m).lower()) for p in pinned)
    )  # cv2 -> opencv-python-headless
    assert not missing, f"imported by the worker but not in its requirements: {missing}"
