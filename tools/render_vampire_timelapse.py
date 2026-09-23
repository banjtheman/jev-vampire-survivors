"""Render recorded Vampire Survivors gameplay with Jev decision telemetry.

Starts on the first captured combat frame by default, with combat at 4x and
level-up, chest and other menus at 1x. Uses the existing video encoder and visual
layout; does not control the game or make API calls. All times are recording
elapsed milliseconds, independent of the game's paused or accelerated clock.
"""
from __future__ import annotations

import argparse
from bisect import bisect_left, bisect_right
from dataclasses import asdict, replace
import json
import math
from pathlib import Path
import shutil
import sys

from PIL import ImageDraw

try:
    from . import render_timelapse as base
    from .playback import PlaybackPlan
except ImportError:  # Direct script invocation.
    import render_timelapse as base
    from playback import PlaybackPlan


MENU_ALIASES = {"levelup": "upgrade", "level_up": "upgrade", "chest": "crate"}
TOTAL_KEYS = ("input_tokens", "output_tokens", "requests", "applied_actions",
              "estimated_cost_usd", "unknown_usage_requests")


def observed_phase(row):
    state = row.get("state") or {}
    return row.get("phase") or state.get("phase")


class VampireTimeline(base.Timeline):
    """Adapt recorder aliases, retaining causal decisions and source observations.

    Completion time gates probabilities, usage and selected actions. Observation
    time gates state and playback speed. Keeping those clocks separate prevents
    an inference completed during a level-up screen from restoring its older
    combat phase. Frame telemetry is also an observation, never a model result.
    """

    def __init__(self, session: Path):
        self.session = Path(session).resolve()
        self.metadata = base.read_json(self.session / "metadata.json")
        self.summary = base.read_json(self.session / "summary.json")
        self.frames = base.read_jsonl(self.session / "frames.jsonl")
        self.records = base.read_jsonl(self.session / "decisions.jsonl")
        if not self.frames:
            raise ValueError("No captured frames in frames.jsonl")
        for frame in self.frames:
            frame["path"] = frame.get("path") or frame.get("file")
            path = frame.get("path")
            if not isinstance(path, str):
                raise ValueError("Captured frame requires a file or path")
            resolved = (self.session / path).resolve()
            if not resolved.is_relative_to(self.session) or not resolved.is_file():
                raise ValueError(f"Captured frame is missing or outside session: {path}")
        self.frame_times = [row["elapsed_ms"] for row in self.frames]
        interval = base.number(self.metadata.get("capture_interval_ms"))
        if interval is None:
            interval = 1000 / max(1, base.number(self.metadata.get("capture_fps"), 4))
        self.duration_ms = self.frame_times[-1] + max(1, interval)
        self.decisions = [row for row in self.records if row.get("event") == "decision"
                          or row.get("judgment") or "decision_started_ms" in row or "choice" in row]
        # These aliases are only in memory; original telemetry remains intact.
        for row in self.decisions:
            for candidate in (row.get("candidates") or {}).values():
                if isinstance(candidate, dict) and candidate.get("label") and not candidate.get("name"):
                    candidate["name"] = candidate["label"]
                    if candidate.get("kind") == "upgrade":
                        # Menu labels can contain internal ids (e.g. SILF); the
                        # observed description starts with the displayed name.
                        name = str(candidate.get("description") or "").split("|")[0].strip()
                        if name:
                            candidate["name"] = "Choose " + name
                        candidate["kind"] = "select_upgrade"
        self.decision_times = [row["elapsed_ms"] for row in self.decisions]
        self.record_times = [row["elapsed_ms"] for row in self.records]
        self.cumulative = []
        totals = {"requests": 0, "applied_actions": 0, "unknown_usage_requests": 0}
        for row in self.records:
            judgment = row.get("judgment") or {}
            usage = judgment.get("usage") or {}
            for key in ("input_tokens", "output_tokens"):
                if base.number(usage.get(key)) is not None:
                    totals[key] = totals.get(key, 0) + usage[key]
            totals["requests"] += int(bool(judgment) or row.get("event") == "decision")
            totals["applied_actions"] += int(row.get("applied") is True)
            metrics = row.get("metrics") or {}
            for key in TOTAL_KEYS:
                if base.number(metrics.get(key)) is not None:
                    totals[key] = metrics[key]
            if base.number(metrics.get("estimated_cost_usd")) is None and "input_tokens" in totals:
                input_price = base.number(self.metadata.get("price_usd_per_million_input_tokens"), .042)
                output_price = base.number(self.metadata.get("price_usd_per_million_output_tokens"), 0)
                totals["estimated_cost_usd"] = (totals["input_tokens"] * input_price +
                                                totals.get("output_tokens", 0) * output_price) / 1_000_000
            self.cumulative.append(dict(totals))
        self.final_metrics = dict(totals)
        for key in TOTAL_KEYS:
            if base.number(self.summary.get(key)) is not None:
                self.final_metrics[key] = self.summary[key]
        self.completed_model_times = [row["elapsed_ms"] for row in self.decisions if row.get("judgment")]
        self.pending_windows = sorted(
            (row["decision_started_ms"], row["elapsed_ms"])
            for row in self.decisions
            if base.number(row.get("decision_started_ms")) is not None
            and 0 <= row["decision_started_ms"] < row["elapsed_ms"]
        )
        self.pending_starts = [start for start, _ in self.pending_windows]
        self.pending_ends = []
        maximum = -1
        for _, end in self.pending_windows:
            maximum = max(maximum, end)
            self.pending_ends.append(maximum)
        self.observations = []
        decision_ids = {id(row) for row in self.decisions}
        for row in self.records:
            if not row.get("state") and not observed_phase(row):
                continue
            timestamp = row.get("observation_elapsed_ms")
            if timestamp is None and id(row) in decision_ids:
                timestamp = row.get("decision_started_ms")
            timestamp = row["elapsed_ms"] if timestamp is None else timestamp
            if base.number(timestamp) is None or timestamp < 0 or timestamp > row["elapsed_ms"]:
                raise ValueError("Observation timestamp must be between zero and completion time")
            self.observations.append({"elapsed_ms": timestamp, "state": row.get("state") or {},
                                      "phase": observed_phase(row)})
        for frame in self.frames:
            if observed_phase(frame):
                frame_state = dict(frame.get("state") or {})
                if base.number(frame.get("game_elapsed")) is not None:
                    frame_state["elapsed"] = frame["game_elapsed"]
                self.observations.append({"elapsed_ms": frame["elapsed_ms"],
                                          "state": frame_state, "phase": observed_phase(frame)})
        self.observations.sort(key=lambda row: row["elapsed_ms"])
        # Recorder phase-only rows retain the latest full state, without pulling
        # any later observation backwards to fill a missing field.
        state = {}
        for row in self.observations:
            state = {**state, **row["state"]}
            row["state"] = dict(state)
        self.observation_times = [row["elapsed_ms"] for row in self.observations]
        # If the recorder supplies phases, a displayed menu stays at menu speed
        # until a combat frame exists. The next state poll may arrive earlier.
        self.visual_phases = [{"elapsed_ms": row["elapsed_ms"], "phase": observed_phase(row)}
                              for row in self.frames if observed_phase(row)]
        self.visual_phase_times = [row["elapsed_ms"] for row in self.visual_phases]
        self.playback_observations = self.visual_phases or self.observations

    def at(self, elapsed_ms):
        snapshot = super().at(elapsed_ms)
        index = bisect_right(self.observation_times, elapsed_ms) - 1
        observation = self.observations[index] if index >= 0 else None
        visual_index = bisect_right(self.visual_phase_times, elapsed_ms) - 1
        if visual_index >= 0:
            observation = {**(observation or {}), "phase": self.visual_phases[visual_index]["phase"]}
        return replace(snapshot, observation=observation)

    def combat_start_ms(self):
        """Find a captured frame actually inside the first observed combat interval."""
        for index, row in enumerate(self.playback_observations):
            if observed_phase(row) != "combat":
                continue
            frame_index = bisect_left(self.frame_times, row["elapsed_ms"])
            end = (self.playback_observations[index+1]["elapsed_ms"]
                   if index+1 < len(self.playback_observations) else self.duration_ms)
            if frame_index < len(self.frame_times) and self.frame_times[frame_index] < end:
                return self.frame_times[frame_index]
        raise ValueError("No captured combat frame; use --start-at to render a menu-only recording")


