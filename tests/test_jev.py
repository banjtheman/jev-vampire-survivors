"""Contract and failure-path tests; no API calls or real credentials are used."""

from copy import deepcopy
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
import traceback
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from vampire_agent.jev import (
    ENDPOINT,
    JevAPIError,
    JevClient,
    JevConfigurationError,
    JevResponseError,
    load_env,
)


CANDIDATES = {"retreat": {"goal": "avoid enemies"}, "collect": "Collect materials"}
RESPONSE = {
    "model": "jev-test",
    "answers": {"action": {
        "type": "choice", "choice": "retreat", "confidence": 0.7,
        "probabilities": {"retreat": 0.9, "collect": 0.1},
    }},
    "usage": {"input_tokens": 125, "output_tokens": 20},
}


def response_bytes(data=RESPONSE):
    return BytesIO(json.dumps(data).encode())


class JevClientTests(unittest.TestCase):
    def test_official_request_contract_and_metadata(self):
        opener = Mock(return_value=response_bytes())
        client = JevClient("test-key", timeout=2, opener=opener)
        with patch("vampire_agent.jev.time.perf_counter", side_effect=[1.0, 1.25]):
            result = client.choose({"hp": 4}, CANDIDATES, "Which action improves survival?")
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, ENDPOINT)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
        self.assertEqual(opener.call_args.kwargs, {"timeout": 2.0})
        body = json.loads(request.data)
        self.assertEqual(body["model"], "jev-latest")
        self.assertEqual(body["state"], {"hp": 4})
        self.assertEqual(body["questions"]["action"], {
            "type": "choice", "instructions": "Which action improves survival?",
            "criteria": CANDIDATES,
        })
        self.assertEqual(result.choice, "retreat")
        self.assertEqual(result.probabilities, {"retreat": 0.9, "collect": 0.1})
        self.assertEqual(result.confidence, 0.7)
        self.assertEqual(result.latency_ms, 250)
        self.assertEqual(result.model, "jev-test")
        self.assertEqual(result.usage, {"input_tokens": 125, "output_tokens": 20})

    def test_reads_typesafe_api_key_before_legacy_jev_key(self):
        cases = (
            ({"TYPESAFE_API_KEY": "typesafe-key"}, "typesafe-key"),
            ({"JEV_KEY": "legacy-key"}, "legacy-key"),
            ({"TYPESAFE_API_KEY": "typesafe-key", "JEV_KEY": "legacy-key"}, "typesafe-key"),
            ({"TYPESAFE_API_KEY": "  ", "JEV_KEY": "legacy-key"}, "legacy-key"),
        )
        for environment, expected in cases:
            opener = Mock(return_value=response_bytes())
            with self.subTest(environment=sorted(environment)), patch.dict(os.environ, environment, clear=True):
                JevClient(opener=opener).choose({"hp": 4}, CANDIDATES, "Which action improves survival?")
                self.assertEqual(opener.call_args.args[0].get_header("Authorization"), f"Bearer {expected}")

    def test_missing_key_and_invalid_configuration_fail_before_request(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(JevConfigurationError, "TYPESAFE_API_KEY"):
                JevClient()
        for options in ({"timeout": 0}, {"timeout": float("nan")}, {"max_retries": 6}, {"max_retries": True}):
            with self.subTest(options=options), self.assertRaises(JevConfigurationError):
                JevClient("test-key", **options)

    def test_invalid_inputs_do_not_make_a_request(self):
        opener = Mock()
        client = JevClient("test-key", opener=opener)
        invalid = [
            ([], CANDIDATES, "Choose"), ({}, {}, "Choose"),
            ({}, {"a": 7}, "Choose"), ({}, {1: "a"}, "Choose"),
            ({}, CANDIDATES, ""), ({"hp": float("nan")}, CANDIDATES, "Choose"),
            ({"obj": object()}, CANDIDATES, "Choose"),
        ]
        for args in invalid:
            with self.subTest(args=args), self.assertRaises(ValueError):
                client.choose(*args)
        opener.assert_not_called()

    def test_malformed_or_illegal_responses_fail_closed(self):
        modifications = [
            ("choice", "unknown"), ("choice", ["retreat"]),
            ("choice", "collect"), ("type", "noul"),
            ("confidence", float("nan")), ("confidence", True),
            ("probabilities", {"retreat": 1}),
            ("probabilities", {"retreat": 0.8, "collect": 0.8}),
            ("probabilities", {"retreat": 1.2, "collect": -0.2}),
        ]
        for field, value in modifications:
            data = deepcopy(RESPONSE)
            data["answers"]["action"][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(JevResponseError):
                JevClient("test-key", opener=Mock(return_value=response_bytes(data))).choose({}, CANDIDATES, "Choose")
        for data in ({}, [], {**RESPONSE, "usage": {}}, {**RESPONSE, "model": None}):
            with self.subTest(data=data), self.assertRaises(JevResponseError):
                JevClient("test-key", opener=Mock(return_value=response_bytes(data))).choose({}, CANDIDATES, "Choose")

    def test_invalid_json_response(self):
        with self.assertRaises(JevResponseError):
            JevClient("test-key", opener=Mock(return_value=BytesIO(b"not json"))).choose({}, CANDIDATES, "Choose")

    def test_invalid_answer_preserves_valid_paid_usage_metadata(self):
        data = deepcopy(RESPONSE)
        data["answers"]["action"]["choice"] = "illegal"
        with self.assertRaises(JevResponseError) as caught:
            JevClient("test-key", opener=Mock(return_value=response_bytes(data))).choose({}, CANDIDATES, "Choose")
        self.assertEqual(caught.exception.usage, RESPONSE["usage"])
        self.assertEqual(caught.exception.model, RESPONSE["model"])
        self.assertGreaterEqual(caught.exception.latency_ms, 0)

    def test_inconsistent_choice_preserves_only_validated_answer_diagnostics(self):
        data = deepcopy(RESPONSE)
        data["answers"]["action"].update(choice="collect", debug="pretend-private-key")
        data["headers"] = {"Authorization": "Bearer pretend-private-key"}
        data["answers"]["other_question"] = {"secret": "pretend-private-key"}
        with self.assertRaisesRegex(JevResponseError, "highest probability") as caught:
            JevClient("test-key", opener=Mock(return_value=response_bytes(data))).choose({}, CANDIDATES, "Choose")
        self.assertEqual(caught.exception.answer, {
            "type": "choice", "choice": "collect", "confidence": 0.7,
            "probabilities": {"retreat": 0.9, "collect": 0.1},
        })
        self.assertNotIn("pretend-private-key", json.dumps(caught.exception.answer))
        self.assertEqual(caught.exception.usage, RESPONSE["usage"])

    def test_diagnostics_omit_unknown_ids_and_invalid_probability_values(self):
        data = deepcopy(RESPONSE)
        data["answers"]["action"].update(
            choice="untrusted-server-text", confidence=float("nan"),
            probabilities={"retreat": 1.2, "collect": -0.2},
        )
        with self.assertRaises(JevResponseError) as caught:
            JevClient("test-key", opener=Mock(return_value=response_bytes(data))).choose({}, CANDIDATES, "Choose")
        self.assertIsNone(caught.exception.answer)
        # Independently valid fields remain useful when the chosen ID is invalid.
        data["answers"]["action"]["confidence"] = 0.7
        with self.assertRaises(JevResponseError) as caught:
            JevClient("test-key", opener=Mock(return_value=response_bytes(data))).choose({}, CANDIDATES, "Choose")
        self.assertEqual(caught.exception.answer, {"type": "choice", "confidence": 0.7})

    def test_default_transport_reuses_connection_without_replaying_failure(self):
        from vampire_agent.jev import _PersistentHTTPS
        from urllib.request import Request
        request = Request(ENDPOINT, data=b"{}", method="POST")
        with patch("vampire_agent.jev.HTTPSConnection") as factory:
            connection = factory.return_value
            response = Mock(status=200)
            connection.getresponse.return_value = response
            transport = _PersistentHTTPS()
            self.assertIs(transport(request, timeout=2), response)
            self.assertIs(transport(request, timeout=2), response)
            factory.assert_called_once()
            self.assertEqual(connection.request.call_count, 2)
            connection.request.side_effect = OSError("broken")
            with self.assertRaises(OSError):
                transport(request, timeout=2)
            self.assertEqual(connection.request.call_count, 3)
            self.assertIsNone(transport.connection)

    def test_two_decimal_rounding_is_accepted_without_normalizing_raw_values(self):
        candidates = {"west": "Move west", "southwest": "Move southwest", "stay": "Wait"}
        # Each value is a plausible independent rounding of a normalized
        # distribution: [.334,.333,.333] and [.336,.336,.328], respectively.
        distributions = [
            {"west": 0.33, "southwest": 0.33, "stay": 0.33},
            {"west": 0.34, "southwest": 0.34, "stay": 0.33},
        ]
        for probabilities in distributions:
            data = deepcopy(RESPONSE)
            data["answers"]["action"].update(choice="west", probabilities=probabilities)
            with self.subTest(probabilities=probabilities):
                result = JevClient("test-key", opener=Mock(return_value=response_bytes(data))).choose({}, candidates, "Choose")
                self.assertEqual(result.probabilities, probabilities)

    def test_rounding_tolerance_does_not_accept_corrupt_distributions(self):
        distributions = [
            {"retreat": 0.0, "collect": 0.0},
            {"retreat": 0.9, "collect": 0.3},
            {"retreat": 0.8, "collect": 0.18},
            # Extra precision means cent-rounding cannot explain this deficit.
            {"retreat": 0.9001, "collect": 0.0901},
            # A large option set must not produce an unbounded tolerance.
            {str(index): 0.01 for index in range(200)},
        ]
        for probabilities in distributions:
            data = deepcopy(RESPONSE)
            data["answers"]["action"].update(choice=next(iter(probabilities)), probabilities=probabilities)
            candidates = {key: None for key in probabilities}
            with self.subTest(total=sum(probabilities.values())), self.assertRaises(JevResponseError):
                JevClient("test-key", opener=Mock(return_value=response_bytes(data))).choose({}, candidates, "Choose")

    def test_retries_only_rate_limit_or_overload_and_are_bounded(self):
        for status in (429, 529):
            failures = [HTTPError(ENDPOINT, status, "error", {}, BytesIO()) for _ in range(2)]
            opener = Mock(side_effect=failures + [response_bytes()])
            with patch("vampire_agent.jev.time.sleep") as sleep:
                result = JevClient("test-key", max_retries=2, opener=opener).choose({}, CANDIDATES, "Choose")
            self.assertEqual(result.choice, "retreat")
            self.assertEqual(opener.call_count, 3)
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.5, 1.0])
        opener = Mock(side_effect=[HTTPError(ENDPOINT, 429, "error", {}, BytesIO()) for _ in range(3)])
        with patch("vampire_agent.jev.time.sleep"), self.assertRaises(JevAPIError):
            JevClient("test-key", max_retries=1, opener=opener).choose({}, CANDIDATES, "Choose")
        self.assertEqual(opener.call_count, 2)

    def test_default_has_no_retries_and_secrets_never_reach_traceback(self):
        secret = "pretend-private-key"
        errors = [
            HTTPError(ENDPOINT, 401, secret, {}, BytesIO(secret.encode())),
            HTTPError(ENDPOINT, 429, secret, {}, BytesIO(secret.encode())),
            URLError(secret), TimeoutError(secret),
        ]
        for error in errors:
            opener = Mock(side_effect=error)
            try:
                JevClient(secret, opener=opener).choose({}, CANDIDATES, "Choose")
            except JevAPIError:
                self.assertNotIn(secret, traceback.format_exc())
            else:
                self.fail("Expected JevAPIError")
            self.assertEqual(opener.call_count, 1)

    def test_retry_after_too_long_does_not_retry_early(self):
        opener = Mock(side_effect=HTTPError(ENDPOINT, 429, "error", {"Retry-After": "60"}, BytesIO()))
        with patch("vampire_agent.jev.time.sleep") as sleep, self.assertRaises(JevAPIError):
            JevClient("test-key", max_retries=2, opener=opener).choose({}, CANDIDATES, "Choose")
        sleep.assert_not_called()
        self.assertEqual(opener.call_count, 1)


