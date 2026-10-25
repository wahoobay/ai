"""The GitHub Actions workflows that reach AWS."""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def test_no_job_names_a_github_environment():
    """The deploy roles in `infra` trust the `ref:refs/heads/main` OIDC subject. A job
    that names a GitHub environment gets `environment:<name>` as its subject instead,
    and the role refuses it (the first automatic staging deploy failed exactly so)."""
    offenders = {
        f"{path.name}:{i}"
        for path in WORKFLOWS.glob("*.yml")
        for i, line in enumerate(path.read_text().splitlines(), 1)
        if re.match(r"^\s+environment:\s*\S", line)
    }
    assert offenders == set(), sorted(offenders)
