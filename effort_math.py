"""G2 reference distance-domain search. No sorting, resampling or moving-time edits."""
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta
from fractions import Fraction
from itertools import chain
import math


ALGORITHM_VERSION = "g2-arrival-departure-v3"
PRECISION = ("Exact rational distance coordinates from the persisted decimal SI values; observed and "
             "shifted coordinates share identity without epsilon snapping. Source values remain unchanged. "
             "Crossing times and eligibility comparisons are rational; public numeric seconds use binary64. "
             "Elapsed ranking rounds to 9 decimal places and UTC rendering rounds half-even to microseconds. "
             "Review identity uses the reported target and UTC interval; rejected UTC bins partition the domain. "
             "Flat eligible open cells use deterministic attained midpoints, not a claim of earliest possible start. "
             "One-sided limits carry their approached cell's QC and window support, never an attained performance.")


def coordinate(value):
    """Preserve the stored decimal value, including across shifted-end subtraction."""
    return value if isinstance(value, Fraction) else Fraction(str(value))


def duration(value):
    """A datetime difference without an intermediate binary64 clock."""
    return Fraction(value.days * 86400 + value.seconds) + Fraction(value.microseconds, 1_000_000)


def positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def stamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Use a timestamp with an explicit UTC offset.")
    return parsed


def key(candidate):
    return round(candidate["elapsed_seconds"], 9), stamp(candidate["start_utc"]), candidate["block_index"]


def interval_key(candidate):
    # Cumulative-distance roundoff must never create a new reviewable identity for
    # the same reported performance. Source/version are scoped by the caller.
    return tuple(candidate[k] for k in ("distance_m", "start_utc", "end_utc", "kind"))


