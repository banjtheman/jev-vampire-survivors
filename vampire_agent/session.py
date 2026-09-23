"""Bounded live sessions with raw validated judgments and known-cost accounting."""
from dataclasses import asdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import statistics
import time

from vampire_agent.jev import JevClient, JevError
from .accounting import Accounting, PRICE_PER_MILLION
from .bridge import BridgeClient, BridgeError, BridgeRejected
from .policy import DIRECTIONS, INSTRUCTIONS, MENU_INSTRUCTIONS, POLICY_VERSION, PolicyMemory, compact_decision, prepare_decision, menu_decision

# Runtime files belong to the caller's workspace, including after pip install.
ROOT = Path.cwd()
TERMINAL_POSTROLL_SECONDS = 2.0


def validate_limits(seconds, max_calls, max_cost):
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("seconds must be positive and finite")
    if type(max_calls) is not int or max_calls <= 0:
        raise ValueError("max-calls must be a positive integer")
    if type(max_cost) not in (int, float) or not math.isfinite(max_cost) or max_cost <= 0:
        raise ValueError("max-cost must be positive and finite")


def run_session(args):
    validate_limits(args.seconds, args.max_calls, args.max_cost)
    slug = args.session or datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    if re.fullmatch(r"[A-Za-z0-9_-]+", slug) is None:
        raise ValueError("session must contain only letters, numbers, dash and underscore")
    directory = ROOT / ("recordings" if args.record else "runs") / ("vampire-" + slug)
    # Never overwrite an existing run, even if it stopped during initialization.
    directory.mkdir(parents=True, exist_ok=False)
    metadata = {
        "game": "Vampire Survivors", "session": slug, "started_at": datetime.now(timezone.utc).isoformat(),
        "model": "jev-latest", "record": args.record, "capture_fps": 4,
        "policy_version": POLICY_VERSION,
        "xp_navigation_excludes_incoming": True,
        "combat_instructions": INSTRUCTIONS, "menu_instructions": MENU_INSTRUCTIONS,
        "terminal_postroll_seconds": TERMINAL_POSTROLL_SECONDS if args.record else 0,
        "max_seconds": args.seconds, "max_calls": args.max_calls, "max_estimated_cost_usd": args.max_cost,
        "pause_on_stop": getattr(args, "pause_on_stop", True),
        "start_run": getattr(args, "start_run", False),
        "price_usd_per_million_input_tokens": PRICE_PER_MILLION, "output_tokens_free": True,
        "price_source": "https://docs.typesafe.ai/models", "price_checked_at": "2026-09-22",
        "note": "Manual character/stage setup. Estimated cost covers known usage; one in-flight call can cross the limit.",
    }
    (directory / "metadata.json").write_text(json.dumps(metadata, indent=2))
    accounting = Accounting()
    started = time.monotonic()
    clock_origin = started
    deadline = started + args.seconds
    outcome, state, recording, pause_result = "stopped", {}, {}, None
    failures = expired = rejected = 0
    consecutive_errors = 0
    client = None
    last_phase, last_choice = None, None
    memory = PolicyMemory()
    setup_started = False
    last_record_status = started

    def safe_release(bridge):
        try:
            bridge.release()
        except (BridgeError, OSError, ValueError):
            pass

    try:
        client = JevClient(timeout=2.0, max_retries=0)
        with (directory / "decisions.jsonl").open("x") as log, BridgeClient(port=args.port) as bridge:
            def write(record):
                record.update(elapsed_ms=round((time.monotonic() - clock_origin) * 1000, 1), metrics=accounting.snapshot())
                log.write(json.dumps(record, allow_nan=False) + "\n")
                log.flush()

            recording_started = False
            try:
                if args.record:
                    recording = bridge.request("record_start", path=str(directory.resolve()), fps=4)
                    recording_started = True
                    if not isinstance(recording, dict) or recording.get("active") is not True:
                        raise BridgeError("Game recorder did not become active")
                    if isinstance(recording, dict) and all(type(recording.get(key)) in (int, float)
                                                           for key in ("now_ms", "started_at_ms")):
                        clock_origin = time.monotonic() - (recording["now_ms"] - recording["started_at_ms"]) / 1000
                while True:
                    if time.monotonic() >= deadline:
                        outcome = "start_run_unavailable" if getattr(args, "start_run", False) and not setup_started and state.get("phase") == "menu" else "time_limit"
                        if outcome == "start_run_unavailable":
                            metadata["error"] = "No legal start_run offer appeared before the time limit. Select a solo character and stage first."
                        break
                    if accounting.requests >= args.max_calls:
                        outcome = "request_limit"
                        break
                    if accounting.snapshot()["estimated_cost_usd"] >= args.max_cost:
                        outcome = "cost_limit"
                        break
                    if args.record and time.monotonic() - last_record_status >= 5:
                        recording = bridge.request("record_status")
                        last_record_status = time.monotonic()
                        if not isinstance(recording, dict) or not recording.get("active") or recording.get("error"):
                            outcome = "recording_failed"
                            break
                    iteration = time.monotonic()
                    state = bridge.observe()
                    phase = state.get("phase", "menu")
                    if phase != last_phase:
                        write({"event": "phase", "phase": phase, "state": state, "applied": False})
                        print(f"Phase {phase}; {accounting.requests} Jev calls", flush=True)
                        last_phase = phase
                    player = state.get("player") or {}
                    hp, max_hp = player.get("hp"), player.get("max_hp")
                    dead_in_combat = (phase == "combat" and type(hp) in (int, float)
                                      and math.isfinite(hp) and hp <= 0
                                      and type(max_hp) in (int, float) and math.isfinite(max_hp) and max_hp > 0)
                    if phase in ("gameover", "victory") or state.get("terminal") or dead_in_combat:
                        outcome = "gameover" if dead_in_combat else phase
                        if dead_in_combat:
                            # Native phase can remain playing during its death animation.
                            # Preserve the raw observation and make this inference explicit.
                            write({"event": "terminal", "phase": "gameover", "state": state,
                                   "reason": "observed_hp_depleted", "applied": False})
                        if args.record:
                            # Keep the actual game recorder running through the result
                            # screen transition. No decisions or API calls during this tail.
                            safe_release(bridge)
                            time.sleep(TERMINAL_POSTROLL_SECONDS)
                        break
                    if phase == "menu" and getattr(args, "start_run", False) and not setup_started:
                        offers = (state.get("menu") or {}).get("options") or []
                        start_offer = next((offer for offer in offers if isinstance(offer, dict) and offer.get("id") == "start_run"), None)
                        if start_offer is not None:
                            # This is an explicitly requested setup click, never a model decision.
                            setup_record = {"event": "setup", "phase": phase, "state": state,
                                            "action_id": "start_run", "label": start_offer.get("label", "Start selected run"),
                                            "policy": "explicit_setup", "applied": False}
                            try:
                                if time.monotonic() >= deadline:
                                    outcome = "time_limit"
                                    break
                                bridge.act("start_run", state["seq"])
                                setup_started = True
                                setup_record["applied"] = True
                            except BridgeError as exc:
                                setup_record["error"] = str(exc)
                                raise
                            finally:
                                write(setup_record)
                            continue
                    if phase == "combat":
                        context, candidate_geometry = prepare_decision(state)
                        context["previous_action"] = last_choice
                        context["recent_progress"] = memory.observe(state)
                        context, candidates = compact_decision(context, candidate_geometry)
                        instructions = INSTRUCTIONS
                    elif phase in ("levelup", "chest"):
                        safe_release(bridge)
                        context, candidates = menu_decision(state)
                        instructions = MENU_INSTRUCTIONS
                    else:
                        safe_release(bridge)
                        time.sleep(min(.25, max(0, deadline - time.monotonic())))
                        continue
                    if not candidates:
                        time.sleep(min(.25, max(0, deadline - time.monotonic())))
                        continue
                    if time.monotonic() >= deadline:
                        outcome = "time_limit"
                        break
                    accounting.requests += 1
                    record = {
                        "event": "decision", "attempt": accounting.requests, "phase": phase,
                        "decision_started_ms": round((iteration - clock_origin) * 1000, 1),
                        "observation_elapsed_ms": round((iteration - clock_origin) * 1000, 1),
                        "state": state, "decision_state": context, "candidates": candidates, "applied": False,
                    }
                    if phase == "combat":
                        record["candidate_geometry"] = candidate_geometry
                    try:
                        judgment = client.choose(context, candidates, instructions)
                        accounting.add_usage(judgment.usage, judgment.latency_ms)
                        record.update(judgment=asdict(judgment), choice=judgment.choice)
                        metadata["model"] = judgment.model
                        if time.monotonic() >= deadline or (phase == "combat" and time.monotonic() - iteration > .85):
                            record["reason"] = "decision expired"
                            expired += 1
                            safe_release(bridge)
                        else:
                            if phase == "combat":
                                bridge.move(DIRECTIONS[judgment.choice], state["seq"], ttl_ms=500)
                                last_choice = judgment.choice
                                memory.applied(judgment.choice)
                            else:
                                bridge.act(judgment.choice, state["seq"])
                            record["applied"] = True
                            accounting.applied_actions += 1
                        consecutive_errors = 0
                    except JevError as exc:
                        accounting.add_usage(getattr(exc, "usage", None), getattr(exc, "latency_ms", None))
                        record["error"] = str(exc)
                        if getattr(exc, "answer", None) is not None:
                            record["invalid_answer"] = exc.answer
                        failures += 1
                        consecutive_errors += 1
                        safe_release(bridge)
                    except BridgeRejected as exc:
                        rejected += 1
                        record["error"] = str(exc)
                        consecutive_errors += 1
                        safe_release(bridge)
                    except BridgeError as exc:
                        record["error"] = str(exc)
                        write(record)
                        raise
                    write(record)
                    if consecutive_errors >= 3:
                        outcome = "consecutive_failures"
                        break
                    time.sleep(max(0, min(.25 - (time.monotonic() - iteration), deadline - time.monotonic())))
            finally:
                safe_release(bridge)
                if (getattr(args, "pause_on_stop", True) and state.get("phase") == "combat"
                        and outcome not in ("gameover", "victory")):
                    try:
                        pause_result = bridge.request("pause")
                    except (BridgeError, OSError) as exc:
                        pause_result = {"error": str(exc)}
                if recording_started:
                    try:
                        recording = bridge.request("record_stop")
                    except (BridgeError, OSError) as exc:
                        recording = {"error": str(exc)}
    except KeyboardInterrupt:
        outcome = "interrupted"
    except (JevError, BridgeError, OSError, ValueError, KeyError) as exc:
        outcome = "runner_error"
        metadata["error"] = str(exc)
    finally:
        if client is not None:
            client.close()
    summary = {
        "game": "Vampire Survivors", "outcome": outcome, **accounting.snapshot(),
        "duration_seconds": round(time.monotonic() - started, 3),
        "game_elapsed_seconds": state.get("elapsed"), "final_phase": state.get("phase"),
        "final_player": state.get("player"), "api_errors": failures,
        "expired_decisions": expired, "rejected_actions": rejected,
        "median_latency_ms": round(statistics.median(accounting.latencies), 1) if accounting.latencies else None,
        "recording": recording, "pause_result": pause_result, "directory": str(directory),
    }
    if "error" in metadata:
        summary["error"] = metadata["error"]
    (directory / "metadata.json").write_text(json.dumps(metadata, indent=2))
    (directory / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2), flush=True)
    return summary