class EnvironmentTests(unittest.TestCase):
    def test_loads_values_without_expansion_and_respects_existing_environment(self):
        content = "# comment\nJEV_KEY='local-key'\nexport LABEL=\"value # literal\" # comment\nREF=${JEV_KEY}\nHASH=one#two\nEMPTY=\n"
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"JEV_KEY": "existing-key"}, clear=True):
            path = Path(directory) / ".env"
            path.write_text(content)
            self.assertEqual(load_env(path), {"LABEL", "REF", "HASH", "EMPTY"})
            self.assertEqual(os.environ["JEV_KEY"], "existing-key")
            self.assertEqual(os.environ["LABEL"], "value # literal")
            self.assertEqual(os.environ["REF"], "${JEV_KEY}")
            self.assertEqual(os.environ["HASH"], "one#two")
            self.assertEqual(os.environ["EMPTY"], "")

    def test_invalid_file_does_not_partially_load_or_expose_value(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = Path(directory) / ".env"
            path.write_text("JEV_KEY=secret-token\nBAD='secret-token\n")
            with self.assertRaises(JevConfigurationError) as caught:
                load_env(path)
            self.assertNotIn("secret-token", str(caught.exception))
            self.assertNotIn("JEV_KEY", os.environ)
            self.assertEqual(load_env(Path(directory) / "absent"), set())


if __name__ == "__main__":
    unittest.main()