class VampireRenderer(base.Renderer):
    """Reuse the proven telemetry dashboard with game-specific labels and timing."""

    def __init__(self, timeline, speed=4, menu_speed=1, start_ms=None):
        start_ms = timeline.combat_start_ms() if start_ms is None else start_ms
        super().__init__(timeline, speed, menu_speed=menu_speed, start_ms=start_ms)
        records = [{**row, "phase": MENU_ALIASES.get(observed_phase(row), observed_phase(row))}
                   for row in timeline.playback_observations]
        self.playback = PlaybackPlan(records, timeline.duration_ms, speed,
                                     menu_speed=menu_speed, start_ms=start_ms)

    def text(self, draw, xy, text, size=24, fill=base.INK, bold=False, mono=False, max_width=None):
        labels = {"Jev plays Brotato": "Jev plays Vampire Survivors",
                  "JEV × BROTATO": "JEV × VAMPIRE SURVIVORS", "Brotato.": "Vampire Survivors."}
        text = labels.get(text, text)
        if str(text).startswith("Request failed") and getattr(self, "_displayed_decision", {}).get("judgment"):
            text = str(text).replace("Request failed", "Action not applied", 1)
        if text == "Vampire Survivors.":
            size = 68
        if text == "See the recorded result in the game.":
            elapsed = base.number(self.timeline.summary.get("game_elapsed_seconds"))
            if elapsed is not None:
                text = f"Reported survival time: {base.timecode(elapsed * 1000)}"
        return super().text(draw, xy, text, size, fill, bold, mono, max_width)

    def difficulty(self):
        return str(self.timeline.metadata.get("stage") or "Vampire Survivors experiment")

    def draw(self, elapsed_ms):
        snap = self.timeline.at(elapsed_ms)
        self._displayed_decision = snap.decision or {}
        canvas = super().draw(elapsed_ms)
        observation = snap.observation or {}
        state = observation.get("state") or {}
        player = state.get("player") or {}
        parts = []
        elapsed = base.number(state.get("elapsed"))
        if elapsed is not None:
            parts.append("Game " + base.timecode(elapsed * 1000))
        if base.number(player.get("level"), 0) > 0:
            parts.append(f"Lv {base.formatted(player['level'])}")
        if observed_phase(observation):
            parts.append(str(observed_phase(observation)).replace("_", " "))
        draw = ImageDraw.Draw(canvas)
        draw.rectangle((785, 1004, 1405, 1037), fill=base.BG)
        self.text(draw, (790, 1007), " · ".join(parts), 19, base.MUTED, max_width=610)
        return canvas


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--speed", type=float, default=4)
    parser.add_argument("--menu-speed", type=float, default=1)
    parser.add_argument("--start-at", type=float, help="Recorded elapsed seconds; default is first combat frame")
    parser.add_argument("--fps", type=int, default=24)
    parser.add_argument("--outro", type=float, default=3)
    parser.add_argument("--preview-only", action="store_true")
    parser.add_argument("--poster-at", type=float, help="Recorded elapsed seconds; default is first combat frame")
    parser.add_argument("--max-video-seconds", type=float)
    args = parser.parse_args(argv)
    positive = [args.speed, args.menu_speed]
    if args.max_video_seconds is not None:
        positive.append(args.max_video_seconds)
    nonnegative = [args.outro] + [value for value in (args.start_at, args.poster_at) if value is not None]
    if (any(not math.isfinite(value) or value <= 0 for value in positive) or
            any(not math.isfinite(value) or value < 0 for value in nonnegative) or not 1 <= args.fps <= 60):
        parser.error("Speeds/video limit must be positive; timestamps nonnegative; fps must be 1–60")
    try:
        timeline = VampireTimeline(args.session)
        renderer = VampireRenderer(timeline, args.speed, args.menu_speed,
                                   None if args.start_at is None else args.start_at * 1000)
        output = args.output.resolve() if args.output else timeline.session / "jev-vampire-survivors.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        poster_at = renderer.playback.source_start_ms if args.poster_at is None else args.poster_at * 1000
        if poster_at > timeline.duration_ms:
            raise ValueError("Poster timestamp is after the recording")
        poster = output.with_suffix(".png")
        renderer.draw(poster_at).save(poster)
        print(f"Poster: {poster}", flush=True)
        if args.preview_only:
            return 0
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise ValueError("ffmpeg was not found on PATH; poster was generated")
        duration = base.encode(renderer, output, args.fps, 0, args.outro, ffmpeg, args.max_video_seconds)
        edit = {"game": "Vampire Survivors", "source_start_seconds": renderer.playback.source_start_ms / 1000,
                "combat_speed": args.speed, "menu_speed": args.menu_speed, "intro_seconds": 0,
                "outro_seconds": args.outro, "video_seconds": round(duration, 3),
                "segments": [asdict(segment) for segment in renderer.playback.segments]}
        output.with_suffix(".edit.json").write_text(json.dumps(edit, indent=2) + "\n")
        print(json.dumps({"video": str(output), "poster": str(poster), **edit}, indent=2))
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Render failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
