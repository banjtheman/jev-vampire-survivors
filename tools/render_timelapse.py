"""Shared rendering engine retained from the original Jev Brotato harness.

Vampire Survivors uses render_vampire_timelapse.py, which adapts this engine's
layout and telemetry timeline. Original Brotato labels below remain internal
base-class defaults; no Brotato game/controller dependency is required.

Requires Pillow and ffmpeg. No game control or API calls are performed.
Input timestamps are recording-relative milliseconds; game time is never inferred
from wall time. Render only after the recorder has finished writing its files.
"""
from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import deque
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont, ImageOps

try:
    from .playback import PlaybackPlan
except ImportError:  # Direct invocation: python tools/render_timelapse.py
    from playback import PlaybackPlan


WIDTH, HEIGHT = 1920, 1080
BG = "#0b1017"
PANEL = "#131d27"
LINE = "#283742"
INK = "#f2f5ef"
MUTED = "#93a5ae"
GREEN = "#c1f66c"
CYAN = "#71d9e5"
AMBER = "#ffc37a"
ORDER = ["north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest", "stay"]
SHORT = dict(zip(ORDER, ["N", "NE", "E", "SE", "S", "SW", "W", "NW", "STAY"]))


def number(value, default=None):
    return float(value) if type(value) in (int, float) and math.isfinite(value) else default


def read_json(path: Path):
    return json.loads(path.read_text()) if path.exists() else {}


def read_jsonl(path: Path):
    if not path.exists():
        return []
    rows = []
    lines = path.read_text().splitlines()
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            if index == len(lines) - 1:
                print(f"Ignoring unfinished final line in {path.name}", file=sys.stderr)
                break
            raise ValueError(f"Invalid JSON in {path.name}, line {index + 1}") from None
        if not isinstance(row, dict):
            raise ValueError(f"Expected an object in {path.name}, line {index + 1}")
        if number(row.get("elapsed_ms")) is None or row["elapsed_ms"] < 0:
            raise ValueError(f"Missing/nonfinite elapsed_ms in {path.name}, line {index + 1}")
        rows.append(row)
    return sorted(rows, key=lambda row: row["elapsed_ms"])


def timecode(milliseconds):
    total = max(0, int(milliseconds / 1000))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


def formatted(value, digits=0):
    if number(value) is None:
        return "—"
    return f"{value:,.{digits}f}"


def candidate_label(key, candidate):
    if not isinstance(candidate, dict) or not candidate.get("name"):
        return str(key).replace("_", " ")
    verb = {"buy_item": "Buy ", "buy_weapon": "Buy ", "upgrade": "Upgrade ",
            "take_item": "Take ", "recycle_crate": "Recycle ", "combine": "Combine ",
            "recycle_weapon": "Recycle "}.get(candidate.get("kind"), "")
    return verb + candidate["name"]


@dataclass
class Snapshot:
    frame: dict | None
    decision: dict | None
    observation: dict | None
    pending: bool
    metrics: dict
    results_per_second: float
    elapsed_ms: float


