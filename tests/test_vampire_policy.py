from copy import deepcopy
import math
import unittest

from vampire_agent.policy import DIRECTIONS, PolicyMemory, compact_decision, demo_state, horde_sectors, menu_decision, prepare_decision


class VampirePolicyTests(unittest.TestCase):
    def test_unity_y_up_and_diagonal_normalized(self):
        self.assertEqual(DIRECTIONS["north"], (0, 1))
        self.assertEqual(DIRECTIONS["south"], (0, -1))
        for name, vector in DIRECTIONS.items():
            self.assertAlmostEqual(math.hypot(*vector), 0 if name == "stay" else 1)

    def test_path_collision_detected_even_when_endpoint_passes_enemy(self):
        state = demo_state()
        state["player"]["speed"] = 6
        _, candidates = prepare_decision(state)
        self.assertAlmostEqual(candidates["east"]["minimum_enemy_clearance"], -.4)
        self.assertAlmostEqual(candidates["east"]["endpoint_enemy_clearance"], 1.6)
        self.assertGreater(candidates["west"]["minimum_enemy_clearance"], 0)

    def test_enemy_motion_used_in_closest_approach(self):
        state = demo_state()
        state["enemies"] = [{"x": 2, "y": 0, "velocity_x": -4, "radius": .3}]
        _, candidates = prepare_decision(state)
        self.assertAlmostEqual(candidates["stay"]["minimum_enemy_clearance"], -.5)
        self.assertGreater(candidates["north"]["minimum_enemy_clearance"], -.5)

    def test_pickup_types_and_empty_threats_remain_distinct(self):
        state = demo_state()
        state["enemies"] = []
        state["pickups"] = [{"x": 0, "y": 2, "kind": "experience"},
                            {"x": 0, "y": -2, "kind": "healing"}]
        _, candidates = prepare_decision(state)
        self.assertEqual(candidates["north"]["endpoint_experience_distance"], .5)
        self.assertEqual(candidates["south"]["endpoint_healing_distance"], .5)
        self.assertIsNone(candidates["north"]["endpoint_chest_distance"])
        self.assertIsNone(candidates["north"]["minimum_enemy_clearance"])

    def test_missing_or_invalid_speed_does_not_invent_geometry(self):
        for value in (None, 0, -1, math.inf, math.nan, True):
            with self.subTest(speed=value):
                state = demo_state()
                state["player"]["speed"] = value
                with self.assertRaises(ValueError):
                    prepare_decision(state)

    def test_xp_progress_distinguishes_approaching_retreating_and_staying(self):
        state = demo_state()
        context, candidates = prepare_decision(state)
        self.assertEqual(context["current_experience_distance"], 2)
        self.assertEqual(candidates["west"]["experience_progress"], 1.5)
        self.assertEqual(candidates["east"]["experience_progress"], -1.5)
        self.assertEqual(candidates["stay"]["experience_progress"], 0)

    def test_magnet_path_counts_passed_gems_not_just_endpoint_neighbors(self):
        state = demo_state()
        state["player"].update(speed=6, pickup_radius=.2)
        state["pickups"] = [
            {"x": .1, "y": 0, "kind": "experience"},
            {"x": 1, "y": .1, "kind": "xp"},
            {"x": 2, "y": 0, "kind": "GEM"},
            {"x": 1, "y": .3, "kind": "experience"},
            {"x": 3.3, "y": 0, "kind": "experience"},
            {"x": 1, "y": 0, "kind": "healing"},
        ]
        _, candidates = prepare_decision(state)
        # The route ends at x=3; all three collected gems are outside its
        # endpoint magnet radius, while the near-start gem is reachable at rest.
        self.assertEqual(candidates["east"]["xp_pickups_in_magnet_path"], 3)
        self.assertEqual(candidates["stay"]["xp_pickups_in_magnet_path"], 1)
        self.assertEqual(candidates["west"]["xp_pickups_in_magnet_path"], 1)

    def test_incoming_xp_is_counted_but_excluded_from_navigation(self):
        state = demo_state()
        state["player"]["pickup_radius"] = .2
        state["pickups"] = [
            {"x": -.1, "y": 0, "kind": "experience", "moving_to_player": True},
            {"x": .3, "y": 0, "kind": "GEM", "moving_to_player": True},
            {"x": 1, "y": 0, "kind": "xp", "moving_to_player": False},
            {"x": 3, "y": 0, "kind": "experience"},
            {"x": 0, "y": 0, "kind": "healing", "moving_to_player": True},
        ]
        original = deepcopy(state)
        context, candidates = prepare_decision(state)
        self.assertEqual(context["incoming_experience_pickups"], 2)
        self.assertEqual(context["observed_pickup_count"], 5)
        self.assertEqual(context["current_experience_distance"], 1)
        self.assertEqual(candidates["east"]["endpoint_experience_distance"], .5)
        self.assertEqual(candidates["east"]["experience_progress"], .5)
        self.assertEqual(candidates["west"]["endpoint_experience_distance"], 2.5)
        self.assertEqual(candidates["east"]["xp_pickups_in_magnet_path"], 1)
        self.assertEqual(candidates["stay"]["xp_pickups_in_magnet_path"], 0)
        self.assertEqual(candidates["west"]["xp_pickups_in_magnet_path"], 0)
        self.assertEqual(state, original)

    def test_only_incoming_gems_leave_no_navigation_target(self):
        state = demo_state()
        state["player"]["pickup_radius"] = .2
        state["pickups"] = [{"x": 0, "y": 0, "kind": "xp", "moving_to_player": True}]
        context, candidates = prepare_decision(state)
        self.assertEqual(context["incoming_experience_pickups"], 1)
        self.assertIsNone(context["current_experience_distance"])
        for candidate in candidates.values():
            self.assertIsNone(candidate["endpoint_experience_distance"])
            self.assertIsNone(candidate["experience_progress"])
            self.assertEqual(candidate["xp_pickups_in_magnet_path"], 0)

    def test_compact_input_preserves_legal_actions_geometry_and_original_state(self):
        state = demo_state()
        state["player"]["hp"] = 60.123456
        state["inventory"] = [{"name": "KNIFE", "level": 1.0}]
        original_state = deepcopy(state)
        context, candidates = prepare_decision(state)
        original_context, original_candidates = deepcopy(context), deepcopy(candidates)
        model_context, model_candidates = compact_decision(context, candidates)
        self.assertEqual(list(model_candidates), list(candidates))
        for name, candidate in model_candidates.items():
            self.assertEqual(candidate["minimum_enemy_clearance"], candidates[name]["minimum_enemy_clearance"])
            self.assertEqual(candidate["endpoint_enemy_clearance"], candidates[name]["endpoint_enemy_clearance"])
            self.assertEqual(candidate["experience_progress"], candidates[name]["experience_progress"])
        self.assertLess(model_candidates["east"]["minimum_enemy_clearance"], 0)
        self.assertIs(model_candidates["east"]["meets_clearance_margin"], False)
        self.assertGreater(model_candidates["west"]["experience_progress"], 0)
        self.assertLess(abs(model_context["player"]["hp"] - state["player"]["hp"]), .0005)
        self.assertEqual(state, original_state)
        self.assertEqual(context, original_context)
        self.assertEqual(candidates, original_candidates)
        # The compact request must not alias observations or the detailed log.
        model_context["player"]["hp"] = 0
        model_context["inventory"][0]["level"] = 99
        model_candidates["east"]["minimum_enemy_clearance"] = 99
        self.assertEqual(state, original_state)
        self.assertEqual(context, original_context)
        self.assertEqual(candidates, original_candidates)

    def test_compact_input_omits_unknown_metrics_but_keeps_measured_zero(self):
        state = demo_state()
        state["enemies"] = []
        context, candidates = prepare_decision(state)
        _, unknown = compact_decision(context, candidates)
        self.assertNotIn("minimum_enemy_clearance", unknown["stay"])
        self.assertNotIn("xp_pickups_in_magnet_path", unknown["stay"])
        self.assertNotIn("nearest_enemy_in_firing_direction", unknown["stay"])
        self.assertIn("experience_progress", unknown["stay"])
        self.assertEqual(unknown["stay"]["experience_progress"], 0)
        self.assertIs(unknown["stay"]["meets_clearance_margin"], True)
        state["player"]["pickup_radius"] = .2
        context, candidates = prepare_decision(state)
        _, measured = compact_decision(context, candidates)
        self.assertIn("xp_pickups_in_magnet_path", measured["stay"])
        self.assertEqual(measured["stay"]["xp_pickups_in_magnet_path"], 0)

    def test_stay_preserves_facing_while_movement_changes_firing_direction(self):
        state = demo_state()
        state["player"].update(facing_x=-2, facing_y=0)
        state["enemies"] = [{"x": -2, "y": 0}, {"x": 3, "y": 0}]
        _, candidates = prepare_decision(state)
        self.assertEqual(candidates["stay"]["nearest_enemy_in_firing_direction"], 2)
        self.assertEqual(candidates["east"]["nearest_enemy_in_firing_direction"], 1.5)
        self.assertEqual(candidates["west"]["nearest_enemy_in_firing_direction"], .5)
        self.assertIsNone(candidates["north"]["nearest_enemy_in_firing_direction"])

    def test_unknown_facing_and_pickup_radius_do_not_imply_aim_or_collection(self):
        for facing in ({}, {"facing_x": 1}, {"facing_y": 0},
                       {"facing_x": 0, "facing_y": 0}):
            with self.subTest(facing=facing):
                state = demo_state()
                state["player"].update(facing)
                state["enemies"] = [{"x": 3, "y": 0}]
                _, candidates = prepare_decision(state)
                self.assertIsNone(candidates["stay"]["nearest_enemy_in_firing_direction"])
                self.assertEqual(candidates["east"]["nearest_enemy_in_firing_direction"], 1.5)
                for candidate in candidates.values():
                    self.assertIsNone(candidate["xp_pickups_in_magnet_path"])

    def test_no_observed_xp_is_distinct_from_zero_progress_toward_a_gem(self):
        state = demo_state()
        state["player"]["pickup_radius"] = .3
        state["pickups"] = [{"x": 0, "y": 0, "kind": "healing"}]
        context, candidates = prepare_decision(state)
        self.assertIsNone(context["current_experience_distance"])
        for candidate in candidates.values():
            self.assertIsNone(candidate["experience_progress"])
            self.assertIsNone(candidate["endpoint_experience_distance"])
            self.assertEqual(candidate["xp_pickups_in_magnet_path"], 0)

    def test_clearance_margin_rejects_contact_and_exposes_crowd_truncation(self):
        state = demo_state()
        state.update(enemy_count_total=300, enemy_snapshot_truncated=True)
        context, candidates = prepare_decision(state)
        self.assertFalse(candidates["east"]["meets_clearance_margin"])
        self.assertTrue(candidates["west"]["meets_clearance_margin"])
        self.assertEqual(context["observed_enemy_count"], 1)
        self.assertEqual(context["enemy_count_total"], 300)
        self.assertTrue(context["enemy_snapshot_truncated"])

    def test_menu_uses_exact_offer_ids_descriptions_and_inventory(self):
        state = {"phase": "levelup", "inventory": [{"name": "Whip", "level": 1}],
                 "menu": {"options": [{"id": "offer-17", "label": "Whip", "description": "+1 projectile"}]}}
        context, candidates = menu_decision(state)
        self.assertEqual(candidates, {"offer-17": {"label": "Whip", "description": "+1 projectile"}})
        self.assertEqual(context["inventory"], state["inventory"])
        self.assertEqual(state["menu"]["options"][0]["id"], "offer-17")

    def test_duplicate_menu_ids_rejected(self):
        with self.assertRaises(ValueError):
            menu_decision({"phase": "levelup", "menu": {"options": [{"id": "a"}, {"id": "a"}]}})