class Block:
    def __init__(self, trace, block, qc, config, global_flags=()):
        self.trace, self.block, self.qc, self.config = trace, block, qc, config
        self.samples = [trace["samples"]["rows"][i] for i in block["sample_indices"]]
        self.origin = stamp(self.samples[0]["timestamp_utc"])
        self.clocks = [duration(stamp(s["timestamp_utc"]) - self.origin) for s in self.samples]
        self.times = [float(t) for t in self.clocks]
        self.distances = [s["distance_m"] for s in self.samples]
        if (any(type(d) not in (int, float) or not math.isfinite(d) or d < 0 for d in self.distances)
                or any(b <= a for a, b in zip(self.times, self.times[1:]))
                or any(b < a for a, b in zip(self.distances, self.distances[1:]))):
            raise ValueError("Contradictory or incomplete block; original records were not repaired.")
        self.coordinates = [coordinate(d) for d in self.distances]
        self.flags = [*qc["flags"], *global_flags]
        self.flag_ranges = []
        self.uncertain_index_ranges = []
        for flag in self.flags:
            ts = [duration(stamp(t) - self.origin) for t in flag.get("sample_times_utc", ()) if t]
            self.flag_ranges.append((min(ts), max(ts)) if ts else None)
            raw = flag.get("sample_indices", ())
            self.uncertain_index_ranges.append((min(raw), max(raw)) if raw and len(ts) < len(raw) else None)

    def crossing(self, x, role, side=0):
        """A first arrival, L last departure, or the adjacent increasing-edge limit."""
        ds, x = self.coordinates, coordinate(x)
        if x < ds[0] or x > ds[-1]:
            raise ValueError("Requested distance lies outside this running block.")
        first, after = bisect_left(ds, x), bisect_right(ds, x)
        if first != after:
            if side < 0:
                if first == 0:
                    return None
                left, right = first - 1, first
            elif side > 0:
                if after == len(ds):
                    return None
                left, right = after - 1, after
            else:
                left = right = first if role == "arrival" else after - 1
        else:
            left, right = first - 1, first
        fraction = Fraction(0) if left == right else (x - ds[left]) / (ds[right] - ds[left])
        t = self.clocks[left] + fraction * (self.clocks[right] - self.clocks[left])
        return {"requested_distance_m": float(x), "distance_m": float(x), "distance_coordinate_m": str(x), "role": role,
                "limit_side": side, "fraction": float(fraction), "elapsed_seconds": float(t), "elapsed_coordinate_s": str(t),
                "timestamp_utc": (self.origin + timedelta(microseconds=round(t * 1_000_000 + self.origin.microsecond) - self.origin.microsecond)).isoformat(),
                "sample_indices": [self.samples[left]["record_index"], self.samples[right]["record_index"]],
                "samples": [self.samples[left], self.samples[right]],
                "bracket_gap_s": self.times[right] - self.times[left], "positions": [left, right]}

    def candidate(self, start, distance, side=0, *, whole=False):
        start, distance = coordinate(start), coordinate(distance)
        a, b = self.crossing(start, "departure", side), self.crossing(start + distance, "arrival", side)
        if a is None or b is None:
            return None
        if whole:
            # Whole-activity evidence means the complete recorded sample interval,
            # including leading/trailing stationarity. Header duration is never substituted.
            a, b = self.crossing(start, "arrival"), self.crossing(start + distance, "departure")
        first, last = coordinate(a["elapsed_coordinate_s"]), coordinate(b["elapsed_coordinate_s"])
        if last <= first:
            return None
        indices = [a["positions"][0], b["positions"][1]]
        count = indices[1] - indices[0] + 1
        raw_first, raw_last = a["sample_indices"][0], b["sample_indices"][1]
        flags = []
        for flag, span, uncertain in zip(self.flags, self.flag_ranges, self.uncertain_index_ranges):
            # Summary/event-history issues apply to the source. Interval anomalies
            # apply only where their time interval overlaps the chosen open interior.
            global_issue = flag["code"] in {"timer_event_coverage", "inconsistent_total_distance",
                "summary_distance_disagreement", "unsupported_sport", "invalid_event_clock",
                "missing_event_timestamp"} or not flag.get("sample_indices")
            # Untimed original records are not members of G1 timed blocks, but
            # still belong to the raw support of an interval that spans them.
            uncertain_overlap = uncertain and max(raw_first, uncertain[0]) <= min(raw_last, uncertain[1])
            support_overlap = flag.get("scope") == "RAW_SAMPLE_SUPPORT" and any(
                raw_first <= i <= raw_last for i in flag["sample_indices"])
            if global_issue or uncertain_overlap or support_overlap or (span and (max(first, span[0]) < min(last, span[1]) or
                                        first < span[0] == span[1] < last)):
                flags.append(flag)
        for label, crossing in (("start", a), ("end", b)):
            if crossing["bracket_gap_s"] > self.config["endpoint_bracket_max_gap_s"]:
                flags.append({"code": "interpolation_bracket_gap_s", "effect": "SUSPECT",
                              "sample_indices": crossing["sample_indices"],
                              "sample_times_utc": [s["timestamp_utc"] for s in crossing["samples"]], "calculation": {
                                  "endpoint": label, "gap_s": crossing["bracket_gap_s"],
                                  "threshold_s": self.config["endpoint_bracket_max_gap_s"]}})
        if count < self.config["min_candidate_samples"]:
            flags.append({"code": "insufficient_trace_resolution", "effect": "INSUFFICIENT",
                          "sample_indices": [self.samples[i]["record_index"] for i in range(indices[0], indices[1] + 1)],
                          "sample_times_utc": [self.samples[i]["timestamp_utc"] for i in range(indices[0], indices[1] + 1)],
                          "calculation": {"original_sample_count": count, "minimum": self.config["min_candidate_samples"]}})
        effects = {f["effect"] for f in flags}
        state = next((s for s in ("INVALID", "INSUFFICIENT", "SUSPECT") if s in effects), "VALID")
        whole = whole or (a["timestamp_utc"] == self.trace["samples"]["rows"][0]["timestamp_utc"] and
                          b["timestamp_utc"] == self.trace["samples"]["rows"][-1]["timestamp_utc"])
        elapsed = float(last - first)
        if not math.isfinite(elapsed) or elapsed <= 0:
            raise ValueError("Requested interval elapsed time cannot be represented as positive finite seconds.")
        pace, speed = float((last - first) * 1000 / distance), float(distance / (last - first))
        distance = float(distance)
        return {"distance_m": distance, "start_distance_m": a["distance_m"], "end_distance_m": b["distance_m"],
                "start_elapsed_seconds": float(first), "end_elapsed_seconds": float(last),
                "start_utc": a["timestamp_utc"], "end_utc": b["timestamp_utc"],
                "elapsed_seconds": elapsed, "elapsed_coordinate_s": str(last - first), "pace_seconds_per_km": pace,
                "mean_speed_mps": speed, "block_index": self.block["block_index"],
                "computed_quality_state": state, "flags": flags, "original_sample_count": count,
                "start": a, "end": b, "attainment": "ONE_SIDED_LIMIT" if side else "ATTAINED",
                "kind": "WHOLE_ACTIVITY" if whole else "CONTINUOUS_SEGMENT",
                "timer_event_coverage": self.qc["timer_event_coverage"],
                "provenance": {"category": "TRAINING_FAST" if state == "VALID" else "EASY_OR_UNKNOWN", "tags": [], "verified_maximal": False,
                               "planned_workout": None, "inferred_pattern": None, "user_correction": None}}

    def distance_at(self, t):
        """Forward distance at an exact clock, used only to partition the domain."""
        if t < self.clocks[0] or t > self.clocks[-1]:
            return None
        i = bisect_left(self.clocks, t)
        if self.clocks[i] == t:
            return self.coordinates[i]
        fraction = (t - self.clocks[i - 1]) / (self.clocks[i] - self.clocks[i - 1])
        return self.coordinates[i - 1] + fraction * (self.coordinates[i] - self.coordinates[i - 1])

    def in_window(self, candidate, min_end):
        return min_end is None or coordinate(candidate["end"]["elapsed_coordinate_s"]) >= duration(stamp(min_end) - self.origin)

    def starts(self, distance, min_end=None, rejected=()):
        ds, distance = self.coordinates, coordinate(distance)
        lo, hi = ds[0], ds[-1] - distance
        if hi < lo:
            return []
        starts = {lo, hi}
        starts.update(d for d in ds if lo <= d <= hi)
        starts.update(d - distance for d in ds if lo <= d - distance <= hi)
        def add_clock(t, shift):
            d = self.distance_at(t)
            if d is not None and lo <= d - shift <= hi:
                starts.add(d - shift)
        if min_end is not None:
            add_clock(duration(stamp(min_end) - self.origin), distance)
        # QC is constant inside each open cell, including flags whose known clock
        # is not itself a member of this running block's original samples.
        for span in self.flag_ranges:
            for t in span or ():
                add_clock(t, 0)
                add_clock(t, distance)
        # A rejection covers a reported UTC interval, whose half-even rendering
        # bin has exact +/-0.5 microsecond boundaries. Partition at those true
        # eligibility boundaries; never probe an arbitrary epsilon near a limit.
        for target, start, end, kind in rejected:
            if coordinate(target) == distance and kind == "CONTINUOUS_SEGMENT":
                for value, shift in ((start, 0), (end, distance)):
                    t = duration(stamp(value) - self.origin)
                    for offset in (Fraction(-1, 2_000_000), Fraction(0), Fraction(1, 2_000_000)):
                        add_clock(t + offset, shift)
        return sorted(starts)

    def flat_representative(self, left, right, distance, candidate=None):
        """A constant objective can be attained only inside an eligible open cell.

        Every endpoint bracket and original-record count is constant inside that
        cell. Its midpoint is deterministic; an open QC domain has no first start.
        """
        c = candidate if candidate is not None else self.candidate((left + right) / 2, distance)
        if c is None:
            return None
        a, b = c["start"]["positions"], c["end"]["positions"]
        def slope(pair):
            i, j = pair
            return (self.clocks[j] - self.clocks[i]) / (self.coordinates[j] - self.coordinates[i])
        if a[0] == a[1] or b[0] == b[1] or slope(a) != slope(b):
            return None
        c["selection"] = {"basis": "FLAT_OPEN_CELL_MIDPOINT", "open_start_domain_m": [float(left), float(right)],
                          "open_start_coordinates_m": [str(left), str(right)],
                          "cell_elapsed_function": "CONSTANT", "representative_attained": True, "earliest_start_attained": False,
                          "start_tie_rule": "EARLIEST_ENUMERATED_REPRESENTATIVE"}
        return c

    def cell_candidates(self, left, right, distance, min_end, rejected):
        """Limits are approached through this cell, not through a boundary alone.

        Original/shifted knots, QC clocks, recency and rejection-bin boundaries
        partition every eligibility change. The exact midpoint witnesses the
        constant support/QC/window state of the open affine cell.
        """
        witness = self.candidate((left + right) / 2, distance)
        if witness is None:
            return
        representative = self.flat_representative(left, right, distance, witness)
        if representative:
            yield representative
        support = {"open_start_coordinates_m": [str(left), str(right)],
                   "witness_start_coordinate_m": witness["start"]["distance_coordinate_m"],
                   "witness_end_coordinate_m": witness["end"]["distance_coordinate_m"],
                   "witness_end_clock_s": witness["end"]["elapsed_coordinate_s"],
                   "start_sample_indices": witness["start"]["sample_indices"],
                   "end_sample_indices": witness["end"]["sample_indices"],
                   "computed_quality_state": witness["computed_quality_state"],
                   "min_end_inclusive_utc": min_end, "recency_eligible": self.in_window(witness, min_end),
                   "review_eligible": interval_key(witness) not in rejected}
        for x, side in ((left, 1), (right, -1)):
            c = self.candidate(x, distance, side)
            if c is not None:
                # This is the QC of the approaching intervals, not an override of
                # the attained boundary candidate, which is evaluated separately.
                c["computed_quality_state"], c["flags"] = witness["computed_quality_state"], witness["flags"]
                c["provenance"]["category"] = witness["provenance"]["category"]
                c["approached_cell"] = {**support, "approach_side": side}
                yield c

    def audit(self, candidate):
        first, last = candidate["start_elapsed_seconds"], candidate["end_elapsed_seconds"]
        a, b = candidate["start"]["positions"][0], candidate["end"]["positions"][1]
        plateaus = []
        i = 0
        while i < len(self.samples):
            j = bisect_right(self.distances, self.distances[i]) - 1
            if j > i and max(first, self.times[i]) < min(last, self.times[j]):
                plateaus.append({"sample_indices": [s["record_index"] for s in self.samples[i:j + 1]],
                                 "start_utc": self.samples[i]["timestamp_utc"], "end_utc": self.samples[j]["timestamp_utc"],
                                 "duration_s": self.times[j] - self.times[i], "distance_m": self.distances[i]})
            i = j + 1
        candidate["audit"] = {"interior_plateaus": plateaus,
            "max_overlapping_sample_gap_s": max((self.times[i + 1] - self.times[i] for i in range(a, b)), default=0),
            "block_left_boundary": self.block["left_boundary"], "block_right_boundary": self.block["right_boundary"],
            "sample_range": [self.samples[a]["record_index"], self.samples[b]["record_index"]],
            "confirmed_boundary_crossed": False}
        return candidate


