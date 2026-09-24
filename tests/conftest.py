"""Shared pytest fixtures for the whole test suite.

Autouse network isolation lives here, not in an individual test file's
fixtures, specifically so isolation cannot be skipped by omission. See
the fixture docstring below for what that protects against and why an
autouse fixture — rather than something each test file has to remember
to apply — is the fix."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_network(monkeypatch):
    """build_app()'s lifespan starts background tasks that reach the
    network: a Google Sheets poller, and an SLOChecker that polls
    worker_url/poller_url every 30s (and once immediately on startup).

    On this shared machine, WORKER_URL/POLLER_URL default to
    localhost:8081/8082 — the REAL production worker and poller — and
    SHEET_URL defaults to the production Google Sheet. Any test that
    builds the app without overriding these would issue live requests
    against production services and the production sheet: slow, rude to
    production, and (per an earlier incident) able to overwrite
    regenerated catalogue files under assets/.

    Autouse, applied via monkeypatch (auto-reverted at test teardown) so
    every test in this suite gets it regardless of which fixtures it
    requests — a test file that builds the app through a fixture that
    forgets to isolate the network no longer silently depends on a
    sibling test having left the isolation in place process-globally."""
    monkeypatch.setenv("SHEET_URL", "")
    monkeypatch.setenv("WORKER_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("POLLER_URL", "http://127.0.0.1:1")
