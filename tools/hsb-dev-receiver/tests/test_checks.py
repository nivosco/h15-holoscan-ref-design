# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Microchip Technology Inc. and its subsidiaries.
"""Stream-level continuity and timing checks."""

from __future__ import annotations

from dataclasses import dataclass, field

from hsb_tools.checks import StreamChecker
from hsb_tools.patterns import PatternType, build_frame
from hsb_tools.protocol import FrameMetadata

WIDTH, HEIGHT = 64, 32
PERIOD_NS = 16_666_667


@dataclass
class FakeFrame:
    data: bytes
    metadata: FrameMetadata
    packets: int = 3
    psn_gaps: int = 0
    issues: list[str] = field(default_factory=list)

    @property
    def bytes_received(self) -> int:
        return self.metadata.bytes_written


def frame(
    number: int, counter: int | None = None, timestamp_ns: int | None = None
) -> FakeFrame:
    timestamp = PERIOD_NS * number if timestamp_ns is None else timestamp_ns
    data = build_frame(
        PatternType.GRADIENT, number if counter is None else counter, WIDTH, HEIGHT
    )
    metadata = FrameMetadata(0, 0, 0, timestamp, len(data), number, timestamp + 1000)
    return FakeFrame(data, metadata)


def test_in_order_frames_pass_and_report_period() -> None:
    checker = StreamChecker(WIDTH, HEIGHT)

    for number in range(10):
        assert checker.add(frame(number))

    summary = checker.report.summary()
    assert checker.report.passed
    assert abs(summary["frame_period_ms_mean"] - PERIOD_NS / 1e6) < 1e-3


def test_constant_sender_counter_offset_is_accepted() -> None:
    # The sender counts frames dropped before the host configured the stream.
    checker = StreamChecker(WIDTH, HEIGHT)

    for number in range(5):
        checker.add(frame(number, counter=number + 120))

    assert checker.report.passed


def test_frame_number_gap_counts_lost_frames() -> None:
    checker = StreamChecker(WIDTH, HEIGHT)

    for number in (0, 1, 4, 5):
        checker.add(frame(number))

    assert checker.report.frames_lost == 2
    assert not checker.report.passed


def test_counter_jump_without_frame_number_jump_is_reported() -> None:
    checker = StreamChecker(WIDTH, HEIGHT)

    checker.add(frame(0, counter=0))
    checker.add(frame(1, counter=2))

    assert checker.report.counter_offset_changes == 1


def test_timestamp_regression_is_reported() -> None:
    checker = StreamChecker(WIDTH, HEIGHT)

    checker.add(frame(0, timestamp_ns=2 * PERIOD_NS))
    checker.add(frame(1, timestamp_ns=PERIOD_NS))

    assert checker.report.timestamp_regressions == 1


def test_frame_number_wraps_at_32_bits() -> None:
    checker = StreamChecker(WIDTH, HEIGHT)

    checker.add(frame(0xFFFFFFFF, counter=3, timestamp_ns=PERIOD_NS))
    checker.add(frame(0, counter=4, timestamp_ns=2 * PERIOD_NS))

    assert checker.report.frames_lost == 0


def test_loss_tolerant_pass_ignores_incomplete_frames() -> None:
    checker = StreamChecker(WIDTH, HEIGHT)
    checker.add(frame(0))
    lossy = frame(1)
    lossy.issues.append("psn 9: expected 8")
    checker.add(lossy)
    checker.add(frame(2))

    assert not checker.report.passed
    assert checker.report.passed_allowing_loss
    assert checker.report.frames_complete == 2


def test_loss_tolerant_pass_still_fails_on_corrupted_complete_frame() -> None:
    checker = StreamChecker(WIDTH, HEIGHT)
    corrupted = frame(0)
    data = bytearray(corrupted.data)
    data[-1] ^= 0xFF
    corrupted.data = bytes(data)
    checker.add(corrupted)

    assert checker.report.complete_frame_errors == 1
    assert not checker.report.passed_allowing_loss
