"""G4 deterministic comparisons. These calculations do not validate physiology."""
from itertools import combinations
from fractions import Fraction
import math


CS_DISTANCES = (400, 800, 1000, 1500, 3000, 5000)
WINDOWS = (56, 84, 112, 168)
ALGORITHM_VERSION = "g4-centered-ols-riegel-v1"
SENSITIVITY_LABEL = "Method sensitivity; NOT A 95% PERSONAL CONFIDENCE INTERVAL"


def positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def qualification(provenance):
    """Consume #30 additively; never promote a category or QC verdict to a test."""
    q = provenance.get("qualification") or {}
    supported = q.get("schema_version") == "effort-qualification-v1"
    protocol = isinstance(provenance.get("protocol"), str) and bool(provenance["protocol"].strip())
    test = (supported and provenance.get("verified_maximal") is True and protocol
            and q.get("maximality") == "CONFIRMED" and q.get("rest_state") == "RESTED"
            and q.get("control_state") == "CONTROLLED"
            and q.get("test_evidence_status") == "SUPPORTED_ATTESTATION")
    race = (supported and q.get("race_evidence_status") == "SUPPORTED_ATTESTATION"
            and q.get("race_distance_verification") in {"ATTESTED", "DOCUMENTED"}
            and q.get("race_time_verification") in {"ATTESTED", "DOCUMENTED"}
            and q.get("maximality") != "NOT_MAXIMAL")
    return {"comparison_basis": "SUPPORTED_TEST_ATTESTATION" if test else "SUPPORTED_RACE_ATTESTATION" if race else "UNVERIFIED",
            "credible_maximal_trial": bool(test), "comparison_eligible": bool(test or race),
            "qualification": q or None, "legacy_declaration": provenance.get("verified_maximal", False),
            "reason": None if test or race else "No supported test/race qualification; labels and legacy declarations alone do not establish maximality."}


def overlap(rows):
    groups = {}
    for row in rows:
        groups.setdefault(row["ref"]["activity_id"], []).append(row["id"])
    return {"distinct_activities": len(groups), "observation_count": len(rows),
            "max_activity_fraction": max((len(ids) / len(rows) for ids in groups.values()), default=None),
            "activities": [{"activity_id": aid, "observation_ids": ids, "count": len(ids)} for aid, ids in sorted(groups.items())],
            "independence": "NOT_ESTABLISHED", "reason": "Distances within one workout are not independent maximal tests."}


