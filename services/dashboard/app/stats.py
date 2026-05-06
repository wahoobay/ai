"""Statistical helpers for rigorous reporting.

Every aggregate we show to the outside world should carry an uncertainty
estimate. These helpers provide:

  - bootstrap_ci:        non-parametric bootstrap over an arbitrary statistic
  - bootstrap_count_ci:  specialised for count aggregates (fast, uses numpy)
  - poisson_count_ci:    exact CI for Poisson counts when that assumption holds
  - mean_ci:             bootstrap CI for the mean of a small sample
  - rate_ci:             Wilson CI for a Bernoulli rate (e.g. fish-frame rate)

Design goals: deterministic (explicit seed), side-effect-free, numpy-only so
they work in both the dashboard and eval harness.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np

DEFAULT_B = 2000          # bootstrap resamples
DEFAULT_ALPHA = 0.05      # → 95 % CI


@dataclass(frozen=True)
class CI:
    """Closed-form CI container. ``.as_dict()`` is JSON-safe."""
    point: float
    lo: float
    hi: float
    method: str
    n: int
    alpha: float = DEFAULT_ALPHA

    def as_dict(self) -> dict:
        return {
            "point": None if self.point is None else float(self.point),
            "lo":    None if self.lo    is None else float(self.lo),
            "hi":    None if self.hi    is None else float(self.hi),
            "method": self.method,
            "n": int(self.n),
            "alpha": self.alpha,
        }


# ---------------------------------------------------------------------------
# Core bootstrap
# ---------------------------------------------------------------------------


def bootstrap_ci(
    sample: Sequence[float],
    statistic: Callable[[np.ndarray], float],
    B: int = DEFAULT_B,
    alpha: float = DEFAULT_ALPHA,
    seed: int = 0,
) -> CI:
    """Percentile bootstrap CI for an arbitrary scalar statistic."""
    arr = np.asarray(sample, dtype=np.float64)
    n = arr.size
    if n == 0:
        return CI(point=float("nan"), lo=float("nan"), hi=float("nan"),
                  method="bootstrap_percentile", n=0, alpha=alpha)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(B, n))
    draws = np.apply_along_axis(statistic, 1, arr[idx])
    lo, hi = np.quantile(draws, [alpha / 2, 1 - alpha / 2])
    return CI(point=float(statistic(arr)), lo=float(lo), hi=float(hi),
              method="bootstrap_percentile", n=int(n), alpha=alpha)


def mean_ci(
    sample: Sequence[float],
    B: int = DEFAULT_B,
    alpha: float = DEFAULT_ALPHA,
    seed: int = 0,
) -> CI:
    return bootstrap_ci(sample, np.mean, B=B, alpha=alpha, seed=seed)


# ---------------------------------------------------------------------------
# Count-oriented CIs (species counts, event totals, etc.)
# ---------------------------------------------------------------------------


def poisson_count_ci(count: int, alpha: float = DEFAULT_ALPHA) -> CI:
    """Exact Poisson CI using the chi-square relationship.

    Good default for "how many events in window W?" when events are
    independent. Falls back to bootstrap if you suspect clustering.
    """
    from scipy.stats import chi2  # lazy import
    if count < 0:
        raise ValueError("count must be non-negative")
    if count == 0:
        lo = 0.0
    else:
        lo = chi2.ppf(alpha / 2,     2 * count)       / 2.0
    hi = chi2.ppf(1 - alpha / 2, 2 * (count + 1)) / 2.0
    return CI(point=float(count), lo=float(lo), hi=float(hi),
              method="poisson_exact", n=int(count), alpha=alpha)


def bootstrap_count_ci(
    species_per_event: Sequence[str],
    species_id: str,
    B: int = DEFAULT_B,
    alpha: float = DEFAULT_ALPHA,
    seed: int = 0,
) -> CI:
    """Bootstrap CI for the count of one species in a labelled event list.

    Accounts for within-sample dependence that Poisson assumes away (a fish
    passing in front of the camera for 30 s produces many correlated events;
    bootstrapping events treats them as exchangeable, which is closer to right
    for a session-level question like "how many species sightings today?").
    """
    arr = np.asarray([s == species_id for s in species_per_event], dtype=np.int32)
    n = arr.size
    if n == 0:
        return CI(point=0.0, lo=0.0, hi=0.0, method="bootstrap_count", n=0, alpha=alpha)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(B, n))
    draws = arr[idx].sum(axis=1)
    lo, hi = np.quantile(draws, [alpha / 2, 1 - alpha / 2])
    return CI(point=float(arr.sum()), lo=float(lo), hi=float(hi),
              method="bootstrap_count", n=int(n), alpha=alpha)


# ---------------------------------------------------------------------------
# Rate (Bernoulli) CIs
# ---------------------------------------------------------------------------


def rate_ci(
    successes: int,
    trials: int,
    alpha: float = DEFAULT_ALPHA,
) -> CI:
    """Wilson score interval for a binomial proportion.

    Preferred over Normal-approx for small n or rates near 0/1.
    """
    if trials <= 0:
        return CI(point=float("nan"), lo=float("nan"), hi=float("nan"),
                  method="wilson", n=0, alpha=alpha)
    from scipy.stats import norm  # lazy import
    z = norm.ppf(1 - alpha / 2)
    p = successes / trials
    denom = 1 + z * z / trials
    centre = (p + z * z / (2 * trials)) / denom
    halfw  = (z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials))) / denom
    return CI(point=float(p), lo=float(max(0.0, centre - halfw)),
              hi=float(min(1.0, centre + halfw)), method="wilson",
              n=int(trials), alpha=alpha)


# ---------------------------------------------------------------------------
# Aggregation helper for species counts with CIs
# ---------------------------------------------------------------------------


def species_counts_with_ci(
    species_ids: Sequence[str],
    method: str = "poisson",
    B: int = DEFAULT_B,
    alpha: float = DEFAULT_ALPHA,
    seed: int = 0,
) -> list[dict]:
    """Given a flat list of species_id observations, return per-species counts
    with CIs. ``method='poisson'`` uses the exact Poisson CI; ``'bootstrap'``
    uses the event-level bootstrap above.
    """
    from collections import Counter
    counter = Counter(species_ids)
    n_total = sum(counter.values())
    out = []
    for sid, n in counter.most_common():
        if method == "bootstrap":
            ci = bootstrap_count_ci(species_ids, sid, B=B, alpha=alpha, seed=seed)
        else:
            ci = poisson_count_ci(n, alpha=alpha)
        out.append({
            "species_id": sid,
            "count": int(n),
            "ci": ci.as_dict(),
            "share": (n / n_total) if n_total else None,
        })
    return out
