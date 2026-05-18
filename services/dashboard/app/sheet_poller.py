"""Periodic refresh of Wahoo Bay's species + celebrities Google Sheet.

The catalogue lives in a public Google Sheet that camera operators
maintain by hand — new species rows when a fish is identified, new
celebrity entries when chatters name a recurring animal. The local
files (`assets/species.xlsx`, `assets/species_manifest.json`,
`assets/celebrities.json`) are derived from it.

This module pulls a fresh xlsx export on a schedule (default daily),
re-runs the build scripts when the sheet has changed, and asks the
dashboard's `refresh_fn` to hot-reload the in-memory catalogues. No
dashboard restart required for chatters' new animals to show up.

Sheet visibility: relies on "anyone with the link" sharing — the
`export?format=xlsx` URL works without auth in that mode. If the
sheet becomes private later, the poller logs a warning and the
existing local files stay in place (graceful degradation).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import sys
from pathlib import Path
from typing import Awaitable, Callable

import httpx

log = logging.getLogger(__name__)

# Scripts the poller re-runs after a sheet update lands. Order matters
# — build_species_manifest must run before build_celebrities, since the
# celebrities parser uses the species manifest's common-name layer for
# its binomial reverse-lookup.
_BUILD_SCRIPTS = ("build_species_manifest.py", "build_celebrities.py")


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:16]


class SheetPoller:
    """Async sheet-watcher. Runs forever inside the dashboard's
    asyncio loop; safe to cancel from the lifespan shutdown path."""

    def __init__(
        self,
        sheet_url: str,
        xlsx_path: Path,
        repo_root: Path,
        interval_s: float,
        http_client: httpx.AsyncClient,
        refresh_fn: Callable[[], Awaitable[None]],
        startup_delay_s: float = 60.0,
    ) -> None:
        self.sheet_url = sheet_url
        self.xlsx_path = xlsx_path
        self.repo_root = repo_root
        self.interval_s = interval_s
        self.http_client = http_client
        self.refresh_fn = refresh_fn
        self.startup_delay_s = startup_delay_s

    async def run_forever(self) -> None:
        # Wait a bit so we don't fight startup for the first cycle.
        await asyncio.sleep(self.startup_delay_s)
        while True:
            try:
                await self.refresh_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("sheet_poller: iteration failed; continuing")
            try:
                await asyncio.sleep(self.interval_s)
            except asyncio.CancelledError:
                raise

    async def refresh_once(self, force: bool = False) -> dict:
        """Pull the sheet; rebuild catalogues + hot-reload only if the
        bytes changed (or `force=True`). Returns a tiny status dict
        usable by the admin endpoint."""
        result: dict = {
            "ok": False,
            "downloaded_bytes": 0,
            "sheet_changed": False,
            "rebuilt": [],
            "errors": [],
        }
        try:
            r = await self.http_client.get(
                self.sheet_url, timeout=60.0, follow_redirects=True,
            )
        except Exception as e:
            msg = f"download failed: {e}"
            log.warning("sheet_poller: %s", msg)
            result["errors"].append(msg)
            return result
        if r.status_code != 200:
            msg = f"HTTP {r.status_code} fetching {self.sheet_url}"
            log.warning("sheet_poller: %s", msg)
            result["errors"].append(msg)
            return result

        new_bytes = r.content
        result["downloaded_bytes"] = len(new_bytes)
        new_hash = _sha(new_bytes)
        old_hash = (
            _sha(self.xlsx_path.read_bytes()) if self.xlsx_path.exists() else None
        )

        if old_hash == new_hash and not force:
            log.debug("sheet_poller: unchanged (sha=%s)", new_hash)
            result["ok"] = True
            return result

        # Write atomically — temp file + rename keeps the old xlsx
        # readable if the rename is interrupted.
        tmp = self.xlsx_path.with_suffix(self.xlsx_path.suffix + ".tmp")
        try:
            tmp.write_bytes(new_bytes)
            tmp.replace(self.xlsx_path)
        except Exception as e:
            msg = f"write failed: {e}"
            log.exception("sheet_poller: %s", msg)
            result["errors"].append(msg)
            return result
        log.info(
            "sheet_poller: xlsx updated (sha %s → %s, %d bytes)",
            old_hash, new_hash, len(new_bytes),
        )
        result["sheet_changed"] = True

        # Re-run build scripts. Subprocess them so a script crash can't
        # take down the dashboard event loop, and stdout/stderr land in
        # the log alongside everything else.
        for script in _BUILD_SCRIPTS:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                str(self.repo_root / "scripts" / script),
                cwd=str(self.repo_root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await proc.communicate()
            tail = (out or b"").decode("utf-8", errors="replace").strip().splitlines()[-1:]
            if proc.returncode != 0:
                msg = f"{script} exit={proc.returncode}: {' | '.join(tail)}"
                log.warning("sheet_poller: %s", msg)
                result["errors"].append(msg)
            else:
                log.info("sheet_poller: %s OK — %s", script, ' | '.join(tail))
                result["rebuilt"].append(script)

        # Hot-reload in-memory state (celebrities catalogue, common-name
        # manifest fallback). Done even if some build scripts errored,
        # as long as their output files still exist on disk.
        try:
            await self.refresh_fn()
        except Exception as e:
            msg = f"refresh_fn failed: {e}"
            log.exception("sheet_poller: %s", msg)
            result["errors"].append(msg)
            return result

        result["ok"] = not result["errors"]
        return result