def ols(rows, policy):
    """Centered distance-on-time OLS, including raw diagnostics for unusable fits."""
    ds = [r["candidate"]["distance_m"] for r in rows]
    ts = [r["candidate"]["elapsed_seconds"] for r in rows]
    result = {"observation_ids": [r["id"] for r in rows], "distances_m": ds,
              "status": "INSUFFICIENT_DATA", "fit_state": "INSUFFICIENT", "reason": None,
              "cs_mps": None, "d_prime_m": None, "r_squared": None, "r_squared_reason": "FIT_UNAVAILABLE",
              "residuals": [], "flags": [], "source_overlap": overlap(rows),
              "units": {"cs": "m/s", "d_prime": "m", "residual": "m", "time": "elapsed seconds"},
              "physiological_validation": "UNVERIFIED", "personal_predictive_validity": "UNTESTED",
              "review_policy": policy, "prescription_authority": False}
    if any(not positive(d) or not positive(t) for d, t in zip(ds, ts)):
        result["reason"] = "NONPOSITIVE_OR_NONFINITE_INPUT"
        return result
    if len(ds) != len(set(ds)):
        result["reason"] = "REPEATED_DISTANCE_SELECT_ONE_OBSERVATION_PER_DISTANCE"
        return result
    if any(d not in CS_DISTANCES for d in ds) or len(ds) < 3:
        result["reason"] = "NEED_THREE_DISTINCT_SUPPORTED_DISTANCES"
        return result
    try:
        # At most six points: exact rational centering of the supplied numeric
        # values is cheap and avoids cancellation and invented zero tolerances.
        # Convert only results to binary64; this does not alter source precision.
        qt, qd = [Fraction(t) for t in ts], [Fraction(d) for d in ds]
        mt, md = sum(qt) / len(qt), sum(qd) / len(qd)
        dt, dd = [t - mt for t in qt], [d - md for d in qd]
        denominator = sum(x * x for x in dt)
        result["centered_sums"] = {"mean_time_s": float(mt), "mean_distance_m": float(md), "time_sum_squares": float(denominator),
                                   "arithmetic": "Exact rational centered sums of supplied numeric values; binary64 output"}
        if not denominator:
            result["reason"] = "SINGULAR_ELAPSED_TIMES"
            return result
        qslope = sum(x * y for x, y in zip(dt, dd)) / denominator
        slope, intercept = float(qslope), float(md - qslope * mt)
        residuals = [d - (slope * t + intercept) for d, t in zip(ds, ts)]
        total = float(sum(x * x for x in dd))
        squared = math.fsum(r * r for r in residuals)
        r2 = 1 - squared / total if total else None
        if not all(math.isfinite(v) for v in [slope, intercept, squared, total, *residuals]) or (r2 is not None and not math.isfinite(r2)):
            raise ArithmeticError
    except (ArithmeticError, ValueError):
        result.pop("centered_sums", None)
        result["reason"] = "NONCOMPUTABLE_NUMERICAL_FIT"
        return result
    result.update(status="PROVISIONAL_ESTIMATE", fit_state="TRAINING_DERIVED_UNVERIFIED", cs_mps=slope,
                  d_prime_m=intercept, r_squared=r2, r_squared_reason=None if r2 is not None else "ZERO_DISTANCE_VARIATION",
                  residuals=[{"observation_id": row["id"], "observed_distance_m": d, "elapsed_seconds": t,
                              "fitted_distance_m": slope * t + intercept, "residual_m": r}
                             for row, d, t, r in zip(rows, ds, ts, residuals)])
    tests = [r for r in rows if qualification(r["candidate"]["provenance"])["credible_maximal_trial"]]
    result["maximal_trial_evidence"] = {"qualified_count": len(tests),
        "distinct_activities": len({r["ref"]["activity_id"] for r in tests}),
        "distinct_durations": len({r["candidate"]["elapsed_seconds"] for r in tests})}
    if all(v >= 3 for v in result["maximal_trial_evidence"].values()):
        result["fit_state"] = "MAXIMAL_TRIAL_COMPARISON_UNVERIFIED"
    if slope <= 0:
        result["flags"].append("NONPOSITIVE_CS")
        result["status"] = "CONFLICTED_EVIDENCE"
    if intercept < 0:
        result["flags"].append("NEGATIVE_D_PRIME_PRESERVED")
    if len(rows) > 1 and result["source_overlap"]["distinct_activities"] == 1:
        result["flags"].append("ALL_INPUTS_SHARE_ONE_ACTIVITY")
    checks = {"d_prime_min_m": intercept, "d_prime_max_m": intercept,
              "max_activity_fraction": result["source_overlap"]["max_activity_fraction"]}
    result["policy_checks"] = {}
    for key, value in checks.items():
        threshold = policy[key]
        violated = threshold is not None and (value < threshold if key == "d_prime_min_m" else value > threshold)
        result["policy_checks"][key] = {"value": value, "threshold": threshold,
            "state": "UNKNOWN" if threshold is None else "REVIEW" if violated else "WITHIN_CONFIGURED_POLICY"}
        if violated:
            result["flags"].append(key.upper() + "_REVIEW")
            result["status"] = "CONFLICTED_EVIDENCE"
    return result


def distribution(fits):
    values = sorted(f["cs_mps"] for f in fits if f["cs_mps"] is not None)
    return {"label": SENSITIVITY_LABEL, "values_mps": values, "count": len(values),
            "minimum_mps": min(values) if values else None, "maximum_mps": max(values) if values else None,
            "range_mps": max(values) - min(values) if values else None}


