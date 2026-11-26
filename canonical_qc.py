"""G1 source quality, with provisional engineering thresholds; no effort extraction."""
from datetime import datetime
import math


ALGORITHM_VERSION = "g1-qc-v2"
DEFAULT_CONFIG_VERSION = "g1-engineering-v1"
DEFAULT_CONFIG = {
    "max_sample_gap_s": 30,
    "stationary_review_s": 10,
    "stationary_quarantine_s": 30,
    "endpoint_bracket_max_gap_s": 15,
    "plausible_speed_max_mps": 12,
    "summary_mismatch_absolute_m": 100,
    "summary_mismatch_fraction": 0.01,
    "min_candidate_samples": 4,
}
STOP_TYPES = {"stop", "stop_all", "stop_disable", "stop_disable_all"}


def validate_config(config):
    def positive_finite(value):
        try:
            return type(value) in (int, float) and math.isfinite(value) and value > 0
        except OverflowError:
            return False

    if set(config) != set(DEFAULT_CONFIG) or not all(positive_finite(v) for v in config.values()):
        raise ValueError("Supply all QC settings as positive finite numbers, with no extra settings.")
    if type(config["min_candidate_samples"]) is not int or config["min_candidate_samples"] < 2:
        raise ValueError("min_candidate_samples must be an integer of at least two.")
    if config["stationary_review_s"] > config["stationary_quarantine_s"]:
        raise ValueError("Stationary quarantine must not precede stationary review.")


def seconds(value):
    return datetime.fromisoformat(value).timestamp() if value else None