class VampireHordePolicyTests(unittest.TestCase):
    def test_horde_sectors_partition_compass_and_count_distance_boundaries(self):
        start = (10, -4)
        directions = {name: vector for name, vector in DIRECTIONS.items() if name != "stay"}
        enemies = [{"x": start[0] + dx * distance, "y": start[1] + dy * distance}
                   for dx, dy in directions.values() for distance in (1, 2)]
        enemies.extend([
            {"x": 10, "y": 2},       # North, exactly six units away.
            {"x": 10, "y": 2.001},   # Outside the horde observation radius.
            {"x": 11.5, "y": -4},    # East, exactly at the close-count boundary.
            {"x": 11.501, "y": -4},  # East, just beyond the close-count boundary.
            {"x": 10, "y": -4},      # No bearing: must not invent a compass direction.
        ])
        sectors = horde_sectors(start, enemies)
        self.assertEqual(set(sectors), set(directions) | {"overlapping"})
        self.assertEqual(sum(sector["count"] for sector in sectors.values()), 20)
        for name in directions:
            with self.subTest(direction=name):
                self.assertEqual(sectors[name]["count"], {"north": 3, "east": 4}.get(name, 2))
                self.assertEqual(sectors[name]["within_1_5"], 2 if name == "east" else 1)
                self.assertAlmostEqual(sectors[name]["nearest_distance"], 1)
        self.assertEqual(sectors["overlapping"]["count"], 1)
        self.assertEqual(sectors["overlapping"]["within_1_5"], 1)
        self.assertEqual(sectors["overlapping"]["nearest_distance"], 0)

    def test_sector_boundary_does_not_duplicate_an_enemy_into_adjacent_hordes(self):
        enemies = [{"x": 2 * math.cos(math.radians(angle)),
                    "y": 2 * math.sin(math.radians(angle))} for angle in (22, 23)]
        sectors = horde_sectors((0, 0), enemies)
        self.assertEqual(set(sectors), {"east", "northeast"})
        self.assertEqual(sectors["east"]["count"], 1)
        self.assertEqual(sectors["northeast"]["count"], 1)

    def test_nearer_unknown_hp_does_not_inherit_farther_enemy_hp(self):
        for hp in (None, math.nan, math.inf, True, 0):
            with self.subTest(hp=hp):
                far = {"x": 3, "y": 0, "hp": 50}
                near = {"x": 2, "y": 0, "hp": hp}
                for enemies in ([far, near], [near, far]):
                    sector = horde_sectors((0, 0), enemies)["east"]
                    self.assertEqual(sector["nearest_distance"], 2)
                    if hp == 0 and type(hp) is int:
                        self.assertIn("nearest_hp", sector)
                        self.assertEqual(sector["nearest_hp"], 0)
                    else:
                        self.assertNotIn("nearest_hp", sector)

    def test_forward_cone_uses_enemy_motion_and_candidate_endpoint(self):
        state = demo_state()
        state["player"].update(speed=2, facing_x=1, facing_y=0)
        state["enemies"] = [
            {"x": 3, "y": 2, "velocity_y": -4, "hp": 7},  # Moves into the cone.
            {"x": 3, "y": 0, "velocity_y": 4, "hp": 9},   # Moves out of the cone.
            {"x": 7, "y": 0, "velocity_x": -4, "hp": 11}, # Ends exactly four units ahead.
            {"x": 7.01, "y": 0, "velocity_x": -4, "hp": 13},
        ]
        _, candidates = prepare_decision(state)
        self.assertEqual(candidates["east"]["enemies_in_firing_direction"], 2)
        self.assertEqual(candidates["east"]["nearest_enemy_in_firing_direction"], 2)
        self.assertEqual(candidates["east"]["nearest_forward_enemy_hp"], 7)
        # Staying keeps the observed east-facing aim, but not east's movement endpoint.
        self.assertEqual(candidates["stay"]["enemies_in_firing_direction"], 1)
        self.assertEqual(candidates["stay"]["nearest_enemy_in_firing_direction"], 3)
        self.assertEqual(candidates["stay"]["nearest_forward_enemy_hp"], 7)

    def test_forward_nearest_hp_unknown_and_measured_zero_remain_distinct(self):
        for hp in (None, math.nan, math.inf, True, 0):
            with self.subTest(hp=hp):
                state = demo_state()
                state["player"].update(facing_x=1, facing_y=0)
                state["enemies"] = [{"x": 2, "y": 0, "hp": hp}, {"x": 3, "y": 0, "hp": 50}]
                context, candidates = prepare_decision(state)
                _, model_candidates = compact_decision(context, candidates)
                self.assertEqual(candidates["stay"]["enemies_in_firing_direction"], 2)
                if hp == 0 and type(hp) is int:
                    self.assertEqual(candidates["stay"]["nearest_forward_enemy_hp"], 0)
                    self.assertEqual(model_candidates["stay"]["nearest_forward_enemy_hp"], 0)
                else:
                    self.assertIsNone(candidates["stay"]["nearest_forward_enemy_hp"])
                    self.assertNotIn("nearest_forward_enemy_hp", model_candidates["stay"])

    def test_unknown_stay_facing_count_is_unknown_but_empty_known_cone_is_zero(self):
        state = demo_state()
        state["enemies"] = [{"x": 0, "y": -3, "hp": 10}]
        for facing in ({}, {"facing_x": 0, "facing_y": 0}):
            with self.subTest(facing=facing):
                state["player"].update(facing)
                context, candidates = prepare_decision(state)
                _, model_candidates = compact_decision(context, candidates)
                self.assertIsNone(candidates["stay"]["enemies_in_firing_direction"])
                self.assertNotIn("enemies_in_firing_direction", model_candidates["stay"])
                self.assertEqual(candidates["north"]["enemies_in_firing_direction"], 0)
                self.assertEqual(model_candidates["north"]["enemies_in_firing_direction"], 0)

    def test_compact_input_preserves_horde_and_forward_metrics_without_aliasing(self):
        state = demo_state()
        state["player"].update(facing_x=1, facing_y=0)
        state["enemies"] = [{"x": 2, "y": 0, "hp": 0}, {"x": 3, "y": 0, "hp": 50}]
        context, candidates = prepare_decision(state)
        original_state, original_context, original_candidates = deepcopy(state), deepcopy(context), deepcopy(candidates)
        model_context, model_candidates = compact_decision(context, candidates)
        self.assertEqual(model_context["hordes_within_6_units"]["east"], {
            "count": 2, "within_1_5": 0, "nearest_distance": 2, "nearest_hp": 0,
        })
        self.assertEqual(model_candidates["stay"]["enemies_in_firing_direction"], 2)
        self.assertEqual(model_candidates["stay"]["nearest_forward_enemy_hp"], 0)
        self.assertEqual(model_candidates["west"]["enemies_in_firing_direction"], 0)
        model_context["hordes_within_6_units"]["east"]["count"] = 99
        model_candidates["stay"]["enemies_in_firing_direction"] = 99
        self.assertEqual(state, original_state)
        self.assertEqual(context, original_context)
        self.assertEqual(candidates, original_candidates)


