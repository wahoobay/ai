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
