"""Deterministic, distribution-free population-median confidence summaries.

For sorted iid observations, [X_(k), X_(n-k+1)] covers a population median
with probability at least 1 - 2 * P(Binomial(n, .5) < k). Ties are conservative.
Exact rational tail arithmetic selects the narrowest equal-tailed rank interval.
Bonferroni allocates half the total error to each side; interval subtraction
then covers the population-median change without assuming independent sides.
Diagnostics leave the raw-MAD gate unchanged unless confidence gating is requested.
"""

from fractions import Fraction
import math


def validate_confidence_level(level: float | None) -> None:
    if level is not None and (isinstance(level, bool) or not isinstance(level, (int, float))
                              or not 0 < level < 1):
        raise ValueError("confidence_level must be a finite number strictly between 0 and 1")


def _coverage_fields(coverage: Fraction) -> dict:
    return {"coverage_lower_bound": float(coverage),
            "coverage_lower_bound_fraction": str(coverage)}


def _median_interval(values: list, target: Fraction) -> tuple[dict, Fraction]:
    values = sorted(values)
    n, tail, term = len(values), 0, 1
    scale = 1 << n
    selected = None
    # term is C(n, rank-1); incrementing rank shrinks the interval.
    for rank in range(1, (n + 1) // 2 + 1):
        tail += term
        coverage = Fraction(scale - 2 * tail, scale)
        if coverage < target:
            break
        selected = rank, coverage
        term = term * (n - rank + 1) // rank
    # The caller enforces the finite-interval minimum before reaching here.
    assert selected is not None
    rank, coverage = selected
    return {"interval": [values[rank - 1], values[n - rank]],
            "order_statistic_ranks": [rank, n - rank + 1],
            **_coverage_fields(coverage)}, coverage


def _difference(a: float, b: float, *, lower: bool) -> float:
    """Round interval subtraction outward when binary floating arithmetic rounds inward."""
    exact = Fraction(a) - Fraction(b)
    if isinstance(a, int) and isinstance(b, int):
        return a - b
    try:
        value = float(exact)
    except OverflowError:
        return math.inf if exact > 0 else -math.inf
    if (lower and value > exact) or (not lower and value < exact):
        return math.nextafter(value, -math.inf if lower else math.inf)
    return value


def _metric_confidence(metric: dict, settings: dict, target: Fraction, invalid: bool) -> dict:
    result = {"status": "inconclusive", "reasons": [], "baseline": None, "current": None,
              "delta_interval": None, "adverse_delta_interval": None, "coverage_lower_bound": None,
              "coverage_lower_bound_fraction": None}
    if invalid:
        result.update(status="invalid", reasons=["invalid gate input/statistics; confidence intervals suppressed"])
        return result
    stats = [metric[side] for side in ("baseline", "current")]
    if any(v["status"] != "valid" for s in stats for v in s["values"]):
        result["reasons"].append("missing metric; confidence intervals never silently drop runs")
    minimum = settings["min_samples_per_side"]
    if any(s["count"] < minimum for s in stats):
        result["reasons"].append(f"at least {minimum} complete valid samples required on each side for this confidence level and policy")
    if result["reasons"]:
        return result
    b, b_coverage = _median_interval([v["value"] for v in stats[0]["values"]], target)
    c, c_coverage = _median_interval([v["value"] for v in stats[1]["values"]], target)
    delta = [_difference(c["interval"][0], b["interval"][1], lower=True),
             _difference(c["interval"][1], b["interval"][0], lower=False)]
    try:
        finite = all(math.isfinite(value) for value in delta)
    except OverflowError:
        finite = False
    if not finite:
        result.update(status="invalid", reasons=["numeric overflow in confidence interval; rescale or correct the inputs"])
        return result
    result.update(status="available", baseline=b, current=c, delta_interval=delta,
                  adverse_delta_interval=delta if metric["direction"] == "lower" else [-delta[1], -delta[0]],
                  **_coverage_fields(b_coverage + c_coverage - 1))
    return result


def add_confidence(report: dict, level: float) -> None:
    """Add requested diagnostics to a completed report, preserving gate decisions."""
    target = (1 + Fraction(level)) / 2
    minimum = 1
    # k=1 has maximal finite coverage, 1 - 2^(1-n).
    while 1 - Fraction(2, 1 << minimum) < target:
        minimum += 1
    settings = {"level": level, "method": "exact-binomial-median-bonferroni",
                "min_samples_per_side": max(minimum, report["policy"]["min_samples"]),
                "statistical_min_samples_per_side": minimum, "affects_gate": False,
                "scope": "per group/metric; no simultaneous coverage across groups or metrics",
                "assumptions": "iid observations within each side from a fixed distribution; ties are conservative; matching metadata cannot verify iid"}
    report["confidence"] = settings
    report["limitations"][1] = "Heuristic raw MAD gate; optional confidence intervals are separate diagnostics under iid assumptions"
    # A run with unreadable/invalid metadata could belong to any group. Known
    # bad profiles, however, must not suppress unrelated valid comparisons.
    unassigned = any(run["group_id"] is None for runs in report["runs"].values() for run in runs)
    for group in report["groups"]:
        for metric in group["metrics"]:
            metric["confidence"] = _metric_confidence(metric, settings, target,
                                                      unassigned or group["decision"] == "invalid")


def apply_confidence_gate(report: dict) -> None:
    """Apply validated absolute-only confidence decisions; caller reaggregates.

    Keep the MAD evidence, including invalid input/statistics. An unavailable
    interval never falls back to a MAD pass or regression.
    """
    report["confidence"]["affects_gate"] = True
    report["confidence"]["decision_rule"] = "pass if adverse upper <= tolerance; regression if adverse lower > tolerance; otherwise inconclusive"
    report["limitations"][1] = "Opt-in absolute-tolerance confidence gate under iid assumptions; per group/metric coverage only, no simultaneous or real-site guarantee"
    for group in report["groups"]:
        for metric in group["metrics"]:
            metric["mad_decision"] = metric["decision"]
            metric["mad_reasons"] = metric["reasons"][:]
            confidence = metric["confidence"]
            if metric["decision"] == "invalid":
                continue
            if confidence["status"] != "available":
                metric["decision"] = confidence["status"]
                metric["reasons"] = confidence["reasons"][:]
                continue
            low, high = confidence["adverse_delta_interval"]
            tolerance = metric["tolerance"]
            if high <= tolerance:
                metric["decision"] = "pass"
                metric["reasons"] = ["adverse-change confidence interval upper endpoint <= absolute tolerance"]
            elif low > tolerance:
                metric["decision"] = "regression"
                metric["reasons"] = ["adverse-change confidence interval lower endpoint > absolute tolerance"]
            else:
                metric["decision"] = "inconclusive"
                metric["reasons"] = ["adverse-change confidence interval overlaps the absolute tolerance boundary"]
