"""A small, dependency-free client for TypeSafe's typed Choice API.

API contract: https://docs.typesafe.ai/api
Jev sees structured text, so callers must supply observed game state, not images.
The caller owns legal action generation, freshness checks and action execution.
"""

from __future__ import annotations

from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from http.client import HTTPException, HTTPSConnection
import json
import math
import os
from pathlib import Path
import re
import shlex
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


ENDPOINT = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
# TypeSafe's standard variable first; JEV_KEY keeps older .env files working.
KEY_NAMES = ("TYPESAFE_API_KEY", "JEV_KEY")
_QUESTION_ID = "action"
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class JevError(RuntimeError):
    """Base error with messages safe to log (no credentials or response bodies)."""


class JevConfigurationError(JevError):
    """Missing credentials or invalid local configuration."""


class JevAPIError(JevError):
    """HTTP or transport failure. ``status_code`` is absent for network errors."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class JevResponseError(JevError):
    """An unusable decision with optional, validated fields for diagnostics.

    ``answer`` is a partial Choice answer, never an actionable judgment. It can
    retain a bad probability sum or choice/distribution mismatch for inspection,
    but excludes unknown fields, unknown candidate IDs and out-of-range values.
    """

    usage = None
    model = None
    latency_ms = None
    answer = None


@dataclass(frozen=True)
class Judgment:
    choice: str
    confidence: float
    probabilities: dict[str, float]
    latency_ms: float
    model: str
    usage: dict[str, int]


def load_env(path: str | Path) -> set[str]:
    """Load a simple .env file without executing or interpolating its contents.

    Supports KEY=value, optional ``export``, single/double quotes and comments.
    Existing environment variables win, including variables set to empty strings.
    Missing files are allowed. Returns loaded variable *names*, never values.
    Multiline values and shell expansion are intentionally unsupported.
    """
    try:
        content = Path(path).read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return set()
    except (OSError, UnicodeError):
        raise JevConfigurationError("Could not read the environment file.") from None

    pending: dict[str, str] = {}
    for number, raw in enumerate(content.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)", line)
        if not match:
            raise JevConfigurationError(f"Invalid environment assignment on line {number}.")
        name, value = match.groups()
        if value.startswith(("'", '"')):
            try:
                parts = shlex.split(value, comments=True, posix=True)
                if len(parts) != 1:
                    raise ValueError
                value = parts[0]
            except ValueError:
                raise JevConfigurationError(f"Invalid quoted value on line {number}.") from None
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        if "\x00" in value:
            raise JevConfigurationError(f"Invalid environment value on line {number}.")
        pending[name] = value

    loaded = set()
    for name, value in pending.items():
        if name not in os.environ:
            os.environ[name] = value
            loaded.add(name)
    return loaded


def _environment_key() -> str:
    """Return the first nonempty variable named in KEY_NAMES, or an empty string."""
    for name in KEY_NAMES:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward bearer credentials to a redirected destination.
        return None


class _PersistentHTTPS:
    """One sequential TLS connection, restricted to the official API host.

    A failed connection is closed, never silently replayed against old game state.
    """
    def __init__(self):
        self.connection = None

    def __call__(self, request, *, timeout):
        if request.full_url != ENDPOINT:
            raise ValueError("Only the official Jev endpoint is allowed")
        if self.connection is None:
            self.connection = HTTPSConnection("api.typesafe.ai", timeout=timeout)
        try:
            self.connection.timeout = timeout
            if self.connection.sock is not None:
                self.connection.sock.settimeout(timeout)
            self.connection.request("POST", "/v1/systemone", body=request.data,
                                    headers=dict(request.header_items()))
            response = self.connection.getresponse()
            if response.status != 200:
                error = HTTPError(ENDPOINT, response.status, "API status", response.headers, None)
                self.close()
                raise error
            return response
        except (OSError, HTTPException):
            self.close()
            raise

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None


class JevClient:
    """Select one supplied candidate with Jev, preserving the raw judgment.

    ``timeout`` is the socket timeout in seconds for each attempt, not a total
    wall-clock deadline. ``max_retries`` retries only HTTP 429/529, at most five
    times, with delays of 0.5–5 seconds. Keep it zero for changing combat state.
    ``opener`` can be an injected callable with urllib's ``(request, timeout=)``
    contract, which keeps tests offline. The normal endpoint is fixed to TypeSafe.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        timeout: float = 10.0,
        max_retries: int = 0,
        opener: Callable | None = None,
    ):
        key = _environment_key() if api_key is None else api_key
        if not isinstance(key, str) or not key.strip():
            raise JevConfigurationError("Set TYPESAFE_API_KEY in the environment or load your .env first.")
        if any(ord(char) < 33 or ord(char) > 126 for char in key.strip()):
            raise JevConfigurationError("The API key must be a single printable ASCII token.")
        if isinstance(timeout, bool) or not isinstance(timeout, (float, int)) or not math.isfinite(timeout) or timeout <= 0:
            raise JevConfigurationError("timeout must be a positive finite number of seconds.")
        if type(max_retries) is not int or not 0 <= max_retries <= 5:
            raise JevConfigurationError("max_retries must be an integer from 0 through 5.")
        self._key = key.strip()
        self.timeout = float(timeout)
        self.max_retries = max_retries
        self._open = opener if opener is not None else _PersistentHTTPS()

    def close(self):
        if isinstance(self._open, _PersistentHTTPS):
            self._open.close()

    def choose(
        self,
        state: dict,
        candidates: dict[str, object],
        instructions: str,
    ) -> Judgment:
        """Choose from 1–255 legal option IDs described by JSON criteria.

        Criteria values must be strings, dictionaries, lists or None. For game
        actions, descriptions should include relevant consequences; exact rules
        and legal action filtering belong in the caller's code.
        """
        if not isinstance(state, dict):
            raise ValueError("state must be a dictionary of observed facts.")
        if not isinstance(candidates, dict) or not 1 <= len(candidates) <= 255:
            raise ValueError("candidates must contain between 1 and 255 options.")
        if any(not isinstance(key, str) or not key for key in candidates):
            raise ValueError("Candidate IDs must be nonempty strings.")
        if any(value is not None and not isinstance(value, (str, dict, list)) for value in candidates.values()):
            raise ValueError("Candidate descriptions must be strings, objects, arrays or None.")
        if not isinstance(instructions, str) or not instructions.strip():
            raise ValueError("instructions must be a nonempty question.")
        try:
            body = json.dumps({
                "model": MODEL,
                "state": state,
                "questions": {
                    _QUESTION_ID: {
                        "type": "choice",
                        "instructions": instructions,
                        "criteria": candidates,
                    }
                },
            }, allow_nan=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError, OverflowError, RecursionError):
            raise ValueError("State and candidates must contain finite, JSON-serializable data.") from None

        started = time.perf_counter()
        for attempt in range(self.max_retries + 1):
            request = Request(ENDPOINT, data=body, method="POST", headers={
                "Authorization": f"Bearer {self._key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            })
            try:
                with self._open(request, timeout=self.timeout) as response:
                    raw = response.read(_MAX_RESPONSE_BYTES + 1)
                break
            except HTTPError as error:
                status = error.code
                delay = _retry_delay(error, attempt)
                error.close()
                if status in (429, 529) and attempt < self.max_retries and delay <= 5.0:
                    time.sleep(delay)
                    continue
                message = {
                    401: "Jev authentication failed; check TYPESAFE_API_KEY or JEV_KEY.",
                    403: "Jev denied access for this credential.",
                    422: "Jev rejected the request schema; check state and candidate descriptions.",
                    429: "Jev rate limit reached; try again with fresh state later.",
                    529: "Jev is temporarily overloaded; try again with fresh state later.",
                }.get(status, f"Jev request failed with HTTP {status}.")
                raise JevAPIError(message, status) from None
            except TimeoutError:
                self.close()
                raise JevAPIError("Jev request timed out; discard this decision and observe fresh state.") from None
            except (URLError, OSError, HTTPException):
                self.close()
                raise JevAPIError("Could not reach or read from Jev; check the network connection.") from None

        latency_ms = (time.perf_counter() - started) * 1000
        if len(raw) > _MAX_RESPONSE_BYTES:
            self.close()
            raise JevResponseError("Jev response exceeded the allowed size.")
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError):
            raise JevResponseError("Jev returned invalid JSON.") from None
        try:
            return _parse_judgment(data, set(candidates), latency_ms)
        except JevResponseError as error:
            # A rejected decision can still incur billed input tokens. Preserve
            # only validated accounting metadata and answer fields, never
            # headers, arbitrary server fields or the raw response.
            if isinstance(data, dict):
                usage = data.get("usage")
                if isinstance(usage, dict) and all(type(usage.get(k)) is int and usage[k] >= 0
                                                   for k in ("input_tokens", "output_tokens")):
                    error.usage = {k: usage[k] for k in ("input_tokens", "output_tokens")}
                if isinstance(data.get("model"), str):
                    error.model = data["model"]
                answers = data.get("answers")
                if isinstance(answers, dict):
                    error.answer = _answer_diagnostics(answers.get(_QUESTION_ID), set(candidates))
            error.latency_ms = latency_ms
            raise


def _retry_delay(error: HTTPError, attempt: int) -> float:
    """Respect a server delay; the caller skips waits longer than five seconds."""
    backoff = min(0.5 * 2 ** attempt, 5.0)
    value = error.headers.get("Retry-After") if error.headers else None
    if value is not None:
        try:
            delay = float(value)
        except (ValueError, TypeError):
            try:
                delay = parsedate_to_datetime(value).timestamp() - time.time()
            except (ValueError, TypeError, OverflowError):
                return backoff
        if math.isfinite(delay):
            return max(backoff, delay)
    return backoff


def _probability(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1


def _answer_diagnostics(answer: object, candidates: set[str]) -> dict | None:
    """Copy only known, individually valid fields from an invalid decision."""
    if not isinstance(answer, dict) or answer.get("type") != "choice":
        return None
    safe = {"type": "choice"}
    choice = answer.get("choice")
    if isinstance(choice, str) and choice in candidates:
        safe["choice"] = choice
    probabilities = answer.get("probabilities")
    if isinstance(probabilities, dict) and set(probabilities) == candidates and all(
        type(value) in (int, float) and 0 <= value <= 1 and math.isfinite(value)
        for value in probabilities.values()
    ):
        safe["probabilities"] = dict(probabilities)
    confidence = answer.get("confidence")
    if type(confidence) in (int, float) and 0 <= confidence <= 1 and math.isfinite(confidence):
        safe["confidence"] = confidence
    return safe if len(safe) > 1 else None


def _parse_judgment(data: object, candidates: set[str], latency_ms: float) -> Judgment:
    if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
        raise JevResponseError("Jev response is missing its answers object.")
    answer = data["answers"].get(_QUESTION_ID)
    if not isinstance(answer, dict) or answer.get("type") != "choice":
        raise JevResponseError("Jev response is missing the requested Choice answer.")
    choice = answer.get("choice")
    if not isinstance(choice, str) or choice not in candidates:
        raise JevResponseError("Jev selected an action outside the supplied candidates.")
    probabilities = answer.get("probabilities")
    if not isinstance(probabilities, dict) or set(probabilities) != candidates:
        raise JevResponseError("Jev probabilities do not match the supplied candidates.")
    if not all(_probability(value) for value in probabilities.values()):
        raise JevResponseError("Jev returned an invalid probability.")
    # Live API answers round probabilities to two decimal places. Independent
    # rounding can move their sum away from one by up to .005 per option. Keep
    # the original values for logging; accept only that bounded rounding error,
    # capped at five percentage points even for very large candidate sets.
    rounded_to_cents = all(
        math.isclose(value * 100, round(value * 100), rel_tol=0, abs_tol=1e-8)
        for value in probabilities.values()
    )
    sum_tolerance = min(0.05, 0.005 * len(probabilities)) if rounded_to_cents else 0.001
    if not math.isclose(math.fsum(probabilities.values()), 1.0, rel_tol=0, abs_tol=sum_tolerance + 1e-9):
        raise JevResponseError("Jev probabilities do not sum to one.")
    if probabilities[choice] + 0.000001 < max(probabilities.values()):
        raise JevResponseError("Jev choice does not match its highest probability.")
    confidence = answer.get("confidence")
    if not _probability(confidence):
        raise JevResponseError("Jev returned invalid confidence.")
    model, usage = data.get("model"), data.get("usage")
    if not isinstance(model, str) or not model.strip():
        raise JevResponseError("Jev response is missing the model name.")
    if not isinstance(usage, dict) or any(
        type(usage.get(key)) is not int or usage[key] < 0
        for key in ("input_tokens", "output_tokens")
    ):
        raise JevResponseError("Jev response is missing valid token usage.")
    return Judgment(
        choice=choice,
        confidence=float(confidence),
        probabilities={key: float(value) for key, value in probabilities.items()},
        latency_ms=latency_ms,
        model=model,
        usage={key: usage[key] for key in ("input_tokens", "output_tokens")},
    )
