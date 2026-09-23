"""The video must not display model outcomes before their recorded completion."""
import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from tools.render_timelapse import HEIGHT, WIDTH, Renderer, Timeline


class RenderTimelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        (self.path / "frames").mkdir()
        for index in (1, 2):
            Image.new("RGB", (320, 180), "#203b45").save(self.path / "frames" / f"{index}.png")
        self.write("metadata.json", {"capture_interval_ms": 250, "fixture": True,
                                     "price_usd_per_million_input_tokens": .042})
        self.write_lines("frames.jsonl", [
            {"elapsed_ms": 100, "frame": 1, "path": "frames/1.png"},
            {"elapsed_ms": 1000, "frame": 2, "path": "frames/2.png"},
        ])
        self.write_lines("decisions.jsonl", [
            {"elapsed_ms": 600, "decision_started_ms": 120, "choice": "west", "applied": True,
             "judgment": {"probabilities": {"west": .9, "east": .1}, "usage": {"input_tokens": 1000, "output_tokens": 0}}},
            {"elapsed_ms": 1100, "decision_started_ms": 650, "choice": "east", "applied": False,
             "judgment": {"probabilities": {"west": .1, "east": .9}, "usage": {"input_tokens": 2000, "output_tokens": 0}}},
        ])

    def write(self, filename, value):
        (self.path / filename).write_text(json.dumps(value))

    def write_lines(self, filename, values):
        (self.path / filename).write_text("".join(json.dumps(value) + "\n" for value in values))

    def test_pending_request_has_no_future_choice_or_tokens(self):
        snapshot = Timeline(self.path).at(500)
        self.assertTrue(snapshot.pending)
        self.assertIsNone(snapshot.decision)
        self.assertEqual(snapshot.metrics, {})

    def test_new_pending_request_keeps_previous_completed_distribution(self):
        snapshot = Timeline(self.path).at(900)
        self.assertTrue(snapshot.pending)
        self.assertEqual(snapshot.decision["choice"], "west")
        self.assertEqual(snapshot.decision["judgment"]["probabilities"]["west"], .9)
        self.assertEqual(snapshot.metrics["input_tokens"], 1000)

    def test_completion_and_frame_selection_are_causal(self):
        timeline = Timeline(self.path)
        self.assertIsNone(timeline.at(99).frame)
        self.assertEqual(timeline.at(999).frame["frame"], 1)
        snapshot = timeline.at(1100)
        self.assertFalse(snapshot.pending)
        self.assertEqual(snapshot.frame["frame"], 2)
        self.assertEqual(snapshot.metrics["input_tokens"], 3000)
        self.assertEqual(snapshot.metrics["applied_actions"], 1)
        self.assertAlmostEqual(snapshot.metrics["estimated_cost_usd"], .000126)

    def test_logged_cumulative_metrics_override_derived_estimate(self):
        rows = [json.loads(line) for line in (self.path / "decisions.jsonl").read_text().splitlines()]
        rows[-1]["metrics"] = {"input_tokens": 4000, "output_tokens": 0, "requests": 3,
                                "estimated_cost_usd": .25, "applied_actions": 1}
        self.write_lines("decisions.jsonl", rows)
        snapshot = Timeline(self.path).at(1200)
        self.assertEqual(snapshot.metrics["requests"], 3)
        self.assertEqual(snapshot.metrics["estimated_cost_usd"], .25)

    def test_invalid_timestamp_fails_instead_of_guessing(self):
        self.write_lines("decisions.jsonl", [{"choice": "west"}])
        with self.assertRaisesRegex(ValueError, "elapsed_ms"):
            Timeline(self.path)

    def test_phase_event_refreshes_telemetry_without_erasing_last_decision(self):
        with (self.path / "decisions.jsonl").open("a") as log:
            log.write(json.dumps({"event": "phase", "elapsed_ms": 1150,
                                  "phase": "gameover", "state": {"wave": 4},
                                  "applied": False, "metrics": {"requests": 2, "input_tokens": 3000}}) + "\n")
        timeline = Timeline(self.path)
        snapshot = timeline.at(1200)
        self.assertEqual(len(timeline.decisions), 2)
        self.assertEqual(snapshot.decision["choice"], "east")
        self.assertEqual(snapshot.observation["phase"], "gameover")
        self.assertEqual(snapshot.observation["state"]["wave"], 4)
        self.assertEqual(snapshot.metrics["requests"], 2)

    def test_summary_is_authoritative_only_for_final_totals(self):
        self.write("summary.json", {"input_tokens": 9000, "output_tokens": 0, "requests": 4,
                                    "estimated_cost_usd": .001, "unknown_usage_requests": 1})
        timeline = Timeline(self.path)
        self.assertEqual(timeline.final_metrics["input_tokens"], 9000)
        self.assertEqual(timeline.final_metrics["unknown_usage_requests"], 1)
        self.assertEqual(timeline.at(900).metrics["input_tokens"], 1000)
        self.assertEqual(timeline.at(900).metrics["unknown_usage_requests"], 0)

    def test_dashboard_and_cards_render_at_full_hd(self):
        renderer = Renderer(Timeline(self.path), 4)
        for picture in (renderer.draw(0), renderer.draw(900), renderer.title_card(), renderer.title_card(outro=True)):
            self.assertEqual(picture.size, (WIDTH, HEIGHT))
            self.assertEqual(picture.mode, "RGB")


if __name__ == "__main__":
    unittest.main()
