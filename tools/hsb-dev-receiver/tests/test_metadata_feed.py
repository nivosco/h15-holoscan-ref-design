# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Microchip Technology Inc. and its subsidiaries.
"""Validation of detection metadata and its join to HSB frames."""

from __future__ import annotations

import pytest
from metadata_encoder import encode_frame

from hsb_tools.detections import Bbox, Detection, DetectionFrame
from hsb_tools.metadata_feed import MetadataJoiner

PERIOD_NS = 16_666_000
WIDTH, HEIGHT = 1920, 1080
PERSON_BOX = Bbox(100, 100, 400, 800)


def timestamp(frame_number: int) -> int:
    return 1_000_000_000 + frame_number * PERIOD_NS


def message(
    timestamp_ns: int,
    box: Bbox = PERSON_BOX,
    confidence: float = 0.8,
    tracking_id: int | None = 1,
    size: tuple[int, int] = (WIDTH, HEIGHT),
) -> bytes:
    detection = Detection("person", confidence, box, tracking_id)
    return encode_frame(DetectionFrame(timestamp_ns, *size, detections=(detection,)))


@pytest.fixture
def joiner() -> MetadataJoiner:
    return MetadataJoiner()


def test_joins_metadata_to_its_frame_in_either_arrival_order(
    joiner: MetadataJoiner,
) -> None:
    joiner.add_frame(timestamp(0), 0)
    joiner.add_message(message(timestamp(0)))
    joiner.add_message(message(timestamp(1)))
    joiner.add_frame(timestamp(1), 1)
    joiner.finish()

    report = joiner.report
    assert (report.joined, report.frame_not_received) == (2, 0)
    assert report.passed
    assert joiner.detections_at(timestamp(1)).detections[0].label == "person"
    assert joiner.detections_at(timestamp(2)) is None


def test_metadata_of_a_lost_frame_is_tolerated(joiner: MetadataJoiner) -> None:
    joiner.add_frame(timestamp(0), 0)
    joiner.add_message(message(timestamp(1)))
    joiner.add_frame(timestamp(2), 2)
    joiner.finish()

    assert joiner.report.frame_not_received == 1
    assert joiner.report.passed


def test_metadata_between_consecutive_frames_fails(joiner: MetadataJoiner) -> None:
    joiner.add_frame(timestamp(0), 0)
    joiner.add_frame(timestamp(1), 1)
    joiner.add_message(message(timestamp(0) + PERIOD_NS // 2))
    joiner.finish()

    assert joiner.report.between_consecutive_frames == 1
    assert not joiner.report.passed


@pytest.mark.parametrize(
    ("kwargs", "counter"),
    [
        ({"box": Bbox(100, 100, 2000, 800)}, "bbox_out_of_bounds"),
        ({"box": Bbox(400, 100, 100, 800)}, "bbox_out_of_bounds"),
        ({"confidence": 1.5}, "confidence_out_of_range"),
    ],
)
def test_rejects_inconsistent_detections(
    joiner: MetadataJoiner, kwargs: dict, counter: str
) -> None:
    joiner.add_message(message(timestamp(0), **kwargs))

    assert getattr(joiner.report, counter) == 1
    assert not joiner.report.passed


def test_rejects_timestamps_that_repeat_or_go_back(joiner: MetadataJoiner) -> None:
    joiner.add_message(message(timestamp(5)))
    joiner.add_message(message(timestamp(5)))
    joiner.add_message(message(timestamp(4)))

    assert joiner.report.duplicate_timestamps == 1
    assert joiner.report.timestamp_regressions == 1
    assert not joiner.report.passed


def test_counts_tracks_and_size_changes(joiner: MetadataJoiner) -> None:
    joiner.add_message(message(timestamp(0), tracking_id=7))
    joiner.add_message(message(timestamp(1), tracking_id=None))
    joiner.add_message(message(timestamp(2), tracking_id=8, size=(1280, 720)))

    report = joiner.report
    assert report.track_ids == {7, 8}
    assert report.detections_without_track == 1
    assert report.frame_size_changes == 1


def test_counts_undecodable_messages(joiner: MetadataJoiner) -> None:
    joiner.add_message(b"\x08\xff")

    assert joiner.report.decode_errors == 1
    assert not joiner.report.passed