def assess(trace, sha, config):
    """Keep file order and values. All ranges refer to original FIT message indices."""
    samples, events, header = trace["samples"]["rows"], trace["events"], trace["header"]
    flags = []

    def flag(code, effect, indices=(), event_indices=(), group=None, calculation=None):
        indices, event_indices = list(indices), list(event_indices)
        refs = [{"sha256": sha, "message_group": "record_mesgs", "index": i} for i in indices]
        refs += [{"sha256": sha, "message_group": "event_mesgs", "index": i} for i in event_indices]
        if group:
            refs.append({"sha256": sha, "message_group": group, "index": 0})
        flags.append({"code": code, "effect": effect, "sample_indices": indices,
                      "sample_times_utc": [samples[i]["timestamp_utc"] for i in indices],
                      "event_indices": event_indices, "raw_references": refs,
                      "calculation": calculation or {}})

    timers = [e for e in events if e["fields"].get("event") == 0]
    ambiguous_events = [e for e in events if e["event"] == "unknown"]
    recognized = [e for e in timers if e["timestamp_utc"] and e["event_type"] in STOP_TYPES | {"start"}]
    event_order_error = any(seconds(b["timestamp_utc"]) < seconds(a["timestamp_utc"])
                            for a, b in zip(recognized, recognized[1:]))
    partial = bool(ambiguous_events) or len(recognized) != len(timers) or event_order_error
    # Repeated transitions cannot demonstrate a complete timer history.
    for a, b in zip(recognized, recognized[1:]):
        if (a["event_type"] == "start") == (b["event_type"] == "start"):
            partial = True
    timer_coverage = "PARTIAL" if partial else "RECORDED" if timers else "NOT_PROVIDED"
    flag("timer_event_coverage", "SUSPECT" if partial else "INFO", range(len(samples)) if partial else (),
         event_indices=[e["event_index"] for e in timers + ambiguous_events],
         calculation={"coverage": timer_coverage, "proves_continuity": False,
                      "reason": "TIMER_EVENTS_" + timer_coverage,
                      "event_order_error": event_order_error,
                      "affected_range_reason": "source-wide review for partial event history" if partial else None})
    for event in timers + ambiguous_events:
        if event["timestamp_utc"] is None:
            flag("invalid_event_clock" if event["fields"].get("timestamp") is not None else "missing_event_timestamp",
                 "SUSPECT", event_indices=[event["event_index"]], calculation={"raw_value": event["fields"].get("timestamp")})
    if event_order_error:
        flag("timestamp_order_error", "SUSPECT", event_indices=[e["event_index"] for e in recognized],
             calculation={"stream": "timer events", "reason": "nonmonotonic timestamps; blocks withheld"})
    # Never reorder contradictory event clocks into apparent continuity.
    boundaries = [] if event_order_error else [
        {"event_index": e["event_index"], "timestamp_utc": e["timestamp_utc"],
         "kind": "resume_or_start" if e["event_type"] == "start" else "confirmed_stop",
         "raw_reference": {"sha256": sha, "message_group": "event_mesgs", "index": e["event_index"]}}
        for e in recognized]
    intervals, stopped = [], []
    lower, left, stop = None, None, None
    running = True  # No initial event means unknown coverage, not a fabricated pause.
    for boundary in boundaries:
        stamp = seconds(boundary["timestamp_utc"])
        if boundary["kind"] == "confirmed_stop":
            if running:
                intervals.append((lower, stamp, left, boundary))
                stop = stamp
            running = False
        else:
            if running and lower is not None:
                intervals.append((lower, stamp, left, boundary))
            if not running:
                stopped.append((stop, stamp))
            lower, left, running = stamp, boundary, True
    if running:
        intervals.append((lower, None, left, None))
    else:
        stopped.append((stop, None))

    times = [seconds(s["timestamp_utc"]) for s in samples]
    distances = [s["distance_m"] for s in samples]

    def crosses_break(first, last):
        return any(first <= seconds(b["timestamp_utc"]) < last if b["kind"] == "confirmed_stop"
                   else first < seconds(b["timestamp_utc"]) <= last for b in boundaries)

    def bracket(stamp):
        before = [i for i, t in enumerate(times) if t is not None and t <= stamp]
        after = [i for i, t in enumerate(times) if t is not None and t >= stamp]
        return sorted(set(before[-1:] + after[:1]))

    for boundary in boundaries:
        flag("pause_event_inside_window" if boundary["kind"] == "confirmed_stop" else "timer_start_boundary", "INFO",
             bracket(seconds(boundary["timestamp_utc"])), [boundary["event_index"]],
             calculation={"timestamp_utc": boundary["timestamp_utc"], "terminates_block": True})

    if header["start_utc"] is None:
        value = header["session"].get("start_time")
        flag("invalid_clock" if value is not None else "missing_header_timestamp",
             "INVALID" if value is not None else "INFO", group="session_mesgs",
             calculation={"field": "start_time", "raw_value": value})
    if header["elapsed_seconds"] is None:
        value = header["session"].get("total_elapsed_time")
        flag("invalid_header_elapsed" if value is not None else "missing_header_elapsed",
             "INVALID" if value is not None else "INFO", group="session_mesgs",
             calculation={"field": "total_elapsed_time", "raw_value": value, "end_basis": header.get("end_basis")})
    if header.get("end_issue") == "END_OUT_OF_RANGE":
        flag("invalid_header_span", "INVALID", group="session_mesgs", calculation={"reason": "END_OUT_OF_RANGE"})
    if header.get("message_timestamp_utc") is None:
        value = header["session"].get("timestamp")
        flag("invalid_summary_message_clock" if value is not None else "missing_summary_message_clock",
             "INFO", group="session_mesgs", calculation={"field": "timestamp", "raw_value": value,
             "meaning": "message written-at only; not used for the session span"})
    for i, sample in enumerate(samples):
        if times[i] is None:
            value = sample["fields"].get("timestamp")
            flag("invalid_clock" if value is not None else "missing_sample_timestamp",
                 "INVALID" if value is not None else "SUSPECT", [i],
                 calculation={"raw_value": value, "reason": "relative_or_invalid" if value is not None else "not_provided"})
        if distances[i] is None:
            flag("missing_sample_distance", "SUSPECT", [i], calculation={"raw_value": sample["fields"].get("distance")})
        elif distances[i] < 0:
            flag("negative_distance", "INVALID", [i], calculation={"distance_m": distances[i]})
    for values, code, key, invalid in ((times, "timestamp_order_error", "delta_s", lambda delta: delta <= 0),
                                       (distances, "distance_reversal", "delta_m", lambda delta: delta < 0)):
        known = [i for i, value in enumerate(values) if value is not None]
        for a, b in zip(known, known[1:]):
            if b > a + 1 and invalid(values[b] - values[a]):
                flag(code, "INVALID", range(a, b + 1), calculation={key: values[b] - values[a], "missing_fields_between": True})
    for i in range(1, len(samples)):
        t0, t1, d0, d1 = times[i - 1], times[i], distances[i - 1], distances[i]
        dt = t1 - t0 if t0 is not None and t1 is not None else None
        dd = d1 - d0 if d0 is not None and d1 is not None else None
        if dt is not None and dt <= 0:
            flag("timestamp_order_error", "INVALID", [i - 1, i], calculation={"delta_s": dt})
        if dd is not None and dd < 0:
            flag("distance_reversal", "INVALID", [i - 1, i], calculation={"delta_m": dd})
        if dt is not None and dt > config["max_sample_gap_s"]:
            split = crosses_break(t0, t1)
            flag("gap_duration_s", "INFO" if split else "SUSPECT", [i - 1, i],
                 calculation={"gap_duration_s": dt, "threshold_s": config["max_sample_gap_s"], "confirmed_boundary": split})
        if dt is not None and dt > 0 and dd is not None and dd / dt > config["plausible_speed_max_mps"]:
            flag("distance_jump", "SUSPECT", [i - 1, i],
                 calculation={"reason": "GPS_OR_DISTANCE_JUMP", "delta_m": dd, "delta_s": dt,
                              "speed_mps": dd / dt, "threshold_mps": config["plausible_speed_max_mps"]})
    first = 0
    while first < len(samples):
        last = first
        while (last + 1 < len(samples) and distances[first] is not None and distances[last + 1] == distances[first]
               and times[last] is not None and times[last + 1] is not None and times[last + 1] > times[last]):
            last += 1
        duration = times[last] - times[first] if times[first] is not None and times[last] is not None else 0
        if duration >= config["stationary_review_s"]:
            confirmed = any(a <= times[first] and (b is None or times[last] <= b) for a, b in stopped)
            flag("stationary_plateau_s", "INFO" if confirmed else "SUSPECT", range(first, last + 1),
                 calculation={"reason": "CONFIRMED_STOP" if confirmed else "AMBIGUOUS_STATIONARY",
                              "duration_s": duration, "review_s": config["stationary_review_s"],
                              "quarantine_s": config["stationary_quarantine_s"],
                              "quarantined": duration >= config["stationary_quarantine_s"] and not confirmed,
                              "fabricated_stop": False})
        first = last + 1
    if len(samples) >= 2 and distances[0] is not None and distances[-1] is not None and header["distance_m"] is not None:
        total = distances[-1] - distances[0]
        difference = abs(total - header["distance_m"])
        tolerance = max(config["summary_mismatch_absolute_m"], config["summary_mismatch_fraction"] * header["distance_m"])
        if difference > tolerance:
            flag("inconsistent_total_distance", "SUSPECT", [0, len(samples) - 1], group="session_mesgs",
                 calculation={"trace_total_m": total, "header_total_m": header["distance_m"],
                              "difference_m": difference, "tolerance_m": tolerance})
    sufficient = sum(t is not None and d is not None for t, d in zip(times, distances)) >= 2
    if not sufficient:
        flag("insufficient_trace", "INSUFFICIENT", range(len(samples)),
             calculation={"reason": "Fewer than two timestamped distance records; summary retained."})
    state = ("INVALID" if any(f["effect"] == "INVALID" for f in flags) else "INSUFFICIENT" if not sufficient else
             "SUSPECT" if any(f["effect"] == "SUSPECT" for f in flags) else "VALID")
    blocks = []
    if not event_order_error:
        for lower, upper, left, right in intervals:
            indices = [i for i, t in enumerate(times) if t is not None and (lower is None or t >= lower)
                       and (upper is None or t <= upper) and
                       not (right and right["kind"] == "resume_or_start" and t == upper)]
            if indices:
                blocks.append({"block_index": len(blocks), "sample_indices": indices,
                               "start_utc": samples[indices[0]]["timestamp_utc"],
                               "end_utc": samples[indices[-1]]["timestamp_utc"],
                               "left_boundary": left, "right_boundary": right})
    included = {i for block in blocks for i in block["sample_indices"]}
    return {"state": state, "timer_event_coverage": timer_coverage, "flags": flags, "boundaries": boundaries,
            "blocks": blocks, "unassigned_sample_indices": [i for i in range(len(samples)) if i not in included],
            "candidate_rules_applied": False,
            "limitations": ["VALID is engineering QC, not proof of continuous movement or an authoritative PB.",
                            "Endpoint brackets and minimum candidate samples are evaluated only by G2.",
                            "Blocks retain original indices; flagged ranges are not repaired or erased."]}
