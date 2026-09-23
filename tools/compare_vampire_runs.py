"""Compare observed Vampire Survivors runs without controlling the game or API.

Usage: python tools/compare_vampire_runs.py BASELINE_DIRECTORY RUN_DIRECTORY
Add --json for structured output. A live run may end in an incomplete JSONL
record; only that unfinished final line is ignored. Logs are never modified.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import statistics
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vampire_agent.policy import DIRECTIONS, EXPERIENCE_KINDS, prepare_decision


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def read_live_jsonl(path):
    """Tolerate an interrupted final write, not corrupted complete records."""
    lines = path.read_bytes().splitlines(keepends=True)
    records, ignored = [], False
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            if index == len(lines) - 1 and not line.endswith(b"\n"):
                ignored = True
                break
            raise ValueError(f"Invalid complete JSONL record at {path}:{index + 1}") from exc
        if not isinstance(record, dict):
            raise ValueError(f"Expected JSON object at {path}:{index + 1}")
        records.append(record)
    return records, ignored


def read_optional_json(path):
    if not path.exists():
        return {}, None
    try:
        value = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}, f"{path.name} is incomplete or invalid; using observed log data"
    if not isinstance(value, dict):
        return {}, f"{path.name} is not an object; using observed log data"
    return value, None


def recorded_candidates(row, exclude_incoming=False):
    """Prefer historical geometry; recompute only missing candidate fields."""
    required = {"minimum_enemy_clearance", "endpoint_experience_distance"}
    for field in ("candidate_geometry", "candidates"):
        candidates = row.get(field) or {}
        if row.get("choice") in candidates and all(
            isinstance(candidate, dict) and required <= candidate.keys()
            for candidate in candidates.values()
        ):
            return candidates, False
    state = row["state"]
    if not exclude_incoming:
        # Historical policies navigated toward every observed gem. Preserve that
        # definition when today's geometry recomputes an older missing record.
        state = dict(state, pickups=[{key: value for key, value in pickup.items()
                                     if key != "moving_to_player"}
                                    for pickup in state.get("pickups", [])])
    _, candidates = prepare_decision(state)
    return candidates, True


def compare_run(directory, clearance=.5, minimum_progress=.1, progress_tolerance=.05):
    directory = Path(directory).resolve()
    rows, ignored = read_live_jsonl(directory / "decisions.jsonl")
    summary, warning = read_optional_json(directory / "summary.json")
    metadata, metadata_warning = read_optional_json(directory / "metadata.json")
    explicit_exclusion = metadata.get("xp_navigation_excludes_incoming")
    exclude_incoming = (explicit_exclusion if type(explicit_exclusion) is bool else
                        metadata.get("policy_version") in {"xp-and-facing-v3", "horde-and-weapons-v4"})
    warnings = [value for value in (warning, metadata_warning) if value]
    if ignored:
        warnings.append("Ignored the unfinished final decisions.jsonl record")
    decisions = [row for row in rows if row.get("event") == "decision"]
    combat = [row for row in decisions if row.get("phase") == "combat"
              and row.get("applied") is True
              and finite((row.get("state", {}).get("player") or {}).get("hp"))
              and row["state"]["player"]["hp"] > 0]

    switches = reversals = unknown_transitions = 0
    for before, after in zip(combat, combat[1:]):
        switches += before.get("choice") != after.get("choice")
        a, b = DIRECTIONS.get(before.get("choice")), DIRECTIONS.get(after.get("choice"))
        if a is None or b is None:
            unknown_transitions += 1
        else:
            reversals += sum(x * y for x, y in zip(a, b)) < -.999

    xp = {"decisions_with_observed_xp": 0, "approaching": 0, "retreating": 0,
          "unchanged_within_tolerance": 0, "eligible_opportunities": 0,
          "missed_opportunities": 0, "missed_while_retreating": 0,
          "missed_at_full_health": 0, "geometry_recomputed_decisions": 0,
          "unusable_geometry_decisions": 0, "incoming_xp_observations_excluded": 0}
    for row in combat:
        state = row["state"]
        player = state["player"]
        pickups = [pickup for pickup in state.get("pickups", [])
                   if str(pickup.get("kind", "")).lower() in EXPERIENCE_KINDS]
        if exclude_incoming:
            xp["incoming_xp_observations_excluded"] += sum(pickup.get("moving_to_player") is True for pickup in pickups)
            pickups = [pickup for pickup in pickups if pickup.get("moving_to_player") is not True]
        if not pickups:
            continue
        try:
            positions = [(pickup["x"], pickup["y"]) for pickup in pickups]
            if not all(finite(value) for point in positions + [(player["x"], player["y"])] for value in point):
                raise ValueError("Nonfinite pickup geometry")
            current_distance = min(math.hypot(x - player["x"], y - player["y"])
                                   for x, y in positions)
            candidates, recomputed = recorded_candidates(row, exclude_incoming)
            xp["geometry_recomputed_decisions"] += recomputed
            selected_distance = candidates[row["choice"]]["endpoint_experience_distance"]
            if not finite(selected_distance):
                raise ValueError("No selected XP endpoint distance")
            delta = selected_distance - current_distance
            eligible = any(finite(candidate.get("minimum_enemy_clearance"))
                           and candidate["minimum_enemy_clearance"] >= clearance
                           and finite(candidate.get("endpoint_experience_distance"))
                           and candidate["endpoint_experience_distance"] <= current_distance - minimum_progress
                           for candidate in candidates.values())
        except (KeyError, TypeError, ValueError):
            xp["unusable_geometry_decisions"] += 1
            continue
        xp["decisions_with_observed_xp"] += 1
        xp["approaching"] += delta < -progress_tolerance
        xp["retreating"] += delta > progress_tolerance
        xp["unchanged_within_tolerance"] += abs(delta) <= progress_tolerance
        if eligible:
            xp["eligible_opportunities"] += 1
            if delta >= -progress_tolerance:
                xp["missed_opportunities"] += 1
                xp["missed_while_retreating"] += delta > progress_tolerance
                xp["missed_at_full_health"] += (finite(player.get("max_hp"))
                                                and player["hp"] >= player["max_hp"] - .01)
    xp["missed_opportunity_fraction"] = (xp["missed_opportunities"] / xp["eligible_opportunities"]
                                         if xp["eligible_opportunities"] else None)

    observations = []
    for row in rows:
        state = row.get("state") or {}
        player = state.get("player") or {}
        if finite(state.get("elapsed")) and finite(player.get("level")) and player["level"] > 0:
            observations.append((state["elapsed"], player))
    if finite(summary.get("game_elapsed_seconds")) and isinstance(summary.get("final_player"), dict):
        observations.append((summary["game_elapsed_seconds"], summary["final_player"]))
    milestones = {}
    first_damage = None
    for elapsed, player in observations:
        if finite(player.get("level")):
            milestones.setdefault(str(int(player["level"])), elapsed)
        if (first_damage is None and finite(player.get("hp")) and finite(player.get("max_hp"))
                and player["max_hp"] > 0 and player["hp"] < player["max_hp"] - .01):
            first_damage = elapsed
    observed_elapsed = observations[-1][0] if observations else None
    final_player = observations[-1][1] if observations else {}
    xp_observations = [(elapsed, player["xp"]) for elapsed, player in observations if finite(player.get("xp"))]
    xp_gain = xp_span = xp_per_minute = None
    if xp_observations:
        if any(t1 < t0 or xp1 < xp0 for (t0, xp0), (t1, xp1) in zip(xp_observations, xp_observations[1:])):
            warnings.append("Observed game clock or XP counter decreased; XP/minute omitted")
        else:
            xp_span = xp_observations[-1][0] - xp_observations[0][0]
            xp_gain = xp_observations[-1][1] - xp_observations[0][1]
            xp_per_minute = 60 * xp_gain / xp_span if xp_span > 0 else None
    latest_metrics = next((row["metrics"] for row in reversed(rows) if isinstance(row.get("metrics"), dict)), {})
    totals = dict(latest_metrics)
    for key in ("requests", "applied_actions", "input_tokens", "output_tokens", "estimated_cost_usd", "unknown_usage_requests"):
        if key in summary:
            totals[key] = summary[key]
    latencies = [row["judgment"]["latency_ms"] for row in decisions
                 if finite((row.get("judgment") or {}).get("latency_ms"))]
    if finite(summary.get("median_latency_ms")):
        latency, latency_source = summary["median_latency_ms"], "completed summary"
    else:
        latency = statistics.median(latencies) if latencies else None
        latency_source = "logged valid judgments so far; excludes errors without latency"
    return {
        "directory": str(directory), "name": directory.name,
        "status": "complete" if summary else "partial (no complete summary)",
        "outcome": summary.get("outcome"), "policy_version": metadata.get("policy_version", "unrecorded"),
        "stage": metadata.get("stage"),
        "xp_pickup_definition": "excludes incoming XP" if exclude_incoming else "all observed XP",
        "xp_navigation_excludes_incoming": exclude_incoming,
        "records_read": len(rows), "ignored_partial_last_line": ignored, "warnings": warnings,
        "game_elapsed_seconds": observed_elapsed, "final_observed_level": final_player.get("level"),
        "level_first_observed_seconds": milestones, "first_observed_hp_loss_seconds": first_damage,
        "xp_gained_over_observed_window": xp_gain, "xp_observed_window_seconds": xp_span,
        "xp_per_observed_game_minute": xp_per_minute,
        "alive_applied_combat_decisions": len(combat), "consecutive_applied_transitions": max(0, len(combat) - 1),
        "direction_switches": switches, "exact_reversals": reversals,
        "transitions_with_unknown_direction": unknown_transitions,
        "stay_decisions": sum(row.get("choice") == "stay" for row in combat),
        "xp_opportunities": xp, "accounting": totals, "median_latency_ms": latency,
        "latency_source": latency_source,
    }


def display_value(value, digits=2):
    if value is None:
        return "unknown"
    return f"{value:.{digits}f}" if isinstance(value, float) else str(value)


def print_comparison(result):
    runs = result["runs"]
    print("| Measure | " + " | ".join(run["name"] for run in runs) + " |")
    print("| --- | " + " | ".join("---" for _ in runs) + " |")
    measures = [
        ("Status", lambda r: r["status"]),
        ("Outcome", lambda r: r["outcome"]),
        ("Stage (metadata)", lambda r: r["stage"]),
        ("Policy version", lambda r: r["policy_version"]),
        ("XP opportunity pickup set", lambda r: r["xp_pickup_definition"]),
        ("Observed game seconds", lambda r: r["game_elapsed_seconds"]),
        ("Observed level", lambda r: r["final_observed_level"]),
        ("Level first observed (game seconds)", lambda r: ", ".join(f"L{k}: {v:g}" for k, v in r["level_first_observed_seconds"].items())),
        ("First observed HP loss (game seconds)", lambda r: r["first_observed_hp_loss_seconds"]),
        ("XP / observed game minute", lambda r: r["xp_per_observed_game_minute"]),
        ("Alive applied combat decisions", lambda r: r["alive_applied_combat_decisions"]),
        ("Direction switches / transitions", lambda r: f'{r["direction_switches"]}/{r["consecutive_applied_transitions"]}'),
        ("Exact reversals", lambda r: r["exact_reversals"]),
        ("Stay decisions", lambda r: r["stay_decisions"]),
        ("XP approach / retreat / unchanged", lambda r: " / ".join(str(r["xp_opportunities"][k]) for k in ("approaching", "retreating", "unchanged_within_tolerance"))),
        ("Missed / eligible XP opportunities", lambda r: f'{r["xp_opportunities"]["missed_opportunities"]}/{r["xp_opportunities"]["eligible_opportunities"]}'),
        ("Missed at full HP", lambda r: r["xp_opportunities"]["missed_at_full_health"]),
        ("API requests", lambda r: r["accounting"].get("requests")),
        ("Estimated API cost (USD)", lambda r: display_value(r["accounting"].get("estimated_cost_usd"), 6)),
        ("Median latency (ms)", lambda r: r["median_latency_ms"]),
        ("Geometry recomputed / unusable", lambda r: f'{r["xp_opportunities"]["geometry_recomputed_decisions"]}/{r["xp_opportunities"]["unusable_geometry_decisions"]}'),
    ]
    for label, getter in measures:
        print(f"| {label} | " + " | ".join(display_value(getter(run)) for run in runs) + " |")
    print("\n" + result["interpretation"])
    print("\n" + result["xp_opportunity_definition"])
    for note in result.get("comparability_notes", []):
        print("\n" + note)
    for run in runs:
        if run["status"] != "complete":
            print(f'\n{run["name"]}: latest logged accounting; {run["latency_source"]}.')
        for warning in run["warnings"]:
            print(f'\n{run["name"]}: {warning}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directories", type=Path, nargs="+")
    parser.add_argument("--json", action="store_true", help="Print structured JSON instead of a Markdown table")
    parser.add_argument("--clearance", type=float, default=.5, help="Minimum modeled enemy clearance for an eligible XP approach")
    parser.add_argument("--minimum-progress", type=float, default=.1, help="Required reduction in nearest-XP endpoint distance")
    parser.add_argument("--progress-tolerance", type=float, default=.05, help="Movement in nearest-XP distance treated as unchanged")
    args = parser.parse_args()
    if any(not finite(value) or value < 0 for value in (args.clearance, args.minimum_progress, args.progress_tolerance)):
        parser.error("Geometry thresholds must be finite and nonnegative")
    if args.minimum_progress <= args.progress_tolerance:
        parser.error("Minimum progress must exceed the unchanged-distance tolerance")
    try:
        runs = [compare_run(directory, args.clearance, args.minimum_progress, args.progress_tolerance)
                for directory in args.directories]
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    stages = {str(run["stage"]).strip().casefold() for run in runs if run["stage"]}
    different_stages = True if len(stages) > 1 else (False if all(run["stage"] for run in runs) else None)
    comparability_notes = []
    if different_stages:
        comparability_notes.append("Different stages are recorded in metadata; enemy encounters and progression differ, so these runs cannot isolate a policy effect.")
    elif different_stages is None:
        comparability_notes.append("Stage metadata is missing for at least one run; stage equivalence cannot be established.")
    if len({run["xp_pickup_definition"] for run in runs}) > 1:
        comparability_notes.append("XP-opportunity definitions differ: some runs exclude gems already moving toward the player, while others include all observed gems. Their opportunity counts are not directly equivalent.")
    result = {
        "runs": runs,
        "different_stages": different_stages,
        "comparability_notes": comparability_notes,
        "interpretation": "Descriptive observations from different encountered states, not a controlled causal comparison. Movement and XP-opportunity counts include only applied combat decisions observed while HP > 0. Level times are first observations. XP/minute uses the observed cumulative-XP difference and game-time interval.",
        "xp_opportunity_definition": f"Eligible: an offered direction reduces nearest-XP endpoint distance by at least {args.minimum_progress:g} world units with modeled minimum enemy clearance >= {args.clearance:g}. Missed: selected direction reduces distance by no more than {args.progress_tolerance:g}; retreat: distance increases by more than {args.progress_tolerance:g}. Geometry is approximate and does not guarantee safety. Logged full candidate_geometry is preferred over model candidates; fallback recomputation is counted. Incoming-XP exclusion follows the explicit xp_navigation_excludes_incoming metadata flag when present; otherwise known v3/v4 policies exclude XP flagged moving_to_player=true and older policies include all observed XP.",
    }
    if args.json:
        print(json.dumps(result, indent=2, allow_nan=False))
    else:
        print_comparison(result)


if __name__ == "__main__":
    main()