class Timeline:
    """A causal join: completed records are visible only at their own timestamp."""

    def __init__(self, session: Path):
        self.session = session
        self.metadata = read_json(session / "metadata.json")
        self.summary = read_json(session / "summary.json")
        self.frames = read_jsonl(session / "frames.jsonl")
        self.records = read_jsonl(session / "decisions.jsonl")
        # Phase records contain fresh telemetry/accounting, but are not decisions.
        self.decisions = [row for row in self.records if row.get("event") == "decision"
                          or row.get("judgment") or "decision_started_ms" in row or "choice" in row]
        if not self.frames:
            raise ValueError("No captured frames in frames.jsonl")
        self.frame_times = [row["elapsed_ms"] for row in self.frames]
        self.decision_times = [row["elapsed_ms"] for row in self.decisions]
        self.record_times = [row["elapsed_ms"] for row in self.records]
        for frame in self.frames:
            if not isinstance(frame.get("path"), str) or not (session / frame["path"]).is_file():
                raise ValueError(f"Captured frame is missing: {frame.get('path')}")
        interval = number(self.metadata.get("capture_interval_ms"), 250)
        self.duration_ms = self.frame_times[-1] + max(1, interval)
        # Do not extrapolate unseen gameplay beyond the capture interval.
        self.duration_ms = max(self.duration_ms, 1)
        self.cumulative = []
        inputs = outputs = requests = applied = 0
        inputs_known = outputs_known = False
        for row in self.records:
            judgment = row.get("judgment") or {}
            usage = judgment.get("usage") or {}
            if number(usage.get("input_tokens")) is not None:
                inputs += usage["input_tokens"]
                inputs_known = True
            if number(usage.get("output_tokens")) is not None:
                outputs += usage["output_tokens"]
                outputs_known = True
            requests += int(bool(judgment) or row.get("policy") == "jev")
            applied += int(row.get("applied") is True)
            metrics = row.get("metrics") or {}
            totals = {
                "input_tokens": metrics.get("input_tokens", inputs if inputs_known else None),
                "output_tokens": metrics.get("output_tokens", outputs if outputs_known else None),
                "requests": metrics.get("requests", requests),
                "applied_actions": metrics.get("applied_actions", applied),
                "estimated_cost_usd": metrics.get("estimated_cost_usd"),
                "unknown_usage_requests": metrics.get("unknown_usage_requests", 0),
            }
            if number(totals["estimated_cost_usd"]) is None and number(totals["input_tokens"]) is not None:
                input_price = number(self.metadata.get("price_usd_per_million_input_tokens"), .042)
                output_price = number(self.metadata.get("price_usd_per_million_output_tokens"), 0)
                totals["estimated_cost_usd"] = (totals["input_tokens"] * input_price +
                                                (totals["output_tokens"] or 0) * output_price) / 1_000_000
            self.cumulative.append(totals)
        self.final_metrics = dict(self.cumulative[-1]) if self.cumulative else {}
        # Summary is authoritative at the end, including usage from a request that
        # was interrupted after its response but before its decision was logged.
        for key in ("input_tokens", "output_tokens", "requests", "applied_actions",
                    "estimated_cost_usd", "unknown_usage_requests"):
            if number(self.summary.get(key)) is not None:
                self.final_metrics[key] = self.summary[key]
        self.completed_model_times = [row["elapsed_ms"] for row in self.decisions if row.get("judgment")]
        self.pending_windows = sorted(
            (row["decision_started_ms"], row["elapsed_ms"])
            for row in self.decisions
            if number(row.get("decision_started_ms")) is not None
            and 0 <= row["decision_started_ms"] < row["elapsed_ms"]
        )
        self.pending_starts = [start for start, _ in self.pending_windows]
        # Prefix maximum supports overlapping requests without peeking at results.
        self.pending_ends = []
        maximum = -1
        for _, end in self.pending_windows:
            maximum = max(maximum, end)
            self.pending_ends.append(maximum)

    def at(self, elapsed_ms):
        frame_index = bisect_right(self.frame_times, elapsed_ms) - 1
        decision_index = bisect_right(self.decision_times, elapsed_ms) - 1
        record_index = bisect_right(self.record_times, elapsed_ms) - 1
        pending_index = bisect_right(self.pending_starts, elapsed_ms) - 1
        window = min(10_000, max(1, elapsed_ms))
        count = bisect_right(self.completed_model_times, elapsed_ms) - bisect_right(
            self.completed_model_times, max(-1, elapsed_ms - window))
        return Snapshot(
            frame=self.frames[frame_index] if frame_index >= 0 else None,
            decision=self.decisions[decision_index] if decision_index >= 0 else None,
            observation=self.records[record_index] if record_index >= 0 else None,
            pending=pending_index >= 0 and self.pending_ends[pending_index] > elapsed_ms,
            metrics=self.cumulative[record_index] if record_index >= 0 else {},
            results_per_second=count / (window / 1000),
            elapsed_ms=elapsed_ms,
        )


