from contextlib import ExitStack
import json
import math
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from vampire_agent.jev import JevAPIError, JevResponseError, Judgment
from vampire_agent.bridge import BridgeRejected
from vampire_agent.policy import demo_state
from vampire_agent.session import run_session, validate_limits


def judgment(choice="west", input_tokens=1000):
    return Judgment(choice, 1, {choice: 1}, 200, "test-model", {"input_tokens": input_tokens, "output_tokens": 20})


class VampireSessionTests(unittest.TestCase):
    def run_fake(self, *, choices=None, states=None, record=False, max_calls=1, max_cost=.02, move_error=None, start_run=False):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            stack.enter_context(patch("vampire_agent.session.ROOT", Path(directory)))
            bridge = MagicMock()
            if states:
                bridge.observe.side_effect = states
            else:
                bridge.observe.return_value = demo_state()
            bridge.request.return_value = {"active": True, "now_ms": 100, "started_at_ms": 100}
            if move_error:
                bridge.move.side_effect = move_error
            stack.enter_context(patch("vampire_agent.session.BridgeClient")).return_value.__enter__.return_value = bridge
            client = MagicMock()
            client.choose.side_effect = choices or [judgment()]
            stack.enter_context(patch("vampire_agent.session.JevClient", return_value=client))
            bridge.attach_mock(stack.enter_context(patch("vampire_agent.session.time.sleep")), "capture_wait")
            stack.enter_context(patch("builtins.print"))
            args = SimpleNamespace(seconds=60, max_calls=max_calls, max_cost=max_cost, session="test", record=record, port=4244, start_run=start_run)
            result = run_session(args)
            records = [json.loads(line) for line in (Path(result["directory"]) / "decisions.jsonl").read_text().splitlines()]
            return result, records, bridge, client

    def test_request_limit_and_action_accounting(self):
        result, records, bridge, client = self.run_fake()
        self.assertEqual(result["outcome"], "request_limit")
        self.assertEqual(result["requests"], 1)
        self.assertEqual(result["applied_actions"], 1)
        self.assertAlmostEqual(result["estimated_cost_usd"], .000042)
        self.assertEqual(result["output_tokens"], 20)
        bridge.move.assert_called_once_with((-1, 0), 1, ttl_ms=500)
        client.close.assert_called_once()
        decision = next(row for row in records if row["event"] == "decision")
        self.assertLessEqual(decision["observation_elapsed_ms"], decision["elapsed_ms"])
        self.assertEqual(decision["judgment"]["probabilities"], {"west": 1})

    def test_cost_limit_blocks_following_request(self):
        result, _, _, client = self.run_fake(choices=[judgment(input_tokens=1_000_000)], max_calls=5, max_cost=.01)
        self.assertEqual(result["outcome"], "cost_limit")
        self.assertEqual(client.choose.call_count, 1)
        self.assertEqual(result["estimated_cost_usd"], .042)

    def test_invalid_response_usage_counted_but_action_not_applied(self):
        error = JevResponseError("bad choice distribution")
        error.usage = {"input_tokens": 999, "output_tokens": 12}
        error.latency_ms = 150
        error.answer = {"choice": "west"}
        result, records, bridge, _ = self.run_fake(choices=[error])
        self.assertEqual(result["input_tokens"], 999)
        self.assertEqual(result["api_errors"], 1)
        self.assertEqual(result["applied_actions"], 0)
        self.assertEqual(result["unknown_usage_requests"], 0)
        bridge.move.assert_not_called()
        self.assertEqual(records[-1]["invalid_answer"], error.answer)

    def test_unknown_usage_and_three_consecutive_failures_stop(self):
        result, _, bridge, client = self.run_fake(choices=[JevAPIError("timeout")] * 3, max_calls=5)
        self.assertEqual(result["outcome"], "consecutive_failures")
        self.assertEqual(result["unknown_usage_requests"], 3)
        self.assertEqual(client.choose.call_count, 3)
        bridge.move.assert_not_called()

    def test_live_menu_uses_offered_action_and_gameover_stops(self):
        menu = {"seq": 8, "phase": "levelup", "menu": {"options": [{"id": "upgrade-2", "label": "Whip"}]}}
        end = {"seq": 9, "phase": "gameover", "elapsed": 123}
        result, _, bridge, client = self.run_fake(states=[menu, end], choices=[judgment("upgrade-2")], max_calls=5)
        self.assertEqual(result["outcome"], "gameover")
        bridge.act.assert_called_once_with("upgrade-2", 8)
        bridge.move.assert_not_called()
        self.assertEqual(client.choose.call_args.args[1], {"upgrade-2": {"label": "Whip"}})

    def test_recording_stopped_after_rejected_action(self):
        result, _, bridge, _ = self.run_fake(record=True, move_error=BridgeRejected("stale"))
        self.assertEqual(result["rejected_actions"], 1)
        self.assertEqual(result["input_tokens"], 1000)
        self.assertEqual(result["applied_actions"], 0)
        self.assertEqual([call.args[0] for call in bridge.request.call_args_list], ["record_start", "pause", "record_stop"])
        self.assertGreaterEqual(bridge.release.call_count, 2)

    def test_progress_memory_records_only_applied_movement(self):
        result, records, _, client = self.run_fake(
            choices=[judgment("west"), judgment("east"), judgment("north")],
            max_calls=3, move_error=[BridgeRejected("stale"), None, None])
        decisions = [row for row in records if row["event"] == "decision"]
        histories = [row["decision_state"]["recent_progress"]["recent_applied_actions"] for row in decisions]
        self.assertEqual(histories, [[], [], ["east"]])
        self.assertEqual(decisions[-1]["decision_state"]["previous_action"], "east")
        self.assertEqual(result["applied_actions"], 2)
        self.assertEqual(client.choose.call_count, 3)

    def test_invalid_limits_rejected_before_live_work(self):
        for seconds, calls, cost in [(math.nan, 120, .02), (60, 0, .02), (60, True, .02), (60, 1, math.inf), (0, 1, .01)]:
            with self.assertRaises(ValueError):
                validate_limits(seconds, calls, cost)

    def test_slow_combat_judgment_is_accounted_and_not_applied(self):
        clock = [0]
        def slow_choice(*args):
            clock[0] += .9
            return judgment()
        with patch("vampire_agent.session.time.monotonic", side_effect=lambda: clock[0]):
            result, records, bridge, _ = self.run_fake(choices=slow_choice)
        self.assertEqual(result["expired_decisions"], 1)
        self.assertEqual(result["input_tokens"], 1000)
        self.assertEqual(records[-1]["reason"], "decision expired")
        bridge.move.assert_not_called()

    def test_menu_result_after_deadline_is_not_applied(self):
        clock = [0]
        def slow_choice(*args):
            clock[0] += 61
            return judgment("upgrade-2")
        menu = {"seq": 8, "phase": "levelup", "menu": {"options": [{"id": "upgrade-2", "label": "Whip"}]}}
        with patch("vampire_agent.session.time.monotonic", side_effect=lambda: clock[0]):
            result, _, bridge, _ = self.run_fake(states=[menu], choices=slow_choice)
        self.assertEqual(result["outcome"], "time_limit")
        self.assertEqual(result["expired_decisions"], 1)
        bridge.act.assert_not_called()

    def test_explicit_setup_starts_once_without_counting_as_jev(self):
        menu = {"seq": 8, "phase": "menu", "menu": {"options": [{"id": "start_run", "label": "Start run"}]}}
        end = {"seq": 10, "phase": "gameover"}
        result, records, bridge, client = self.run_fake(states=[menu, menu, demo_state(), end],
                                                       max_calls=5, start_run=True)
        bridge.act.assert_called_once_with("start_run", 8)
        client.choose.assert_called_once()
        self.assertEqual(result["requests"], 1)
        self.assertEqual(result["applied_actions"], 1)
        setup = [record for record in records if record["event"] == "setup"]
        self.assertEqual(len(setup), 1)
        self.assertEqual(setup[0]["policy"], "explicit_setup")
        self.assertTrue(setup[0]["applied"])
        self.assertNotIn("judgment", setup[0])

    def test_setup_is_off_by_default(self):
        menu = {"seq": 8, "phase": "menu", "menu": {"options": [{"id": "start_run", "label": "Start run"}]}}
        end = {"seq": 10, "phase": "gameover"}
        result, records, bridge, client = self.run_fake(states=[menu, end])
        bridge.act.assert_not_called()
        client.choose.assert_not_called()
        self.assertEqual(result["requests"], 0)
        self.assertFalse(any(record["event"] == "setup" for record in records))

    def test_start_requires_exact_offered_id_and_times_out_clearly(self):
        menu = {"seq": 8, "phase": "menu", "menu": {"options": [{"id": "startRun", "label": "Start run"}]}}
        with patch("vampire_agent.session.time.monotonic", side_effect=iter(range(1000))):
            result, _, bridge, client = self.run_fake(states=[menu] * 100, start_run=True)
        self.assertEqual(result["outcome"], "start_run_unavailable")
        self.assertIn("Select a solo character and stage first", result["error"])
        bridge.act.assert_not_called()
        client.choose.assert_not_called()

    def test_terminal_recording_keeps_two_seconds_of_actual_capture_without_api_calls(self):
        end = {"seq": 8, "phase": "gameover", "elapsed": 152, "player": {"hp": 0}}
        result, _, bridge, client = self.run_fake(states=[end], record=True)
        self.assertEqual(result["outcome"], "gameover")
        self.assertEqual(result["requests"], 0)
        client.choose.assert_not_called()
        bridge.capture_wait.assert_called_once_with(2.0)
        events = bridge.mock_calls
        tail = next(index for index, call in enumerate(events) if call[0] == "capture_wait")
        stop = next(index for index, call in enumerate(events) if call[0] == "request" and call.args[0] == "record_stop")
        self.assertLess(tail, stop)
        self.assertTrue(any(call[0] == "release" for call in events[:tail]))

    def test_unrecorded_terminal_session_has_no_capture_tail(self):
        result, _, bridge, _ = self.run_fake(states=[{"seq": 8, "phase": "gameover"}])
        self.assertEqual(result["outcome"], "gameover")
        bridge.capture_wait.assert_not_called()

    def test_combat_with_depleted_known_hp_stops_without_jev_and_keeps_terminal_tail(self):
        dead = demo_state()
        dead["player"]["hp"] = 0
        result, records, bridge, client = self.run_fake(states=[dead], record=True)
        self.assertEqual(result["outcome"], "gameover")
        self.assertEqual(result["requests"], 0)
        self.assertEqual(result["applied_actions"], 0)
        self.assertEqual(result["final_phase"], "combat")  # Preserve what the native bridge actually reported.
        client.choose.assert_not_called()
        bridge.move.assert_not_called()
        bridge.act.assert_not_called()
        bridge.capture_wait.assert_called_once_with(2.0)
        terminal = next(row for row in records if row["event"] == "terminal")
        self.assertEqual(terminal["reason"], "observed_hp_depleted")
        self.assertFalse(terminal["applied"])
        self.assertFalse(any(row["event"] == "decision" for row in records))

    def test_zero_startup_max_hp_does_not_falsely_signal_gameover(self):
        startup = demo_state()
        startup["player"].update(hp=0, max_hp=0)
        result, records, bridge, client = self.run_fake(states=[startup])
        self.assertEqual(result["outcome"], "request_limit")
        client.choose.assert_called_once()
        self.assertFalse(any(row["event"] == "terminal" for row in records))


if __name__ == "__main__":
    unittest.main()
