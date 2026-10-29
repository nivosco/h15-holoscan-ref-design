# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Microchip Technology Inc. and its subsidiaries.
"""Stream-level checks across frames: continuity, timing and content."""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Protocol

from .patterns import check_frame
from .protocol import FrameMetadata

MAX_RECORDED_ISSUES = 50
NANOSECONDS_PER_MILLISECOND = 1e6


class ReceivedFrame(Protocol):
    """What the stream checker needs from an assembled RoCE or COE frame."""

    data: bytes
    metadata: FrameMetadata
    packets: int
    bytes_received: int
    psn_gaps: int
    issues: list[str]


@dataclass
class StreamReport:
    """Accumulated verification results."""

    frames: int = 0
    frames_complete: int = 0
    complete_frame_errors: int = 0
    frames_content_ok: int = 0
    frames_with_protocol_issues: int = 0
    frames_incomplete: int = 0
    frames_lost: int = 0
    pattern_failures: int = 0
    counter_offset_changes: int = 0
    timestamp_regressions: int = 0
    metadata_before_frame_start: int = 0
    psn_gaps: int = 0
    packets: int = 0
    frame_periods_ms: list[float] = field(default_factory=list)
    packets_per_frame: set[int] = field(default_factory=set)
    issues: list[str] = field(default_factory=list)

    def record(self, text: str) -> None:
        """Keeps the first issues for the final report.

        Args:
            text: Issue description.
        """
        if len(self.issues) < MAX_RECORDED_ISSUES:
            self.issues.append(text)

    def summary(self) -> dict[str, object]:
        """Returns the report as JSON-friendly values.

        Returns:
            Summary dictionary.
        """
        periods = self.frame_periods_ms
        return {
            "frames": self.frames,
            "frames_complete": self.frames_complete,
            "complete_frame_errors": self.complete_frame_errors,
            "frames_content_ok": self.frames_content_ok,
            "frames_incomplete": self.frames_incomplete,
            "frames_with_protocol_issues": self.frames_with_protocol_issues,
            "frames_lost": self.frames_lost,
            "pattern_failures": self.pattern_failures,
            "counter_offset_changes": self.counter_offset_changes,
            "timestamp_regressions": self.timestamp_regressions,
            "metadata_before_frame_start": self.metadata_before_frame_start,
            "psn_gaps": self.psn_gaps,
            "packets": self.packets,
            "packets_per_frame": sorted(self.packets_per_frame),
            "frame_period_ms_mean": round(statistics.fmean(periods), 3)
            if periods
            else None,
            "frame_period_ms_stdev": round(statistics.pstdev(periods), 3)
            if periods
            else None,
            "frame_period_ms_max": round(max(periods), 3) if periods else None,
            "issues": self.issues,
        }

    @property
    def passed(self) -> bool:
        """True when every received frame was intact and nothing was lost."""
        return (
            self.frames > 0
            and self.frames_content_ok == self.frames
            and self.frames_lost == 0
            and self.counter_offset_changes == 0
            and self.timestamp_regressions == 0
        )

    @property
    def passed_allowing_loss(self) -> bool:
        """True when every frame that arrived complete was correct.

        For lossy links (e.g. a laptop standing in for the Jetson): packet loss is
        reported but tolerated, while corruption, misplacement, out-of-step counters
        and timestamp regressions still fail.
        """
        return (
            self.frames_complete > 0
            and self.complete_frame_errors == 0
            and self.timestamp_regressions == 0
            and self.metadata_before_frame_start == 0
        )


class StreamChecker:
    """Validates a sequence of frames against the synthetic sender's contract."""

    def __init__(self, width: int, height: int, verify_pattern: bool = True) -> None:
        """Creates a checker.

        Args:
            width: Frame width.
            height: Frame height.
            verify_pattern: Compare frame content with the expected test pattern.
        """
        self.width = width
        self.height = height
        self.verify_pattern = verify_pattern
        self.report = StreamReport()
        self._last_frame_number: int | None = None
        self._last_timestamp_ns: int | None = None
        self._counter_offset: int | None = None

    def add(self, frame: ReceivedFrame) -> bool:
        """Checks one frame.

        Args:
            frame: Assembled frame.

        Returns:
            True when this frame passed every check.
        """
        report = self.report
        metadata = frame.metadata
        number = metadata.frame_number
        report.frames += 1
        report.packets += frame.packets
        report.psn_gaps += frame.psn_gaps
        report.packets_per_frame.add(frame.packets)
        ok = True

        if self._last_frame_number is not None:
            gap = (number - self._last_frame_number - 1) & 0xFFFFFFFF
            if gap:
                report.frames_lost += gap
                report.record(f"frame {number}: {gap} frame(s) missing before it")
        self._last_frame_number = number

        complete = frame.bytes_received == metadata.bytes_written and not frame.issues
        if frame.bytes_received != metadata.bytes_written:
            report.frames_incomplete += 1
            ok = False
        if frame.issues:
            report.frames_with_protocol_issues += 1
            report.record(f"frame {number}: {'; '.join(frame.issues[:3])}")
            ok = False
        if complete:
            report.frames_complete += 1

        ok &= self._check_timing(number, metadata)
        if self.verify_pattern:
            content_ok = self._check_content(number, frame.data, complete)
            if complete and not content_ok:
                report.complete_frame_errors += 1
            ok &= content_ok
        if ok:
            report.frames_content_ok += 1
        return ok

    def _check_timing(self, number: int, metadata: FrameMetadata) -> bool:
        report = self.report
        ok = True
        if metadata.metadata_timestamp_ns < metadata.timestamp_ns:
            report.metadata_before_frame_start += 1
            report.record(f"frame {number}: metadata timestamp precedes frame start")
            ok = False
        if self._last_timestamp_ns is not None:
            period_ns = metadata.timestamp_ns - self._last_timestamp_ns
            if period_ns <= 0:
                report.timestamp_regressions += 1
                report.record(f"frame {number}: timestamp went back {-period_ns} ns")
                ok = False
            else:
                report.frame_periods_ms.append(period_ns / NANOSECONDS_PER_MILLISECOND)
        self._last_timestamp_ns = metadata.timestamp_ns
        return ok

    def _check_content(self, number: int, data: bytes, complete: bool) -> bool:
        report = self.report
        result = check_frame(data, self.width, self.height)
        if not result.ok:
            report.pattern_failures += 1
            where = (
                f" at byte {result.first_mismatch_offset}"
                if result.first_mismatch_offset
                else ""
            )
            report.record(f"frame {number}: content {result.problem}{where}")
            return False
        if not complete:
            # A header that survived packet loss may belong to another frame; only
            # complete frames are trusted for the counter relationship.
            return True
        offset = (result.header.counter - number) & 0xFFFFFFFF
        if self._counter_offset is not None and offset != self._counter_offset:
            report.counter_offset_changes += 1
            report.record(
                f"frame {number}: sender counter {result.header.counter} out of step"
            )
            self._counter_offset = offset
            return False
        self._counter_offset = offset
        return True