def critical_speed(rows, policy):
    rows = sorted(rows, key=lambda row: (row["candidate"]["distance_m"], row["id"]))
    primary = ols(rows, policy)
    distinct = len({r["candidate"]["distance_m"] for r in rows}) == len(rows)
    subsets = [ols(list(group), policy) for n in range(3, len(rows) + 1) for group in combinations(rows, n)] if distinct else []
    for fit in subsets:
        fit["original_p02_group"] = fit["distances_m"] == [400, 800, 5000]
    loo = [{"omitted_distance_m": r["candidate"]["distance_m"], "fit": ols(rows[:i] + rows[i + 1:], policy)}
           for i, r in enumerate(rows)] if distinct and len(rows) >= 4 else []
    subset_distribution = distribution(subsets)
    slope = primary["cs_mps"]
    loo_delta = max((abs(x["fit"]["cs_mps"] - slope) / abs(slope) for x in loo
                    if x["fit"]["cs_mps"] is not None), default=None) if slope else None
    subset_range = subset_distribution["range_mps"] / abs(slope) if slope and subset_distribution["range_mps"] is not None else None
    checks = {}
    for key, value in (("max_loo_relative_cs_change", loo_delta), ("max_subset_relative_cs_range", subset_range)):
        threshold = policy[key]
        violated = value is not None and threshold is not None and value > threshold
        checks[key] = {"value": value, "threshold": threshold,
                       "state": "NOT_AVAILABLE" if value is None else "UNKNOWN" if threshold is None else "REVIEW" if violated else "WITHIN_CONFIGURED_POLICY"}
        if violated:
            primary["flags"].append(key.upper() + "_REVIEW")
            primary["status"] = "CONFLICTED_EVIDENCE"
    return {"primary": primary, "subsets": subsets, "subset_distribution": subset_distribution,
            "leave_one_distance_out": {"state": "AVAILABLE" if loo else "NOT_AVAILABLE",
                "reason": None if loo else "At least four distinct distances are required; every retained fit needs at least three.",
                "fits": loo, "distribution": distribution([x["fit"] for x in loo])}, "sensitivity_policy_checks": checks}


def riegel(row, target, k, policy):
    c = row["candidate"]
    q = qualification(c["provenance"])
    result = {"source_observation_id": row["id"], "source_distance_m": c["distance_m"], "target_distance_m": target,
              "k": k, "elapsed_seconds": None, "status": "PROVISIONAL_ESTIMATE",
              "comparison_state": "PREDICTION_COMPARISON" if q["comparison_eligible"] else "EXPLORATORY",
              "qualification": q, "personal_predictive_validity": "UNTESTED", "prescription_authority": False,
              "uncertainty": "No calibrated personal prediction interval; source performance may be nonmaximal.",
              "reason": None}
    try:
        ratio = target / c["distance_m"]
        value = c["elapsed_seconds"] * ratio ** k
        if not positive(value) or not positive(ratio):
            raise ArithmeticError
    except (ArithmeticError, ValueError):
        result.update(status="INSUFFICIENT_DATA", comparison_state="UNAVAILABLE", reason="NONCOMPUTABLE_POWER_LAW")
        return result
    threshold = policy["far_extrapolation_ratio"]
    result.update(elapsed_seconds=value, distance_ratio=ratio,
                  extrapolation="LONGER_DISTANCE_EXTRAPOLATION" if ratio > 1 else "SHORTER_DISTANCE_COMPARISON" if ratio < 1 else "SAME_DISTANCE",
                  far_extrapolation_review={"threshold": threshold, "state": "UNKNOWN" if threshold is None else "REVIEW" if ratio > threshold else "WITHIN_CONFIGURED_POLICY"})
    if ratio > 1:
        result["uncertainty"] += " Extrapolation extends beyond this source effort; uncertainty grows with distance ratio. No numerical accuracy is established."
    return result
