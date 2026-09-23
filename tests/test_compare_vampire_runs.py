import json
from pathlib import Path
import tempfile
import unittest

from tools.compare_vampire_runs import compare_run, read_live_jsonl
from vampire_agent.policy import prepare_decision


class CompareVampireRunsTests(unittest.TestCase):
    def test_only_unfinished_final_json_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "decisions.jsonl"
            path.write_bytes(b'{"event":"phase"}\n{"event":')
            records, ignored = read_live_jsonl(path)
            self.assertEqual(records, [{"event": "phase"}])
            self.assertTrue(ignored)
            path.write_bytes(b'{"event":"phase"}\n{"event":\n')
            with self.assertRaisesRegex(ValueError, "Invalid complete JSONL"):
                read_live_jsonl(path)

    def test_logged_geometry_and_usage_drive_comparison(self):
        state = {
            "phase": "combat", "elapsed": 10,
            "player": {"x": 0, "y": 0, "hp": 100, "max_hp": 100, "level": 1, "speed": 2, "xp": 0},
            "enemies": [{"x": 10, "y": 0, "radius": .1}],
            "pickups": [{"x": -1, "y": 0, "kind": "experience"}],
        }
        _, candidates = prepare_decision(state)
        decision = {"event": "decision", "phase": "combat", "state": state,
                    "choice": "east", "applied": True, "candidate_geometry": candidates,
                    "judgment": {"latency_ms": 123}, "metrics": {"requests": 1, "input_tokens": 999}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "decisions.jsonl").write_text(json.dumps(decision) + "\n")
            result = compare_run(path)
        self.assertEqual(result["status"], "partial (no complete summary)")
        self.assertEqual(result["accounting"]["input_tokens"], 999)
        self.assertEqual(result["median_latency_ms"], 123)
        self.assertEqual(result["xp_opportunities"]["geometry_recomputed_decisions"], 0)
        self.assertEqual(result["xp_opportunities"]["retreating"], 1)
        self.assertEqual(result["xp_opportunities"]["eligible_opportunities"], 1)
        self.assertEqual(result["xp_opportunities"]["missed_at_full_health"], 1)


if __name__ == "__main__":
    unittest.main()
