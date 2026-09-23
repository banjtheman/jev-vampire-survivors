import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

from vampire_agent.jev import JevClient, JevError, load_env
from .bridge import BridgeClient, BridgeError
from .policy import INSTRUCTIONS, prepare_decision, demo_state
from .session import ROOT, run_session, validate_limits


def main(argv=None):
    parser = argparse.ArgumentParser(description="Experimental Jev controller for Vampire Survivors")
    parser.add_argument("--env", type=Path, default=ROOT / ".env")
    parser.add_argument("--port", type=int, default=4244)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("observe", help="Read one game observation; no Jev call")
    commands.add_parser("release", help="Release injected movement")
    demo = commands.add_parser("demo", help="Show synthetic decision input; completely offline by default")
    demo.add_argument("--live", action="store_true", help="Make exactly one paid Jev call on synthetic data")
    for command, record in (("play", False), ("run", True)):
        live = commands.add_parser(command, help="Play combat and offered upgrades" + (" and record frames" if record else ""))
        live.add_argument("--seconds", type=float, default=60)
        live.add_argument("--max-calls", type=int, default=120)
        live.add_argument("--max-cost", type=float, default=.02, help="Stop after known input usage reaches this estimated USD cost")
        live.add_argument("--session", help="Unique session name (letters, digits, dash, underscore)")
        live.add_argument("--start-run", action="store_true",
                          help="Once, press the exact start_run offer for the already selected character/stage; setup is not a Jev decision")
        live.add_argument("--no-pause-on-stop", dest="pause_on_stop", action="store_false",
                          help="Leave combat running when this bounded session stops (default: pause)")
        live.set_defaults(record=record)
    args = parser.parse_args(argv)
    try:
        if args.command in ("play", "run"):
            validate_limits(args.seconds, args.max_calls, args.max_cost)
            load_env(args.env)
            result = run_session(args)
            if result["outcome"] == "interrupted":
                return 130
            return 1 if result["outcome"] in ("runner_error", "consecutive_failures", "recording_failed", "start_run_unavailable") else 0
        if args.command == "demo":
            state, candidates = prepare_decision(demo_state())
            result = {"synthetic_state": True, "state": state, "candidates": candidates}
            if args.live:
                load_env(args.env)
                client = JevClient(timeout=2.0, max_retries=0)
                try:
                    result["judgment"] = asdict(client.choose(state, candidates, INSTRUCTIONS))
                finally:
                    client.close()
            else:
                result["note"] = "Offline demonstration; no model choice or API request. Add --live for one Jev call."
            print(json.dumps(result, indent=2))
        else:
            with BridgeClient(port=args.port) as bridge:
                print(json.dumps(bridge.observe() if args.command == "observe" else bridge.release(), indent=2))
    except KeyboardInterrupt:
        return 130
    except (JevError, BridgeError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
