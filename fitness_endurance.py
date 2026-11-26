"""Describe completed sessions and original, uninterrupted sample spans."""
from decimal import Decimal, localcontext
from fractions import Fraction
from statistics import median

import effort_math


def inside(value, band):
    return value is not None and Decimal(band["low"]) <= value <= Decimal(band["high"])


def decimal(value):
    return Decimal(str(value)) if value is not None else None


def run_rating(notes):
    ratings = [r for r in notes if r["kind"] == "post_run" and "rpe" in r["ratings"]]
    if len(ratings) != 1:
        return None  # Conflicting sources require an explicit run context choice.
    return {**ratings[0]["ratings"]["rpe"], "context_version_id": ratings[0]["version_id"]}


def session_inputs(activity, trace, obs, feedback):
    source_ids = [activity["activity_id"], *activity["identity"]["merged_activity_ids"]]
    annotations = [r for r in obs if r["activity_id"] in source_ids]
    supplied = next((r for r in annotations if r["canonical_version_id"] == activity["canonical_version_id"]), {})
    notes = [r for r in feedback if r.get("activity_id") in source_ids]
    rpe = supplied.get("rpe") or run_rating(notes)
    hr = supplied.get("mean_hr_bpm")
    hr_method = "RUN_CONTEXT" if hr else None
    if hr is None and trace:
        recorded = [decimal(s["heart_rate_bpm"]) for s in trace["trace"]["samples"]["rows"] if s["heart_rate_bpm"] is not None]
        if recorded:
            hr, hr_method = str(sum(recorded) / len(recorded)), "MEAN_OF_RECORDED_FIT_SAMPLES"
    return {"activity_id": activity["activity_id"], "canonical_version_id": activity["canonical_version_id"],
            "event_end_utc": activity["timing"]["end_utc"], "duration_s": activity["measures"]["elapsed_seconds"],
            "distance_m": activity["measures"]["distance_m"], "route": supplied.get("route"),
            "conditions": supplied.get("conditions"), "grade_percent": supplied.get("grade_percent"),
            "hr_bpm": hr, "hr_method": hr_method, "rpe": rpe, "context_id": supplied.get("id"),
            "context_source": supplied.get("source_reference"),
            "superseded_canonical_context_ids": [r["id"] for r in annotations if r is not supplied],
            "source_refs": activity["raw_sources"]}


def comparable(rows, descriptor):
    result = {"descriptor": descriptor, "status": "UNCONFIGURED", "sample_count": 0, "comparison_count": 0,
              "median_pace_s_per_km": None, "matched": [], "excluded": [], "changes_s_per_km": [],
              "method": "Match exact route and conditions, inclusive duration, grade and configured HR/RPE bands. Use completed session elapsed time and distance. Changes are chronological."}
    if descriptor is None:
        return result
    for row in sorted(rows, key=lambda r: (r["event_end_utc"], r["activity_id"])):
        reasons = []
        for name in ("route", "conditions"):
            if row[name] != descriptor[name]:
                reasons.append(name.upper() + ("_MISSING" if row[name] is None else "_DIFFERS"))
        for name in ("duration_s", "grade_percent", "hr_bpm"):
            if descriptor[name] and not inside(decimal(row[name]), descriptor[name]):
                reasons.append(name.upper() + ("_MISSING" if row[name] is None else "_OUTSIDE_BAND"))
        if descriptor["rpe"]:
            rating = row["rpe"]
            if not rating:
                reasons.append("RPE_MISSING")
            elif rating["scale_version"] != descriptor["rpe"]["scale_version"]:
                reasons.append("INCOMPATIBLE_RPE_SCALE")
            elif not inside(decimal(rating["value"]), descriptor["rpe"]["values"]):
                reasons.append("RPE_OUTSIDE_BAND")
        distance, duration = decimal(row["distance_m"]), decimal(row["duration_s"])
        if not distance or not duration:
            reasons.append("POSITIVE_TIME_AND_DISTANCE_REQUIRED")
        if reasons:
            result["excluded"].append({**row, "reasons": reasons})
        else:
            result["matched"].append({**row, "pace_s_per_km": str(duration * 1000 / distance)})
    matches = result["matched"]
    paces = [Decimal(m["pace_s_per_km"]) for m in matches]
    result.update(status="INSUFFICIENT_DATA" if not paces else "ANECDOTAL" if len(paces) <= 2 else "REPEATED_OBSERVATIONS",
                  sample_count=len(paces), comparison_count=max(len(paces) - 1, 0),
                  median_pace_s_per_km=str(median(paces)) if paces else None,
                  changes_s_per_km=[str(b - a) for a, b in zip(paces, paces[1:])])
    return result


