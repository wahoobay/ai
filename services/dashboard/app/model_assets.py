"""The one model file the dashboard needs: the classifier's class list.

The review catalogue reads ``<models dir>/classifier_v2/classes.json`` to know which species the
classifier can name. The weights live in the models bucket (``MODELS_URL``) and the dashboard
never fetches them, so on AWS the file was missing and the catalogue, and with it the review
queue, stayed empty (2026-10-27). At start the dashboard fetches just ``classes.json`` of each
pinned model version into ``MODELS_DIR``; a file already there (the DGX's bind mount, an earlier
start) is left alone, and a failure is a warning: the page must come up without it."""

from __future__ import annotations

import importlib.util
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path

from common.storage import join, open_read

log = logging.getLogger("wahoobay.dashboard.model_assets")

CLASS_LIST_MODELS: tuple[str, ...] = ("classifier_v2",)


def fetch_class_lists(
    models_url: str,
    dest: Path,
    versions: Mapping[str, str],
    models: Sequence[str] = CLASS_LIST_MODELS,
) -> list[Path]:
    """Copy ``<models_url>/<model>/<version>/classes.json`` to ``<dest>/<model>/classes.json`` for
    each model that has no class list on disk yet. Returns the paths written."""
    written: list[Path] = []
    for model in models:
        target = dest / model / "classes.json"
        if target.exists():
            continue
        version = versions.get(model)
        if not version:
            log.warning(
                "model assets: no pinned version for %s; its classes.json is not fetched", model
            )
            continue
        url = join(models_url, f"{model}/{version}/classes.json")
        try:
            data = open_read(url)
        except FileNotFoundError:
            log.warning(
                "model assets: %s not found; the review catalogue has no classifier classes", url
            )
            continue
        except Exception as e:  # noqa: BLE001 - the page must start without it
            log.warning(
                "model assets: could not read %s (%s); the review catalogue has no classifier classes",
                url,
                e,
            )
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name("classes.json.partial")
        partial.write_bytes(data)
        partial.replace(target)
        written.append(target)
    return written


def pinned_versions(repo_root: Path) -> dict[str, str]:
    """The model versions the worker fetches (``scripts/fetch_models.py``, honouring MODEL_VERSIONS):
    ``/app/fetch_models.py`` in the image, ``scripts/fetch_models.py`` in a checkout."""
    try:
        import fetch_models  # type: ignore[import-not-found]
    except ImportError:
        spec = importlib.util.spec_from_file_location(
            "fetch_models", repo_root / "scripts" / "fetch_models.py"
        )
        if spec is None or spec.loader is None:
            raise
        fetch_models = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fetch_models)
    return fetch_models.versions()