class VampirePolicyMemoryTests(unittest.TestCase):
    @staticmethod
    def observation(elapsed, xp, position=(0, 0)):
        return {"elapsed": elapsed, "player": {"xp": xp, "x": position[0], "y": position[1]}}

    def test_observed_growth_and_return_trip_are_reported_separately(self):
        memory = PolicyMemory()
        initial = memory.observe(self.observation(10, 4))
        self.assertEqual(initial["window_seconds"], 0)
        self.assertEqual(initial["xp_gained"], 0)
        self.assertEqual(initial["seconds_without_xp_gain"], 0)
        self.assertEqual(initial["recent_applied_actions"], [])

        memory.applied("east")
        outward = memory.observe(self.observation(12, 4, (3, 0)))
        self.assertEqual(outward["seconds_without_xp_gain"], 2)
        self.assertEqual(outward["distance_traveled"], 3)
        self.assertEqual(outward["net_displacement"], 3)

        memory.applied("west")
        returned = memory.observe(self.observation(14, 5))
        self.assertEqual(returned["window_seconds"], 4)
        self.assertEqual(returned["xp_gained"], 1)
        self.assertEqual(returned["seconds_without_xp_gain"], 0)
        self.assertEqual(returned["distance_traveled"], 6)
        self.assertEqual(returned["net_displacement"], 0)
        self.assertEqual(returned["recent_applied_actions"], ["east", "west"])
        # Previously logged contexts must not acquire later actions.
        self.assertEqual(outward["recent_applied_actions"], ["east"])

    def test_xp_window_expires_old_gain_but_retains_time_since_last_gain(self):
        memory = PolicyMemory()
        memory.observe(self.observation(0, 1))
        memory.observe(self.observation(2, 3))
        memory.observe(self.observation(8, 3))
        recent = memory.observe(self.observation(10, 3))
        self.assertEqual(recent["window_seconds"], 8)
        self.assertEqual(recent["xp_gained"], 0)
        self.assertEqual(recent["seconds_without_xp_gain"], 8)
        later = memory.observe(self.observation(11, 3))
        self.assertEqual(later["window_seconds"], 3)
        self.assertEqual(later["seconds_without_xp_gain"], 9)

    def test_missing_xp_remains_unknown_until_a_measured_window_exists(self):
        memory = PolicyMemory()
        unknown = memory.observe(self.observation(0, None))
        self.assertIsNone(unknown["xp_gained"])
        self.assertIsNone(unknown["seconds_without_xp_gain"])
        memory.observe(self.observation(5, 3))
        partly_known = memory.observe(self.observation(6, 3))
        self.assertIsNone(partly_known["xp_gained"])
        self.assertEqual(partly_known["seconds_without_xp_gain"], 1)
        measured = memory.observe(self.observation(10, 4))
        self.assertEqual(measured["window_seconds"], 5)
        self.assertEqual(measured["xp_gained"], 1)
        self.assertEqual(measured["seconds_without_xp_gain"], 0)

    def test_applied_history_is_bounded_and_rejects_nonmovement_actions(self):
        memory = PolicyMemory()
        memory.observe(self.observation(0, 0))
        actions = list(DIRECTIONS)
        for action in actions:
            memory.applied(action)
        with self.assertRaises(ValueError):
            memory.applied("select:1")
        result = memory.observe(self.observation(1, 0))
        self.assertEqual(result["recent_applied_actions"], actions[-8:])

    def test_restarted_game_clock_clears_progress_and_action_history(self):
        memory = PolicyMemory()
        memory.observe(self.observation(100, 10, (7, 8)))
        memory.applied("north")
        memory.observe(self.observation(105, 12, (7, 11)))
        reset = memory.observe(self.observation(2, 0, (-3, -4)))
        self.assertEqual(reset, {
            "window_seconds": 0, "xp_gained": 0,
            "seconds_without_xp_gain": 0, "distance_traveled": 0,
            "net_displacement": 0, "recent_applied_actions": [],
        })


if __name__ == "__main__":
    unittest.main()
