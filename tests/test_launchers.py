"""The DGX launchers (scripts/dev) must hand the worker and the dashboard the same
FRAMES_URL, or new frames land where the page does not look."""

from pathlib import Path

DEV = Path(__file__).resolve().parents[1] / "scripts" / "dev"
FRAMES_URL_LINE = 'export FRAMES_URL="${FRAMES_URL:-file://${FRAMES_DIR:-$(pwd)/frames}}"'


def test_worker_and_dashboard_launchers_derive_frames_url_the_same_way():
    for name in ("run_dashboard.sh", "run_worker.sh", "run_worker_pier.sh"):
        text = (DEV / name).read_text()
        assert FRAMES_URL_LINE in text, name
        assert text.count("export FRAMES_URL=") == 1, name
