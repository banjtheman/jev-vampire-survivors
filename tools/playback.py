"""Piecewise playback timing for timestamped gameplay and menu observations.

All public times are milliseconds. Source duration is the recording's ending
timestamp, while ``PlaybackPlan.duration_ms`` is the trimmed video's duration.
An observation changes the phase only at its recorded ``elapsed_ms``; no phase
or decision is moved earlier to accommodate a playback-speed change.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
import math
from numbers import Real


MENU_PHASES = frozenset({"menu", "shop", "upgrade", "crate", "paused", "gameover", "victory"})


def _finite(value, name):
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


@dataclass(frozen=True)
class PlaybackSegment:
    """A source interval and its corresponding output interval.

    Intervals are half-open, except that lookup at the final endpoint clamps to
    the end of the last segment. ``speed`` is source time divided by output time.
    """

    start_ms: float
    end_ms: float
    speed: float
    output_start_ms: float
    output_end_ms: float
    phase: str


class PlaybackPlan:
    """Map video time to recorded time, slowing only observed menu phases.

    ``records`` is an iterable of dictionaries from a decision/event log. A phase
    comes from ``record.phase`` or ``record.state.phase``. Records without a phase
    do not change it. Same-time observations use the last record in input order.
    Before the first phase observation, ``unknown`` uses the base ``speed``.

    ``menu_speed=None`` preserves constant-speed playback. ``start_ms`` trims the
    recording without forgetting a phase observed before that starting point.
    Records at/after ``duration_ms`` cannot add footage past the recording's end.
    """

    def __init__(self, records, duration_ms, speed, menu_speed=None, start_ms=0):
        end = _finite(duration_ms, "duration_ms")
        start = _finite(start_ms, "start_ms")
        base_speed = _finite(speed, "speed")
        slow_speed = base_speed if menu_speed is None else _finite(menu_speed, "menu_speed")
        if end <= 0 or start < 0 or start >= end:
            raise ValueError("Require 0 <= start_ms < duration_ms")
        if base_speed <= 0 or slow_speed <= 0:
            raise ValueError("speed and menu_speed must be positive")
        self.source_start_ms = start
        self.source_end_ms = end
        self.speed = base_speed
        self.menu_speed = slow_speed

        events = {}
        for record in records:
            if not isinstance(record, dict):
                raise ValueError("Playback records must be dictionaries")
            state = record.get("state")
            phase = record.get("phase") or (state.get("phase") if isinstance(state, dict) else None)
            if phase is None or phase == "":
                continue
            if not isinstance(phase, str):
                raise ValueError("Observed phase must be a string")
            phase = phase.strip().lower()
            if not phase:
                continue
            timestamp = _finite(record.get("elapsed_ms"), "record.elapsed_ms")
            if timestamp < 0:
                raise ValueError("record.elapsed_ms cannot be negative")
            if timestamp < end:
                events[timestamp] = phase

        self.segments: list[PlaybackSegment] = []
        self.duration_ms = 0.0
        cursor = 0.0
        phase = "unknown"
        for timestamp, next_phase in sorted(events.items()):
            if next_phase == phase:
                continue
            self._append(max(cursor, start), timestamp, phase)
            cursor, phase = timestamp, next_phase
        self._append(max(cursor, start), end, phase)
        self._output_starts = [segment.output_start_ms for segment in self.segments]
        self._source_starts = [segment.start_ms for segment in self.segments]

    def _append(self, start, end, phase):
        if end <= start:
            return
        speed = self.menu_speed if phase in MENU_PHASES else self.speed
        output_start = self.duration_ms
        output_end = output_start + (end-start)/speed
        if not math.isfinite(output_end) or output_end <= output_start:
            raise ValueError("Playback interval cannot be represented at this speed")
        if self.segments and self.segments[-1].phase == phase and self.segments[-1].speed == speed:
            previous = self.segments.pop()
            segment = PlaybackSegment(previous.start_ms, end, speed, previous.output_start_ms, output_end, phase)
        else:
            segment = PlaybackSegment(start, end, speed, output_start, output_end, phase)
        self.segments.append(segment)
        self.duration_ms = output_end

    def at(self, video_elapsed_ms):
        """Return ``(source_ms, speed, phase)``; clamp outside the video's bounds."""
        timestamp = min(self.duration_ms, max(0.0, _finite(video_elapsed_ms, "video_elapsed_ms")))
        if timestamp >= self.duration_ms:
            segment = self.segments[-1]
            return self.source_end_ms, segment.speed, segment.phase
        index = max(0, bisect_right(self._output_starts, timestamp)-1)
        segment = self.segments[index]
        source = segment.start_ms + (timestamp-segment.output_start_ms)*segment.speed
        source = min(segment.end_ms, max(segment.start_ms, source))
        return source, segment.speed, segment.phase

    def speed_at(self, source_ms):
        """Return speed at a recorded timestamp, clamped to the trimmed interval."""
        timestamp = min(self.source_end_ms, max(self.source_start_ms, _finite(source_ms, "source_ms")))
        index = max(0, bisect_right(self._source_starts, timestamp)-1)
        return self.segments[index].speed
