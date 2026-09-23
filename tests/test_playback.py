import math
import unittest

from tools.playback import PlaybackPlan


class PlaybackPlanTests(unittest.TestCase):
    def records(self):
        return [
            {"elapsed_ms": 0, "phase": "combat"},
            {"elapsed_ms": 8000, "phase": "shop"},
            {"elapsed_ms": 10000, "state": {"phase": "upgrade"}},
            {"elapsed_ms": 11000, "phase": "combat"},
            {"elapsed_ms": 15000, "phase": "gameover"},
        ]

    def test_mixed_duration_and_intervals(self):
        plan = PlaybackPlan(self.records(), 17000, 4, menu_speed=1)
        self.assertEqual(plan.duration_ms, 8000)
        self.assertEqual([(s.start_ms, s.end_ms, s.speed, s.output_start_ms, s.output_end_ms, s.phase)
                          for s in plan.segments], [
            (0, 8000, 4, 0, 2000, "combat"),
            (8000, 10000, 1, 2000, 4000, "shop"),
            (10000, 11000, 1, 4000, 5000, "upgrade"),
            (11000, 15000, 4, 5000, 6000, "combat"),
            (15000, 17000, 1, 6000, 8000, "gameover"),
        ])

    def test_transitions_are_continuous_and_do_not_show_future_observations(self):
        plan = PlaybackPlan(self.records(), 17000, 4, menu_speed=1)
        for segment in plan.segments[1:]:
            before, _, _ = plan.at(segment.output_start_ms-.001)
            exact, speed, phase = plan.at(segment.output_start_ms)
            self.assertLess(before, segment.start_ms)
            self.assertEqual(exact, segment.start_ms)
            self.assertEqual(speed, segment.speed)
            self.assertEqual(phase, segment.phase)
        # A result completed at source 9,500ms is not visible at output 3,499ms.
        self.assertLess(plan.at(3499)[0], 9500)
        self.assertEqual(plan.at(3500)[0], 9500)

    def test_trim_inside_phase_retains_prior_observation(self):
        plan = PlaybackPlan(self.records(), 17000, 4, menu_speed=1, start_ms=9000)
        self.assertEqual(plan.duration_ms, 5000)
        self.assertEqual(plan.at(0), (9000, 1, "shop"))
        self.assertEqual(plan.at(1000), (10000, 1, "upgrade"))
        self.assertEqual(plan.at(2000), (11000, 4, "combat"))
        self.assertEqual(plan.at(3000), (15000, 1, "gameover"))

    def test_default_is_constant_speed_with_or_without_records(self):
        for records in (self.records(), []):
            plan = PlaybackPlan(records, 17000, 4, start_ms=3000)
            self.assertEqual(plan.duration_ms, 3500)
            for video_ms in (0, 100, 1500, 3000, 3500):
                self.assertEqual(plan.at(video_ms)[0], 3000+video_ms*4)
                self.assertEqual(plan.at(video_ms)[1], 4)

    def test_clamped_lookups_and_speed_boundaries(self):
        plan = PlaybackPlan(self.records(), 17000, 4, menu_speed=1)
        self.assertEqual(plan.at(-1), (0, 4, "combat"))
        self.assertEqual(plan.at(100000), (17000, 1, "gameover"))
        self.assertEqual(plan.speed_at(-100), 4)
        self.assertEqual(plan.speed_at(7999), 4)
        self.assertEqual(plan.speed_at(8000), 1)
        self.assertEqual(plan.speed_at(11000), 4)
        self.assertEqual(plan.speed_at(100000), 1)

    def test_unsorted_duplicate_and_missing_phase_records(self):
        records = [
            {"elapsed_ms": 8000, "phase": "shop"},
            {"elapsed_ms": 0, "phase": "combat"},
            {"elapsed_ms": 8000, "phase": "crate"},
            {"elapsed_ms": 9000, "phase": "crate"},
            {"elapsed_ms": 9500, "event": "accounting"},
            {"elapsed_ms": 15000, "phase": "victory"},
        ]
        plan = PlaybackPlan(records, 10000, 4, menu_speed=1)
        self.assertEqual(len(plan.segments), 2)
        self.assertEqual(plan.at(2000), (8000, 1, "crate"))
        self.assertEqual(plan.at(4000), (10000, 1, "crate"))

    def test_unknown_uses_base_speed_until_observed_menu(self):
        plan = PlaybackPlan([{"elapsed_ms": 4000, "phase": "menu"}], 6000, 4, menu_speed=1)
        self.assertEqual(plan.at(0), (0, 4, "unknown"))
        self.assertEqual(plan.at(1000), (4000, 1, "menu"))
        self.assertEqual(plan.duration_ms, 3000)

    def test_invalid_constructor_inputs(self):
        for overrides in ({"duration_ms": 0}, {"duration_ms": math.inf}, {"speed": 0},
                          {"speed": -1}, {"speed": math.nan}, {"speed": True},
                          {"menu_speed": 0}, {"menu_speed": math.inf}, {"start_ms": -1},
                          {"start_ms": 1000}, {"start_ms": math.nan}):
            arguments = {"records": [], "duration_ms": 1000, "speed": 4, **overrides}
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                PlaybackPlan(**arguments)
        for record in ({"phase": "combat"}, {"phase": "combat", "elapsed_ms": -1},
                       {"phase": "combat", "elapsed_ms": math.inf}, {"phase": 5, "elapsed_ms": 0}):
            with self.subTest(record=record), self.assertRaises(ValueError):
                PlaybackPlan([record], 1000, 4)

    def test_invalid_lookup_timestamps(self):
        plan = PlaybackPlan([], 1000, 4)
        for value in (math.nan, math.inf, -math.inf, None, "100"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                plan.at(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                plan.speed_at(value)


if __name__ == "__main__":
    unittest.main()
