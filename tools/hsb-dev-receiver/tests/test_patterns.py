# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Microchip Technology Inc. and its subsidiaries.
"""Test patterns: header format, NV12 layout, and bit-exactness with the C++ sender."""

from __future__ import annotations

import hashlib

import numpy as np
import pytest

from hsb_tools.patterns import (
    BAR_COLORS,
    HEADER_SIZE,
    PatternHeader,
    PatternType,
    build_frame,
    check_frame,
    nv12_frame_size,
)

WIDTH = 1280
HEIGHT = 720

# sha256 of `hsb-synthetic-sender --dump FILE --pattern P` (counter 0) from the C++ sender.
CPP_SENDER_SHA256 = {
    PatternType.PRBS: "01ef484b2207837e9a6c26cba1d29aa5ff86b2826dc39ed747fb4ce7309488aa",
    PatternType.GRADIENT: "fa89327d29441f7feead09df7f6f4f74d670ade58e52fd145e9e940bc0c405fb",
    PatternType.BARS: "e536ee75d8a1cbe09b22634dbda5e84d4693ee6a02f78b4242f6910e2e059914",
}


@pytest.mark.parametrize("pattern", list(PatternType))
def test_python_pattern_matches_cpp_sender(pattern: PatternType) -> None:
    frame = build_frame(pattern, 0, WIDTH, HEIGHT)

    assert hashlib.sha256(frame).hexdigest() == CPP_SENDER_SHA256[pattern]


@pytest.mark.parametrize("pattern", list(PatternType))
def test_built_frame_passes_check(pattern: PatternType) -> None:
    frame = build_frame(pattern, 7, WIDTH, HEIGHT)

    result = check_frame(frame, WIDTH, HEIGHT)

    assert result.ok, result.problem
    assert result.header.counter == 7


def test_header_round_trip() -> None:
    header = PatternHeader(
        version=1, pattern=2, counter=0xDEADBEEF, pattern_index=3, pattern_count=4
    )

    assert PatternHeader.parse(header.pack()) == header
    assert len(header.pack()) == HEADER_SIZE


def test_single_flipped_byte_is_located() -> None:
    frame = bytearray(build_frame(PatternType.PRBS, 1, WIDTH, HEIGHT))
    frame[1_000_000] ^= 0x01

    result = check_frame(bytes(frame), WIDTH, HEIGHT)

    assert not result.ok
    assert result.mismatched_bytes == 1
    assert result.first_mismatch_offset == 1_000_000


def test_stale_frame_body_is_detected() -> None:
    # Header of frame 1 on the body of frame 0: what a missed overwrite would look like.
    current = build_frame(PatternType.PRBS, 1, WIDTH, HEIGHT)
    stale = build_frame(PatternType.PRBS, 0, WIDTH, HEIGHT)

    result = check_frame(current[:HEADER_SIZE] + stale[HEADER_SIZE:], WIDTH, HEIGHT)

    assert not result.ok


def test_missing_header_is_reported() -> None:
    result = check_frame(bytes(nv12_frame_size(WIDTH, HEIGHT)), WIDTH, HEIGHT)

    assert result.problem == "missing pattern header"


def test_short_frame_is_reported() -> None:
    frame = build_frame(PatternType.GRADIENT, 0, WIDTH, HEIGHT)

    result = check_frame(frame[:-1], WIDTH, HEIGHT)

    assert not result.ok
    assert "short frame" in result.problem


def test_index_inconsistent_with_counter_is_reported() -> None:
    frame = bytearray(build_frame(PatternType.PRBS, 5, WIDTH, HEIGHT))
    frame[12:14] = (2).to_bytes(2, "big")

    assert (
        check_frame(bytes(frame), WIDTH, HEIGHT).problem
        == "pattern index does not match counter"
    )


def test_bars_nv12_layout() -> None:
    frame = np.frombuffer(
        build_frame(PatternType.BARS, 0, WIDTH, HEIGHT), dtype=np.uint8
    )
    luma = frame[: WIDTH * HEIGHT].reshape(HEIGHT, WIDTH)
    chroma = frame[WIDTH * HEIGHT :].reshape(HEIGHT // 2, WIDTH)
    bar_width = WIDTH // len(BAR_COLORS)

    for bar, (y, u, v) in enumerate(BAR_COLORS):
        column = bar * bar_width + bar_width // 2
        assert luma[HEIGHT // 2, column] == y
        assert chroma[HEIGHT // 4, column & ~1] == u
        assert chroma[HEIGHT // 4, (column & ~1) + 1] == v