def segment(trace, block, start, end, row):
    distance = block.coordinates[end] - block.coordinates[start]
    if distance <= 0:
        return None
    c = block.candidate(block.coordinates[start], distance)
    if not c or c["computed_quality_state"] != "VALID" or effort_math.interval_key(c) in {tuple(v) for v in trace["rejected_intervals"]}:
        return None
    a, b = c["start"]["positions"][0], c["end"]["positions"][1]
    samples = block.samples[a:b + 1]
    hr = [decimal(s["heart_rate_bpm"]) for s in samples if s["heart_rate_bpm"] is not None]
    elevations = [s["elevation_m"] for s in samples]
    return {"activity_id": row["activity_id"], "canonical_version_id": trace["canonical_version_id"],
            "source_sha256": trace["source_sha256"], "qc_version_id": trace["qc_version_id"],
            "extraction_version_id": trace["extraction_version_id"], "block_index": c["block_index"],
            "record_indices": [samples[0]["record_index"], samples[-1]["record_index"]], "start_utc": c["start_utc"], "end_utc": c["end_utc"],
            "elapsed_seconds": c["elapsed_seconds"], "elapsed_coordinate_s": c["elapsed_coordinate_s"],
            "distance_coordinate_m": str(distance), "preceding_distance_coordinate_m": c["start"]["distance_coordinate_m"],
            "pace_s_per_km": c["pace_seconds_per_km"], "hr_mean_bpm": str(sum(hr) / len(hr)) if hr else None,
            "hr_present_samples": len(hr), "sample_count": len(samples),
            "elevation_start_m": elevations[0], "elevation_end_m": elevations[-1],
            "elevation_present_samples": sum(v is not None for v in elevations),
            "session_rpe": row["rpe"], "trace_status": c["computed_quality_state"]}


def qualified_spans(block, *, pace=None, after=None):
    # A descriptor covers consecutive positive-distance raw sample edges. Never
    # bridge a suspect raw record, a stationary edge, or a stop between G1 blocks.
    first = None
    tainted = {i for f in block.flags if f["effect"] in {"INVALID", "SUSPECT", "INSUFFICIENT"}
               for i in f.get("sample_indices", ())}
    for i in range(len(block.samples) - 1):
        d = block.coordinates[i + 1] - block.coordinates[i]
        valid = d > 0 and (after is None or block.coordinates[i] >= after)
        valid = valid and all(s["record_index"] not in tainted for s in block.samples[i:i + 2])
        if valid and pace is not None:
            rate = (block.clocks[i + 1] - block.clocks[i]) * 1000 / d
            valid = Fraction(pace["low"]) <= rate <= Fraction(pace["high"])
        if valid and first is None:
            first = i
        if first is not None and not valid:
            yield first, i
            first = None
    if first is not None:
        yield first, len(block.samples) - 1


def segments(rows, traces, config):
    sustained, late, missing = [], [], []
    pace, threshold = config["sustained_pace_s_per_km"], config["late_after_m"]
    for row in rows:
        trace = traces.get(str(row["activity_id"]))
        if not trace:
            missing.append({"activity_id": row["activity_id"], "reason": "No eligible selected continuous trace."})
            continue
        if pace is None and threshold is None:
            continue
        blocks, failures = effort_math.prepare(trace["trace"], trace["qc"], trace["config"], trace["global_flags"])
        missing.extend({"activity_id": row["activity_id"], **f} for f in failures)
        for block in blocks:
            for parameter, destination in (({"pace": pace}, sustained), ({"after": Fraction(threshold) if threshold else None}, late)):
                if next(iter(parameter.values())) is None:
                    continue
                for a, b in qualified_spans(block, **parameter):
                    value = segment(trace, block, a, b, row)
                    if value:
                        destination.append(value)
    best = max(sustained, key=lambda s: (Fraction(s["elapsed_coordinate_s"]), -s["activity_id"]), default=None)
    return {"sustained": {"descriptor_s_per_km": pace, "status": "UNCONFIGURED" if not pace else "SUPPORTED_OBSERVATION" if best else "INSUFFICIENT_DATA",
                          "longest_by_elapsed": best, "qualifying_spans": sustained,
                          "method": "Longest elapsed span of consecutive increasing, QC-eligible recorded edges, each within the inclusive configured pace range."},
            "late_blocks": {"after_recorded_m": threshold, "status": "UNCONFIGURED" if not threshold else "SUPPORTED_OBSERVATION" if late else "INSUFFICIENT_DATA",
                            "blocks": late, "method": "Continuous sample-aligned spans beginning at or beyond the specified cumulative distance; session RPE is identified separately from block HR."},
            "unavailable_traces": missing}


def calculate(training, traces, observations, records, config):
    with localcontext() as ctx:
        ctx.prec = 128
        rows = [session_inputs(a, traces.get(str(a["activity_id"])), observations, records) for a in training["activities"]]
        windows = [{"days": w["days"], "longest": w["longest"], "elapsed_seconds": w["measures"]["elapsed_seconds"],
                    "timer_seconds": w["measures"]["timer_seconds"], "coverage": w["coverage"],
                    "processing": w["processing"], "canonical_versions": w["canonical_version_ids"]} for w in training["longest"]]
        return {"windows": windows, "comparable_effort": comparable(rows, config["comparison"]),
                **segments(rows, traces, config), "session_evidence": rows}
