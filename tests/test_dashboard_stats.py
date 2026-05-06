"""Smoke tests for services/dashboard/app/stats.py.

These functions are pure and side-effect free, so they're a clean
target for the first pytest entries. The bigger goal is to make sure
the stats helpers behave at the boundaries (count=0, trials=0,
bootstrap with a single class) — that's where they used to break."""
from __future__ import annotations

import math
import sys
from pathlib import Path

# Make the dashboard's `app` package importable without installing it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "dashboard"))

import pytest  # noqa: E402
from app.stats import (  # noqa: E402
    CI,
    bootstrap_ci,
    bootstrap_count_ci,
    mean_ci,
    poisson_count_ci,
    rate_ci,
)

# ---------------------------------------------------------------------------
# CI dataclass
# ---------------------------------------------------------------------------

def test_ci_as_dict_is_json_safe():
    ci = CI(point=1.5, lo=1.0, hi=2.0, method="x", n=10)
    d = ci.as_dict()
    assert d["point"] == 1.5
    assert d["lo"] == 1.0
    assert d["hi"] == 2.0
    assert d["method"] == "x"
    assert d["n"] == 10
    # everything must be a primitive — np.float64 etc. would break JSON
    for v in d.values():
        assert v is None or isinstance(v, int | float | str)


# ---------------------------------------------------------------------------
# Poisson exact CI
# ---------------------------------------------------------------------------

def test_poisson_zero_count_has_zero_lower_bound():
    ci = poisson_count_ci(0)
    assert ci.point == 0.0
    assert ci.lo == 0.0
    assert ci.hi > 0  # upper bound is non-zero even for zero count


def test_poisson_count_brackets_point():
    ci = poisson_count_ci(10)
    assert ci.lo < 10.0 < ci.hi
    assert ci.method == "poisson_exact"


def test_poisson_negative_raises():
    with pytest.raises(ValueError):
        poisson_count_ci(-1)


# ---------------------------------------------------------------------------
# Wilson rate CI
# ---------------------------------------------------------------------------

def test_rate_ci_zero_trials_returns_nan():
    ci = rate_ci(0, 0)
    assert math.isnan(ci.point)
    assert math.isnan(ci.lo)
    assert math.isnan(ci.hi)


def test_rate_ci_brackets_point():
    ci = rate_ci(50, 100)
    assert ci.point == 0.5
    assert 0.0 <= ci.lo <= 0.5 <= ci.hi <= 1.0


def test_rate_ci_extremes_stay_in_unit_interval():
    for s, t in [(0, 100), (100, 100), (1, 1)]:
        ci = rate_ci(s, t)
        assert 0.0 <= ci.lo <= ci.hi <= 1.0, f"out of [0,1] for ({s}, {t}): {ci}"


# ---------------------------------------------------------------------------
# Bootstrap CIs
# ---------------------------------------------------------------------------

def test_bootstrap_count_ci_empty_input():
    ci = bootstrap_count_ci([], species_id="X", seed=0)
    assert ci.point == 0.0
    assert ci.lo == 0.0
    assert ci.hi == 0.0
    assert ci.n == 0


def test_bootstrap_count_ci_brackets_point():
    sample = ["A"] * 30 + ["B"] * 70
    ci = bootstrap_count_ci(sample, species_id="A", B=500, seed=42)
    assert ci.point == 30.0
    assert ci.lo <= 30.0 <= ci.hi


def test_bootstrap_ci_deterministic_with_seed():
    data = [1.0, 2.0, 3.0, 4.0, 5.0] * 10
    a = bootstrap_ci(data, statistic=lambda x: float(sum(x)) / len(x), B=200, seed=7)
    b = bootstrap_ci(data, statistic=lambda x: float(sum(x)) / len(x), B=200, seed=7)
    assert a.point == b.point
    assert a.lo == b.lo
    assert a.hi == b.hi


def test_mean_ci_brackets_sample_mean():
    data = [10.0, 11.0, 9.0, 10.5, 9.5, 10.2, 9.8, 10.1, 9.9, 10.0]
    ci = mean_ci(data, B=500, seed=0)
    sample_mean = sum(data) / len(data)
    assert abs(ci.point - sample_mean) < 1e-9
    assert ci.lo <= sample_mean <= ci.hi