def prepare(trace, qc, config, global_flags=()):
    blocks, failures = [], []
    for block in qc["blocks"]:
        try:
            blocks.append(Block(trace, block, qc, config, global_flags))
        except (ValueError, TypeError, OverflowError):
            failures.append({"block_index": block["block_index"], "sample_indices": block["sample_indices"],
                             "reason": "Nonmonotone, invalid or missing time/distance; no repair or bridging."})
    return blocks, failures


def extract(trace, qc, config, distances, *, whole_tolerance=0, global_flags=(), min_end=None, rejected=()):
    blocks, failures = prepare(trace, qc, config, global_flags)
    rows = []
    for distance in distances:
        best, flagged, whole_best, internal_best = None, None, None, None
        evaluated, limits = 0, 0
        unattained = None
        for block in blocks:
            xs = block.starts(distance, min_end, rejected)
            probes = (block.candidate(x, distance) for x in xs)
            cells = chain.from_iterable(block.cell_candidates(a, b, distance, min_end, rejected) for a, b in zip(xs, xs[1:]))
            for c in chain(probes, cells):
                if c is None:
                    continue
                side = c["attainment"] == "ONE_SIDED_LIMIT"
                evaluated += not side
                limits += side
                if not block.in_window(c, min_end):
                    continue
                if side:
                    if (c["computed_quality_state"] == "VALID" and c["approached_cell"]["recency_eligible"]
                            and c["approached_cell"]["review_eligible"] and (unattained is None or key(c) < key(unattained))):
                        unattained = c
                    continue
                c.setdefault("selection", {"basis": "EXACT_PARTITION_BOUNDARY", "earliest_start_claimed": False,
                                            "start_tie_rule": "EARLIEST_ENUMERATED_REPRESENTATIVE"})
                valid = c["computed_quality_state"] == "VALID" and interval_key(c) not in rejected
                if valid and (best is None or key(c) < key(best)):
                    best = c
                if not valid and (flagged is None or key(c) < key(flagged)):
                    flagged = c
                all_rows = trace["samples"]["rows"]
                interior = c["start_utc"] != all_rows[0]["timestamp_utc"] or c["end_utc"] != all_rows[-1]["timestamp_utc"]
                if valid and interior and (internal_best is None or key(c) < key(internal_best)):
                    internal_best = c
            span = block.coordinates[-1] - block.coordinates[0]
            entire = block.block["sample_indices"] == list(range(len(trace["samples"]["rows"])))
            if entire and span > 0 and abs(span - coordinate(distance)) <= coordinate(whole_tolerance):
                c = block.candidate(block.distances[0], span, whole=True)
                if (c and c["computed_quality_state"] == "VALID" and interval_key(c) not in rejected
                        and block.in_window(c, min_end)):
                    c["matched_target_distance_m"] = distance
                    c["distance_match_delta_m"] = float(span - coordinate(distance))
                    whole_best = c
        # At a recency cut inside a plateau the constrained infimum need not be
        # attained. Show this explicitly; do not relabel the older first arrival.
        unresolved_limit = unattained and (best is None or key(unattained)[0] < key(best)[0])
        limit_state = "REVIEW_RECENCY_LIMIT" if min_end else "REVIEW_REJECTION_LIMIT" if rejected else "REVIEW_ELIGIBILITY_LIMIT"
        for c in (best, flagged, whole_best, internal_best, unattained if unresolved_limit else None):
            if c:
                next(b for b in blocks if b.block["block_index"] == c["block_index"]).audit(c)
        rows.append({"distance_m": distance, "state": limit_state if unresolved_limit else
                     "VALID" if best else "FLAGGED_CANDIDATE" if flagged else "NOT_CALCULABLE",
                     "best": None if unresolved_limit else best, "flagged_candidate": flagged,
                     "best_whole_activity": whole_best, "best_internal_segment": None if unresolved_limit else internal_best,
                     "attained_candidate": best if unresolved_limit else None,
                     "unattained_limit": unattained if unresolved_limit else None,
                     "candidate_count": evaluated, "one_sided_limit_count": limits})
    return {"distances": rows, "unusable_blocks": failures, "precision_convention": PRECISION}
