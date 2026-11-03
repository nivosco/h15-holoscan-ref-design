# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Microchip Technology Inc. and its subsidiaries.
"""Decoding of the detection metadata published by the H15 sender."""

from __future__ import annotations

from pathlib import Path

import pytest
from metadata_encoder import _bytes, _uint, encode_frame

from hsb_tools.detections import (
    Bbox,
    Detection,
    DetectionFrame,
    MetadataDecodeError,
    decode_frame,
)

DATA_DIR = Path(__file__).resolve().parent / "data"


def test_decodes_a_message_captured_from_the_board() -> None:
    message = (DATA_DIR / "frame_person_tracked.bin").read_bytes()

    frame = decode_frame(message)

    assert frame.isp_timestamp_ns == 285_231_877_000
    assert (frame.frame_width, frame.frame_height) == (1920, 1080)
    assert len(frame.detections) == 1
    person = frame.detections[0]
    assert person.label == "person"
    assert person.tracking_id == 5
    assert person.confidence == pytest.approx(0.432, abs=1e-3)
    assert person.bbox.xmin == pytest.approx(712.07, abs=0.01)
    assert person.bbox.ymax == pytest.approx(1079.07, abs=0.01)


def test_round_trips_nested_detections_and_missing_track() -> None:
    face = Detection("face", 0.5, Bbox(10, 10, 20, 20), tracking_id=None)
    person = Detection("person", 0.75, Bbox(0, 0, 100, 200), 3, detections=(face,))
    expected = DetectionFrame(123, 1920, 1080, detections=(person,))

    decoded = decode_frame(encode_frame(expected))

    assert decoded == expected


def test_skips_unknown_and_landmark_fields() -> None:
    landmarks = _bytes(5, b"\x0a\x05x,y,c")
    unknown_field = _uint(99, 7)
    message = _uint(1, 42) + landmarks + unknown_field + _uint(2, 640)

    frame = decode_frame(message)

    assert (frame.isp_timestamp_ns, frame.frame_width, frame.detections) == (
        42,
        640,
        (),
    )


@pytest.mark.parametrize(
    "message",
    [
        b"\x08\xff",  # truncated varint
        b"\x22\x10\x0a",  # detection longer than the message
        b"\x0b",  # group wire type
        b"\x22\x02\x15\x00",  # confidence with a truncated float
    ],
)
def test_rejects_malformed_messages(message: bytes) -> None:
    with pytest.raises(MetadataDecodeError):
        decode_frame(message)
