"""Every review-tool setting the dashboard reads is documented."""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_every_review_setting_is_in_env_example():
    main = (REPO / "services" / "dashboard" / "app" / "main.py").read_text()
    names = set(re.findall(r'_env\(\s*"((?:REVIEW|DASHBOARD_DB_POOL)[A-Z_]*)"', main))
    assert names, "no review settings found in main.py"
    example = (REPO / ".env.example").read_text()
    missing = sorted(n for n in names if not re.search(rf"^#?\s*{n}=", example, re.M))
    assert not missing, f"undocumented in .env.example: {missing}"


def test_operations_doc_covers_the_pool_task():
    ops = (REPO / "docs" / "operations.md").read_text()
    for phrase in (
        "REVIEW_POOL_ENABLED",
        "2026-09-24-review-tool.sql",
        "/api/review/status",
        "seed_review_demo.py",
    ):
        assert phrase in ops, phrase


def test_operations_doc_covers_the_old_form_and_its_remap():
    ops = (REPO / "docs" / "operations.md").read_text()
    for phrase in ("remap_legacy_corrections.py", "--apply", "REVIEW_SKIP_TTL_DAYS"):
        assert phrase in ops, phrase
