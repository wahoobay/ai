"""The dashboard's review catalogue needs the classifier's class list, which lives with the weights in
the models bucket (2026-10-27: /app/data/models/classifier_v2/classes.json not found on AWS, catalogue
empty, review queue never filled). The dashboard fetches just that file at start."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from app.model_assets import fetch_class_lists
from fastapi.testclient import TestClient
from review_fixtures import TEST_DSN

VERSIONS = {"classifier_v2": "dgx-2026-10-24", "detector_v26": "n3"}


def _source(tmp_path: Path) -> str:
    src = tmp_path / "bucket" / "classifier_v2" / "dgx-2026-10-24"
    src.mkdir(parents=True)
    (src / "classes.json").write_text(json.dumps(["Ocyurus chrysurus", "Chelonia mydas"]))
    (src / "model.safetensors").write_bytes(b"\0" * 1024)  # never fetched: only the class list
    return (tmp_path / "bucket").as_uri() + "/"


def test_fetches_the_class_list_of_the_pinned_version_and_nothing_else(tmp_path):
    dest = tmp_path / "models"
    written = fetch_class_lists(_source(tmp_path), dest, VERSIONS)
    assert written == [dest / "classifier_v2" / "classes.json"]
    assert json.loads(written[0].read_text()) == ["Ocyurus chrysurus", "Chelonia mydas"]
    assert not (dest / "classifier_v2" / "model.safetensors").exists()


def test_a_class_list_already_on_disk_is_left_alone(tmp_path):
    dest = tmp_path / "models"
    (dest / "classifier_v2").mkdir(parents=True)
    (dest / "classifier_v2" / "classes.json").write_text(
        "[]"
    )  # the DGX's bind mount, or a previous start
    assert fetch_class_lists(_source(tmp_path), dest, VERSIONS) == []
    assert (dest / "classifier_v2" / "classes.json").read_text() == "[]"


def test_a_missing_source_is_a_warning_not_a_crash(tmp_path, caplog):
    dest = tmp_path / "models"
    with caplog.at_level(logging.WARNING):
        assert fetch_class_lists((tmp_path / "nowhere").as_uri() + "/", dest, VERSIONS) == []
    assert "classes.json" in caplog.text and not (dest / "classifier_v2").exists()


@pytest.mark.anyio
async def test_the_dashboard_fetches_the_class_list_when_it_starts(
    rdb, review_assets, monkeypatch, tmp_path
):
    from app.main import build_app

    for key, value in review_assets["env"].items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DATABASE_URL", TEST_DSN)
    monkeypatch.setenv("DASHBOARD_COOKIE_SECURE", "false")
    monkeypatch.setenv("FEATURE_REVIEW_TOOL", "true")
    monkeypatch.setenv("REVIEW_POOL_ENABLED", "false")
    monkeypatch.setenv("MODELS_URL", _source(tmp_path))
    monkeypatch.setenv("MODELS_DIR", str(tmp_path / "models"))
    monkeypatch.setenv("MODEL_VERSIONS", "classifier_v2=dgx-2026-10-24")
    with TestClient(build_app()):
        pass
    assert (
        json.loads((tmp_path / "models" / "classifier_v2" / "classes.json").read_text())[0]
        == "Ocyurus chrysurus"
    )
