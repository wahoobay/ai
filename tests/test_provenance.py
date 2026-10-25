"""Provenance at build time (Plan 2a, Task 9): the image carries GIT_SHA, so the
worker's rows say which commit produced them even where there is no git."""

from __future__ import annotations


def test_provenance_uses_git_sha_env(monkeypatch):
    from services.worker.app import provenance

    monkeypatch.setenv("GIT_SHA", "abc123def456")
    monkeypatch.setenv("PATH", "")  # no git anywhere
    assert provenance._git_sha() == "abc123def456"


def test_provenance_falls_back_to_git_then_unknown(monkeypatch, tmp_path):
    from services.worker.app import provenance

    monkeypatch.delenv("GIT_SHA", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))  # no git: nothing to ask
    assert provenance._git_sha() == "unknown"
