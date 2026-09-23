"""Recorded phase changes and inference completion have independent causal clocks."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from tools.render_vampire_timelapse import VampireRenderer, VampireTimeline, base


class VampireRenderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)
        (self.path / "frames").mkdir()
        frames = []
        for index, (timestamp, phase) in enumerate([(0, "menu"), (100, "combat"),
                                                   (300, "levelup"), (800, "chest"),
                                                   (1000, "combat"), (1250, "gameover")]):
            filename = f"frames/{index}.png"
            Image.new("RGB", (320, 180), "#203b45").save(self.path / filename)
            frames.append({"elapsed_ms": timestamp, "file": filename, "phase": phase})
        self.write("metadata.json", {"capture_fps": 4, "fixture": True})
        self.write_lines("frames.jsonl", frames)
        self.write_lines("decisions.jsonl", [
            {"event": "phase", "elapsed_ms": 100, "phase": "combat",
             "state": {"elapsed": 2, "player": {"level": 1}}},
            {"event": "decision", "elapsed_ms": 600, "decision_started_ms": 120,
             "observation_elapsed_ms": 120, "phase": "combat", "state": {"elapsed": 2.02},
             "choice": "west", "applied": False,
             "judgment": {"probabilities": {"west": .9, "east": .1}, "latency_ms": 480,
                          "usage": {"input_tokens": 1000, "output_tokens": 4}}},
            {"event": "decision", "elapsed_ms": 950, "decision_started_ms": 820,
             "phase": "chest", "choice": "confirm", "applied": True,
             "candidates": {"confirm": {"label": "Open chest"}},
             "judgment": {"probabilities": {"confirm": 1.0}, "latency_ms": 130,
                          "usage": {"input_tokens": 2000, "output_tokens": 8}}},
        ])

    def write(self, filename, value):
        (self.path / filename).write_text(json.dumps(value))

    def write_lines(self, filename, rows):
        (self.path / filename).write_text("".join(json.dumps(row) + "\n" for row in rows))

    def test_pending_request_cannot_show_future_decision_or_cost(self):
        snapshot = VampireTimeline(self.path).at(400)
        self.assertTrue(snapshot.pending)
        self.assertIsNone(snapshot.decision)
        self.assertNotIn("input_tokens", snapshot.metrics)
        self.assertNotIn("estimated_cost_usd", snapshot.metrics)
        self.assertEqual(snapshot.observation["phase"], "levelup")
        self.assertEqual(snapshot.observation["state"]["player"]["level"], 1)

    def test_completed_combat_decision_does_not_regress_observed_menu_phase(self):
        snapshot = VampireTimeline(self.path).at(650)
        self.assertFalse(snapshot.pending)
        self.assertEqual(snapshot.decision["choice"], "west")
        self.assertEqual(snapshot.observation["phase"], "levelup")
        self.assertEqual(snapshot.metrics["input_tokens"], 1000)
        self.assertAlmostEqual(snapshot.metrics["estimated_cost_usd"], .000042)

    def test_default_video_opens_on_combat_and_menus_play_at_real_speed(self):
        timeline = VampireTimeline(self.path)
        renderer = VampireRenderer(timeline)
        self.assertEqual(renderer.playback.source_start_ms, 100)
        self.assertEqual(renderer.playback.at(0), (100, 4, "combat"))
        self.assertEqual(renderer.playback.speed_at(299), 4)
        self.assertEqual(renderer.playback.speed_at(300), 1)
        self.assertEqual(renderer.playback.speed_at(800), 1)
        self.assertEqual(renderer.playback.speed_at(1000), 4)
        self.assertEqual(renderer.playback.speed_at(1250), 1)
        self.assertEqual(renderer.playback.duration_ms, 1062.5)

    def test_frame_file_and_candidate_label_aliases_do_not_change_source_logs(self):
        timeline = VampireTimeline(self.path)
        self.assertEqual(timeline.at(950).frame["path"], "frames/3.png")
        self.assertEqual(timeline.at(950).decision["candidates"]["confirm"]["name"], "Open chest")
        original = json.loads((self.path / "decisions.jsonl").read_text().splitlines()[-1])
        self.assertNotIn("name", original["candidates"]["confirm"])

    def test_frame_game_clock_refreshes_without_a_new_decision(self):
        frames = [json.loads(line) for line in (self.path / "frames.jsonl").read_text().splitlines()]
        frames[-1]["game_elapsed"] = 3
        self.write_lines("frames.jsonl", frames)
        timeline = VampireTimeline(self.path)
        self.assertEqual(timeline.at(1249).observation["state"]["elapsed"], 2.02)
        self.assertEqual(timeline.at(1250).observation["state"]["elapsed"], 3)
        self.assertEqual(timeline.at(1250).decision["choice"], "confirm")

    def test_upgrade_labels_use_displayed_name_instead_of_internal_id(self):
        rows = [json.loads(line) for line in (self.path / "decisions.jsonl").read_text().splitlines()]
        rows[-1]["candidates"] = {"select:0": {"label": "Choose SILF", "kind": "upgrade",
                                              "description": "Peachone | New! | Bombards in a circling zone."}}
        self.write_lines("decisions.jsonl", rows)
        candidate = VampireTimeline(self.path).decisions[-1]["candidates"]["select:0"]
        self.assertEqual(base.candidate_label("select:0", candidate), "Choose Peachone")

    def test_valid_response_rejected_by_game_is_not_labelled_as_api_failure(self):
        timeline = VampireTimeline(self.path)
        timeline.decisions[0]["error"] = "not_in_combat"
        renderer = VampireRenderer(timeline)
        with patch.object(base.Renderer, "text") as text:
            renderer.draw(650)
        labels = [str(call.args[2]) for call in text.call_args_list]
        self.assertTrue(any(label.startswith("Action not applied") for label in labels))
        self.assertFalse(any(label.startswith("Request failed") for label in labels))

    def test_menu_frame_stays_at_menu_speed_until_a_combat_frame_is_available(self):
        with (self.path / "decisions.jsonl").open("a") as log:
            log.write(json.dumps({"event": "phase", "elapsed_ms": 980, "phase": "combat"}) + "\n")
        timeline = VampireTimeline(self.path)
        renderer = VampireRenderer(timeline)
        self.assertEqual(timeline.at(990).observation["phase"], "chest")
        self.assertEqual(renderer.playback.speed_at(990), 1)
        self.assertEqual(timeline.at(1000).observation["phase"], "combat")
        self.assertEqual(renderer.playback.speed_at(1000), 4)

    def test_latest_recorded_totals_are_used_without_leaking_summary(self):
        with (self.path / "decisions.jsonl").open("a") as log:
            log.write(json.dumps({"event": "phase", "elapsed_ms": 1200, "phase": "combat",
                                  "metrics": {"requests": 3, "input_tokens": 4000,
                                              "unknown_usage_requests": 1}}) + "\n")
        self.write("summary.json", {"input_tokens": 9999, "estimated_cost_usd": .01})
        timeline = VampireTimeline(self.path)
        self.assertEqual(timeline.at(1199).metrics["input_tokens"], 3000)
        self.assertEqual(timeline.at(1200).metrics["input_tokens"], 4000)
        self.assertEqual(timeline.at(1300).metrics["unknown_usage_requests"], 1)
        self.assertEqual(timeline.final_metrics["input_tokens"], 9999)

    def test_future_observation_timestamp_is_rejected(self):
        self.write_lines("decisions.jsonl", [{"event": "decision", "elapsed_ms": 100,
                                             "observation_elapsed_ms": 101, "phase": "combat"}])
        with self.assertRaisesRegex(ValueError, "Observation timestamp"):
            VampireTimeline(self.path)

    def test_menu_only_capture_needs_an_explicit_start(self):
        self.write_lines("decisions.jsonl", [])
        rows = [json.loads(line) for line in (self.path / "frames.jsonl").read_text().splitlines()]
        for row in rows:
            row["phase"] = "menu"
        self.write_lines("frames.jsonl", rows)
        timeline = VampireTimeline(self.path)
        with self.assertRaisesRegex(ValueError, "No captured combat frame"):
            VampireRenderer(timeline)
        self.assertEqual(VampireRenderer(timeline, start_ms=0).playback.source_start_ms, 0)

    def test_dashboard_and_result_card_render_full_hd_with_correct_game_labels(self):
        self.write("summary.json", {"game_elapsed_seconds": 74, "outcome": "gameover"})
        renderer = VampireRenderer(VampireTimeline(self.path))
        for canvas in (renderer.draw(100), renderer.draw(650), renderer.draw(950),
                       renderer.title_card(), renderer.title_card(outro=True)):
            self.assertEqual(canvas.size, (1920, 1080))
            self.assertEqual(canvas.mode, "RGB")


if __name__ == "__main__":
    unittest.main()