def load_font(size, bold=False, mono=False):
    paths = (["/System/Library/Fonts/Menlo.ttc", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"]
             if mono else
             ["/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"])
    for path in paths:
        if Path(path).is_file():
            return ImageFont.truetype(path, size=size)
    return ImageFont.load_default(size=size)


class Renderer:
    def __init__(self, timeline: Timeline, speed: float, menu_speed=None, start_ms=0):
        self.timeline = timeline
        self.speed = speed
        self.menu_speed = menu_speed
        self.playback = PlaybackPlan(timeline.records, timeline.duration_ms, speed,
                                     menu_speed=menu_speed, start_ms=start_ms)
        self.fonts = {}
        self.cache = deque(maxlen=2)

    def font(self, size, bold=False, mono=False):
        key = (size, bold, mono)
        if key not in self.fonts:
            self.fonts[key] = load_font(*key)
        return self.fonts[key]

    def text(self, draw, xy, text, size=24, fill=INK, bold=False, mono=False, max_width=None):
        text = str(text)
        font = self.font(size, bold, mono)
        if max_width:
            original = text
            while text and draw.textlength(text, font=font) > max_width:
                text = text[:-1]
            if text != original:
                text = text[:-2] + "…"
        draw.text(xy, text, font=font, fill=fill)

    def panel(self, draw, box, fill=PANEL):
        draw.rounded_rectangle(box, radius=16, fill=fill, outline=LINE, width=1)

    def source_image(self, frame):
        path = self.timeline.session / frame["path"]
        for cached_path, cached_image in self.cache:
            if cached_path == path:
                return cached_image
        with Image.open(path) as raw:
            picture = raw.convert("RGB")
        self.cache.append((path, picture))
        return picture

    def picture(self, canvas, frame, box):
        if frame is None:
            return
        x, y, w, h = box
        picture = ImageOps.contain(self.source_image(frame), (w, h), Image.Resampling.LANCZOS)
        canvas.paste(picture, (x + (w-picture.width)//2, y + (h-picture.height)//2))

    def difficulty(self):
        difficulty = self.timeline.metadata.get("difficulty", self.timeline.metadata.get("danger"))
        return f"Danger {difficulty}" if type(difficulty) in (int, float) else str(difficulty or "Brotato experiment")

    def playback_description(self):
        if self.menu_speed is not None:
            return f"{self.speed:g}× combat / {self.menu_speed:g}× menus"
        return f"{self.speed:g}× footage"

    def draw(self, elapsed_ms):
        snap = self.timeline.at(elapsed_ms)
        canvas = Image.new("RGB", (WIDTH, HEIGHT), BG)
        draw = ImageDraw.Draw(canvas)
        decision = snap.decision or {}
        observation = snap.observation or {}
        state = observation.get("state") or {}
        judgment = decision.get("judgment") or {}
        metrics = snap.metrics
        is_fixture = self.timeline.metadata.get("fixture", False)

        self.text(draw, (42, 32), "SYSTEM ONE / GAMEPLAY EXPERIMENT", 18, MUTED, mono=True)
        self.text(draw, (40, 64), "Jev plays Brotato", 54, bold=True)
        self.text(draw, (42, 130), self.difficulty() + "   ·   Structured state → typed choice → game input", 22, MUTED)
        draw.rounded_rectangle((1510, 49, 1880, 110), 30, fill=GREEN)
        current_speed = self.playback.speed_at(elapsed_ms)
        phase = observation.get("phase") or state.get("phase")
        speed_kind = ("COMBAT" if phase == "combat" else "RESULT" if phase in ("gameover", "victory") else "MENUS") if self.menu_speed is not None else "TIMELAPSE"
        self.text(draw, (1536, 65), f"{current_speed:g}× {speed_kind}", 26, BG, bold=True)
        self.text(draw, (1520, 126), "SYNTHETIC FIXTURE" if is_fixture else "ACTUAL RECORDED GAMEPLAY", 17, MUTED, mono=True)

        # The capture is fit without cropping, preserving the entire recorded game.
        draw.rectangle((40, 190, 1400, 955), fill="#05080b")
        self.picture(canvas, snap.frame, (40, 190, 1360, 765))
        draw = ImageDraw.Draw(canvas)
        draw.rectangle((40, 190, 1400, 955), outline=LINE, width=2)
        if snap.frame is None:
            self.text(draw, (400, 552), "Waiting for the first recorded frame", 30, MUTED)
        if is_fixture:
            draw.rectangle((40, 912, 1400, 955), fill=BG)
            self.text(draw, (68, 922), "SYNTHETIC TEST IMAGE — NO GAMEPLAY CLAIM", 20, AMBER, mono=True)

        x = 1440
        self.panel(draw, (x, 190, 1880, 345))
        self.text(draw, (x+24, 211), "LAST COMPLETED DECISION", 16, MUTED, mono=True)
        choice = decision.get("choice") or judgment.get("choice")
        accent = GREEN if decision.get("applied") is True else AMBER
        selected = (decision.get("candidates") or {}).get(choice) or {}
        action_label = str(choice).replace("_", " ") if choice else "No result" if decision else "Waiting"
        if choice not in ORDER and isinstance(selected, dict) and selected.get("name"):
            action_label = candidate_label(choice, selected)
        self.text(draw, (x+22, 238), action_label.upper(), 34, accent if choice else MUTED, bold=True, max_width=392)
        if decision:
            status = "Applied to game" if decision.get("applied") is True else "Not applied"
            if decision.get("error"):
                status = "Request failed"
            age = max(0, elapsed_ms-decision["elapsed_ms"])/1000
            self.text(draw, (x+24, 285), f"{status}  ·  {age:.1f}s ago", 20, MUTED)
        self.text(draw, (x+24, 313), "INFERENCE IN FLIGHT" if snap.pending else "No pending recorded request", 15, CYAN if snap.pending else MUTED, mono=True)

        self.panel(draw, (x, 363, 1880, 680))
        probabilities = judgment.get("probabilities") or {}
        truncated = len(probabilities) > 8 and not all(key in ORDER for key in probabilities)
        self.text(draw, (x+24, 383), "CHOICE PROBABILITIES" + (" · TOP 8" if truncated else ""), 17, MUTED, mono=True)
        if probabilities:
            directional = all(key in ORDER for key in probabilities)
            keys = [key for key in ORDER if key in probabilities] if directional else sorted(
                probabilities, key=lambda key: number(probabilities[key], 0), reverse=True)[:8]
            line_height = 27 if directional else 30
            for index, key in enumerate(keys):
                y = 420 + index*line_height
                probability = number(probabilities[key])
                if probability is None:
                    continue
                probability = min(1, max(0, probability))
                label_width = 58 if directional else 180
                bar_x = x+24+label_width
                bar_width = 264 if directional else 140
                fill = accent if key == choice else "#547983"
                candidate = (decision.get("candidates") or {}).get(key) or {}
                label = candidate_label(key, candidate)
                self.text(draw, (x+24, y-2), SHORT[key] if directional else label, 17, INK, mono=directional, max_width=label_width-6)
                draw.rounded_rectangle((bar_x, y+3, bar_x+bar_width, y+17), 6, fill="#26353e")
                if probability > 0:
                    draw.rounded_rectangle((bar_x, y+3, bar_x+max(2, bar_width*probability), y+17), 6, fill=fill)
                self.text(draw, (x+354, y-2), f"{probability*100:.0f}%", 17, INK, mono=True)
        else:
            self.text(draw, (x+24, 450), "No model distribution", 24, MUTED)
            self.text(draw, (x+24, 487), "available at this timestamp.", 20, MUTED)

        self.panel(draw, (x, 698, 1880, 955))
        latency = number(judgment.get("latency_ms"))
        self.text(draw, (x+24, 718), "API LATENCY", 15, MUTED, mono=True)
        self.text(draw, (x+244, 718), "RESULTS / SEC", 15, MUTED, mono=True)
        self.text(draw, (x+24, 741), formatted(latency) + (" ms" if latency is not None else ""), 30, CYAN, bold=True)
        self.text(draw, (x+244, 741), f"{snap.results_per_second:.2f}", 30, INK, bold=True)
        self.text(draw, (x+244, 778), "last 10s, real time", 13, MUTED)
        draw.line((x+24, 807, 1856, 807), fill=LINE, width=1)
        self.text(draw, (x+24, 824), "INPUT TOKENS", 15, MUTED, mono=True)
        self.text(draw, (x+244, 824), "OUTPUT TOKENS", 15, MUTED, mono=True)
        self.text(draw, (x+24, 848), formatted(metrics.get("input_tokens")), 28, INK, bold=True)
        self.text(draw, (x+244, 848), formatted(metrics.get("output_tokens")), 28, INK, bold=True)
        draw.line((x+24, 893, 1856, 893), fill=LINE, width=1)
        cost = number(metrics.get("estimated_cost_usd"))
        unknown_usage = number(metrics.get("unknown_usage_requests"), 0)
        self.text(draw, (x+24, 909), "EST. KNOWN COST" if unknown_usage else "EST. API COST", 15, MUTED, mono=True)
        self.text(draw, (x+238, 904), f"${cost:.5f}" if cost is not None else "—", 25, GREEN, bold=True)

        progress = min(1, max(0, elapsed_ms/self.timeline.duration_ms))
        draw.rounded_rectangle((40, 985, 1400, 991), 3, fill=LINE)
        if progress:
            draw.rounded_rectangle((40, 985, 40+max(3, 1360*progress), 991), 3, fill=GREEN)
        self.text(draw, (40, 1007), f"RECORDED  {timecode(elapsed_ms)} / {timecode(self.timeline.duration_ms)}", 19, MUTED, mono=True)
        observed = []
        if state.get("wave") is not None:
            observed.append(f"Wave {state['wave']}")
        phase = observation.get("phase") or state.get("phase")
        if phase:
            observed.append(str(phase).replace("_", " "))
        if observed:
            self.text(draw, (790, 1007), "Last observation: " + " · ".join(observed), 19, MUTED, max_width=610)
        self.text(draw, (1444, 980), f"{formatted(metrics.get('requests'))} requests  /  {formatted(metrics.get('applied_actions'))} applied", 18, MUTED)
        model = judgment.get("model") or self.timeline.metadata.get("model") or "Model not recorded"
        self.text(draw, (1444, 1011), model, 18, CYAN, mono=True, max_width=436)
        self.text(draw, (40, 1043), "Recorded pixels + timestamped decisions. Selected probabilities describe the last completed request, not a future action.", 15, MUTED)
        if unknown_usage:
            self.text(draw, (1444, 1043), f"Usage unknown for {unknown_usage:g} request(s)", 15, AMBER)
        return canvas

    def title_card(self, outro=False):
        timeline = self.timeline
        canvas = Image.new("RGB", (WIDTH, HEIGHT), BG)
        draw = ImageDraw.Draw(canvas)
        draw.rectangle((40, 64, 48, 1020), fill=GREEN)
        self.text(draw, (88, 72), "JEV × BROTATO", 24, GREEN, mono=True)
        self.text(draw, (90, 183), "OBSERVED RESULT" if outro else "A SYSTEM ONE EXPERIMENT", 19, MUTED, mono=True)
        if outro:
            outcome = timeline.summary.get("outcome") or "Recording complete"
            words = {"gameover": "Game over"}.get(str(outcome), str(outcome).replace("_", " "))
            title = words[0].upper()+words[1:] if words else "Recording complete"
            self.text(draw, (84, 238), title, 60, INK, bold=True, max_width=810)
            wave = timeline.summary.get("wave_reached")
            self.text(draw, (90, 329), f"Highest reported wave: {wave}" if wave is not None else "See the recorded result in the game.", 29, MUTED, max_width=790)
            final = timeline.final_metrics
            self.text(draw, (90, 445), "REQUESTS", 18, MUTED, mono=True)
            self.text(draw, (90, 474), formatted(final.get("requests")), 54, INK, bold=True)
            self.text(draw, (415, 445), "INPUT TOKENS", 18, MUTED, mono=True)
            self.text(draw, (415, 474), formatted(final.get("input_tokens")), 54, INK, bold=True)
            cost = number(final.get("estimated_cost_usd"))
            unknown_usage = number(final.get("unknown_usage_requests"), 0)
            self.text(draw, (90, 589), "ESTIMATED KNOWN API COST" if unknown_usage else "ESTIMATED API COST", 18, MUTED, mono=True)
            self.text(draw, (90, 621), f"${cost:.5f}" if cost is not None else "Not recorded", 54, GREEN, bold=True)
            self.text(draw, (90, 747), f"Recorded duration {timecode(timeline.duration_ms)}", 24, MUTED, max_width=790)
            self.text(draw, (90, 791), self.playback_description(), 24, MUTED, max_width=790)
            if unknown_usage:
                self.text(draw, (90, 838), f"Usage was unavailable for {unknown_usage:g} request(s).", 22, AMBER)
        else:
            self.text(draw, (83, 238), "Jev plays", 104, INK, bold=True)
            self.text(draw, (83, 354), "Brotato.", 104, GREEN, bold=True)
            self.text(draw, (90, 523), "Render test. Synthetic data." if timeline.metadata.get("fixture") else "Real game. Typed decisions.", 33, INK)
            self.text(draw, (90, 588), self.difficulty() + "  /  " + self.playback_description(), 27, MUTED)
            self.text(draw, (90, 693), "OBSERVE → CHOOSE → APPLY", 25, CYAN, mono=True)
            self.text(draw, (90, 748), "Gameplay, probabilities, tokens and latency.", 25, MUTED)
        frame = timeline.frames[-1] if outro else timeline.frames[0]
        self.panel(draw, (928, 222, 1872, 805))
        self.picture(canvas, frame, (945, 244, 910, 512))
        draw = ImageDraw.Draw(canvas)
        caption = "SYNTHETIC TEST FIXTURE — NOT GAMEPLAY" if timeline.metadata.get("fixture") else "FINAL RECORDED FRAME" if outro else "ACTUAL GAME CAPTURE"
        self.text(draw, (951, 774), caption, 16, MUTED, mono=True)
        self.text(draw, (90, 943), "TYPESAFE / SYSTEM ONE", 19, MUTED, mono=True)
        self.text(draw, (951, 943), "COSTS ARE ESTIMATES FROM RECORDED TOKEN USAGE", 16, MUTED, mono=True)
        return canvas


def encode(renderer, output, fps, intro_seconds, outro_seconds, ffmpeg, max_seconds=None):
    duration = renderer.playback.duration_ms/1000
    if max_seconds is not None:
        duration = min(duration, max_seconds)
    count = max(1, math.ceil(duration*fps))
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_name(output.stem + ".rendering.mp4")
    log_path = output.with_suffix(".ffmpeg.log")
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "warning", "-y", "-f", "rawvideo", "-pixel_format", "rgb24",
           "-video_size", f"{WIDTH}x{HEIGHT}", "-framerate", str(fps), "-i", "pipe:0", "-an",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-pix_fmt", "yuv420p", "-threads", "2",
           "-movflags", "+faststart", str(temp)]
    with log_path.open("wb") as log:
        process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=log)
        try:
            for _ in range(round(intro_seconds*fps)):
                if _ == 0:
                    title = renderer.title_card().tobytes()
                process.stdin.write(title)
            for index in range(count):
                elapsed, _, _ = renderer.playback.at(index/fps*1000)
                process.stdin.write(renderer.draw(elapsed).tobytes())
                if index == 0 or index % (fps*5) == 0:
                    print(f"Rendering {index+1}/{count} gameplay frames", flush=True)
            for _ in range(round(outro_seconds*fps)):
                if _ == 0:
                    ending = renderer.title_card(outro=True).tobytes()
                process.stdin.write(ending)
            process.stdin.close()
            if process.wait() != 0:
                raise RuntimeError(f"ffmpeg failed; see {log_path}")
        except BaseException:
            if process.stdin and not process.stdin.closed:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            process.terminate()
            process.wait()
            temp.unlink(missing_ok=True)
            raise
    temp.replace(output)
    return count/fps + round(intro_seconds*fps)/fps + round(outro_seconds*fps)/fps


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path, help="Directory with metadata.json, frames.jsonl and decisions.jsonl")
    parser.add_argument("--output", type=Path, help="Default: SESSION/jev-brotato-timelapse.mp4")
    parser.add_argument("--speed", type=float, default=4, help="Footage speed multiplier (default: 4)")
    parser.add_argument("--menu-speed", type=float, help="Playback speed for shops, upgrades, crates and other menu/result phases")
    parser.add_argument("--start-at", type=float, default=0, help="Start at this recorded elapsed second, before speed changes (default: 0)")
    parser.add_argument("--fps", type=int, default=24)
    parser.add_argument("--intro", type=float, default=3, help="Intro duration in seconds")
    parser.add_argument("--outro", type=float, default=3, help="Outro duration in seconds")
    parser.add_argument("--preview-only", action="store_true", help="Create the dashboard poster PNG without encoding video")
    parser.add_argument("--poster-at", type=float, help="Recorded elapsed seconds for the poster")
    parser.add_argument("--max-video-seconds", type=float, help="Encode only this much gameplay for a render check; title cards are additional")
    args = parser.parse_args()
    if (not math.isfinite(args.speed) or args.speed <= 0 or not 1 <= args.fps <= 60 or
            (args.menu_speed is not None and (not math.isfinite(args.menu_speed) or args.menu_speed <= 0)) or
            not math.isfinite(args.start_at) or args.start_at < 0 or
            any(not math.isfinite(value) or value < 0 for value in (args.intro, args.outro)) or
            (args.poster_at is not None and (not math.isfinite(args.poster_at) or args.poster_at < 0)) or
            (args.max_video_seconds is not None and (not math.isfinite(args.max_video_seconds) or args.max_video_seconds <= 0))):
        parser.error("speed and video limits must be positive; card/poster times nonnegative; fps must be 1–60")
    try:
        timeline = Timeline(args.session.resolve())
        renderer = Renderer(timeline, args.speed, menu_speed=args.menu_speed, start_ms=args.start_at*1000)
        output = args.output.resolve() if args.output else timeline.session / "jev-brotato-timelapse.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        poster_time = (args.poster_at*1000 if args.poster_at is not None else
                       min(timeline.duration_ms, timeline.decision_times[len(timeline.decision_times)//2])
                       if timeline.decisions else timeline.duration_ms/2)
        poster = output.with_suffix(".png")
        renderer.draw(max(args.start_at*1000, min(poster_time, timeline.duration_ms))).save(poster)
        print(f"Poster: {poster}", flush=True)
        if args.preview_only:
            return 0
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            parser.error("ffmpeg was not found on PATH; poster was generated")
        duration = encode(renderer, output, args.fps, args.intro, args.outro, ffmpeg, args.max_video_seconds)
        edit = {"source_start_seconds": args.start_at, "combat_speed": args.speed,
                "menu_speed": args.menu_speed, "intro_seconds": args.intro,
                "outro_seconds": args.outro, "video_seconds": round(duration, 3),
                "segments": [asdict(segment) for segment in renderer.playback.segments]}
        output.with_suffix(".edit.json").write_text(json.dumps(edit, indent=2) + "\n")
        print(json.dumps({"video": str(output), "poster": str(poster), "width": WIDTH, "height": HEIGHT,
                          "fps": args.fps, "playback_speed": args.speed, "video_seconds": round(duration, 3),
                          "menu_speed": args.menu_speed, "source_start_seconds": args.start_at,
                          "recorded_seconds": round(timeline.duration_ms/1000, 3)}, indent=2))
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Render failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
