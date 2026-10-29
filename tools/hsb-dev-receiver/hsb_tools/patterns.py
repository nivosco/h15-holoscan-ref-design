# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Microchip Technology Inc. and its subsidiaries.
"""Deterministic NV12 test patterns, bit-exact with h15/tools/hsb_synthetic_sender."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum
from functools import lru_cache

import numpy as np

HEADER = struct.Struct(">IBBHIHH")  # magic version pattern reserved counter index count
HEADER_SIZE = HEADER.size
HEADER_MAGIC = 0x48534254  # "HSBT"
HEADER_VERSION = 1

GRADIENT_STEP_PIXELS = 8
UV_NEUTRAL = 128
SPLITMIX_GOLDEN = 0x9E3779B97F4A7C15
SPLITMIX_MULTIPLIER_1 = 0xBF58476D1CE4E5B9
SPLITMIX_MULTIPLIER_2 = 0x94D049BB133111EB
UINT64_MASK = (1 << 64) - 1

# BT.601 limited-range 100% bars: white, yellow, cyan, green, magenta, red, blue, black.
BAR_COLORS = np.array(
    [
        (235, 128, 128),
        (210, 16, 146),
        (170, 166, 16),
        (145, 54, 34),
        (106, 202, 222),
        (81, 90, 240),
        (41, 240, 110),
        (16, 128, 128),
    ],
    dtype=np.uint8,
)
BAR_NAMES = ("white", "yellow", "cyan", "green", "magenta", "red", "blue", "black")


class PatternType(IntEnum):
    """Pattern codes carried in the frame header."""

    GRADIENT = 1
    PRBS = 2
    BARS = 3


@dataclass(frozen=True)
class PatternHeader:
    """The 16-byte header at the start of every synthetic frame."""

    version: int
    pattern: int
    counter: int
    pattern_index: int
    pattern_count: int

    @classmethod
    def parse(cls, frame: bytes | memoryview) -> PatternHeader | None:
        """Parses the header.

        Args:
            frame: Frame bytes.

        Returns:
            The header, or None if the magic is missing.
        """
        if len(frame) < HEADER_SIZE:
            return None
        magic, version, pattern, _, counter, index, count = HEADER.unpack_from(frame)
        if magic != HEADER_MAGIC:
            return None
        return cls(version, pattern, counter, index, count)

    def pack(self) -> bytes:
        """Serializes the header.

        Returns:
            16 bytes.
        """
        return HEADER.pack(
            HEADER_MAGIC,
            self.version,
            self.pattern,
            0,
            self.counter,
            self.pattern_index,
            self.pattern_count,
        )


@dataclass(frozen=True)
class PatternCheck:
    """Result of comparing a received frame with its expected pattern."""

    header: PatternHeader | None
    mismatched_bytes: int
    first_mismatch_offset: int | None
    problem: str | None

    @property
    def ok(self) -> bool:
        """True when the frame matched exactly."""
        return self.problem is None


def nv12_frame_size(width: int, height: int) -> int:
    """Returns the NV12 frame size in bytes.

    Args:
        width: Frame width in pixels.
        height: Frame height in pixels.

    Returns:
        width * height * 3 / 2.
    """
    return width * height * 3 // 2


def _splitmix64(values: np.ndarray) -> np.ndarray:
    with np.errstate(over="ignore"):
        values = values ^ (values >> np.uint64(30))
        values = values * np.uint64(SPLITMIX_MULTIPLIER_1)
        values = values ^ (values >> np.uint64(27))
        values = values * np.uint64(SPLITMIX_MULTIPLIER_2)
        values = values ^ (values >> np.uint64(31))
    return values


def _gradient(width: int, height: int, pattern_index: int) -> np.ndarray:
    shift = pattern_index * GRADIENT_STEP_PIXELS
    rows = np.arange(height, dtype=np.uint32)[:, None]
    columns = np.arange(width, dtype=np.uint32)[None, :]
    luma = ((rows + columns + shift) & 0xFF).astype(np.uint8).ravel()
    chroma = np.full(width * height // 2, UV_NEUTRAL, dtype=np.uint8)
    return np.concatenate([luma, chroma])


def _prbs(frame_size: int, pattern_index: int) -> np.ndarray:
    seed = ((pattern_index + 1) * SPLITMIX_GOLDEN) & UINT64_MASK
    word_count = (frame_size + 7) // 8
    with np.errstate(over="ignore"):
        words = np.arange(word_count, dtype=np.uint64) + np.uint64(seed)
    return _splitmix64(words).astype("<u8").view(np.uint8)[:frame_size].copy()


def _bars(width: int, height: int) -> np.ndarray:
    bar_count = len(BAR_COLORS)
    luma_bars = np.arange(width) * bar_count // width
    luma = np.tile(BAR_COLORS[luma_bars, 0], height)
    chroma_bars = (2 * np.arange(width // 2)) * bar_count // width
    chroma_row = np.empty(width, dtype=np.uint8)
    chroma_row[0::2] = BAR_COLORS[chroma_bars, 1]
    chroma_row[1::2] = BAR_COLORS[chroma_bars, 2]
    return np.concatenate([luma, np.tile(chroma_row, height // 2)])


def pattern_count(pattern: PatternType) -> int:
    """Returns how many distinct frames the sender cycles through for a pattern.

    Args:
        pattern: Pattern type.

    Returns:
        1 for bars, 4 otherwise.
    """
    return 1 if pattern == PatternType.BARS else 4


@lru_cache(maxsize=16)
def expected_frame(
    pattern: int, pattern_index: int, width: int, height: int
) -> np.ndarray:
    """Generates the frame the sender emits, without its header.

    Args:
        pattern: PatternType value.
        pattern_index: Index within the pattern cycle.
        width: Frame width.
        height: Frame height.

    Returns:
        Read-only uint8 array of nv12_frame_size bytes; the first HEADER_SIZE bytes hold
        pattern data that the sender overwrites with the header.

    Raises:
        ValueError: Unknown pattern.
    """
    if pattern == PatternType.GRADIENT:
        frame = _gradient(width, height, pattern_index)
    elif pattern == PatternType.PRBS:
        frame = _prbs(nv12_frame_size(width, height), pattern_index)
    elif pattern == PatternType.BARS:
        frame = _bars(width, height)
    else:
        raise ValueError(f"unknown pattern {pattern}")
    frame.flags.writeable = False
    return frame


def build_frame(pattern: PatternType, counter: int, width: int, height: int) -> bytes:
    """Builds a complete frame exactly as the sender does (used by tests).

    Args:
        pattern: Pattern type.
        counter: Sender frame counter.
        width: Frame width.
        height: Frame height.

    Returns:
        Frame bytes including the header.
    """
    count = pattern_count(pattern)
    index = counter % count
    body = expected_frame(int(pattern), index, width, height)
    header = PatternHeader(
        HEADER_VERSION, int(pattern), counter & 0xFFFFFFFF, index, count
    )
    return header.pack() + body[HEADER_SIZE:].tobytes()


def check_frame(frame: bytes | memoryview, width: int, height: int) -> PatternCheck:
    """Compares a received frame with the pattern its header announces.

    Args:
        frame: Received frame bytes.
        width: Frame width.
        height: Frame height.

    Returns:
        The comparison result.
    """
    header = PatternHeader.parse(frame)
    if header is None:
        return PatternCheck(None, 0, None, "missing pattern header")
    if header.version != HEADER_VERSION:
        return PatternCheck(
            header, 0, None, f"unsupported header version {header.version}"
        )
    if header.pattern_count == 0 or header.pattern_index >= header.pattern_count:
        return PatternCheck(header, 0, None, "pattern index out of range")
    if header.counter % header.pattern_count != header.pattern_index:
        return PatternCheck(header, 0, None, "pattern index does not match counter")
    try:
        expected = expected_frame(header.pattern, header.pattern_index, width, height)
    except ValueError as error:
        return PatternCheck(header, 0, None, str(error))
    if len(frame) < len(expected):
        return PatternCheck(
            header, 0, None, f"short frame: {len(frame)} < {len(expected)} bytes"
        )
    received = np.frombuffer(frame, dtype=np.uint8, count=len(expected))
    differences = np.flatnonzero(received[HEADER_SIZE:] != expected[HEADER_SIZE:])
    if differences.size == 0:
        return PatternCheck(header, 0, None, None)
    first = int(differences[0]) + HEADER_SIZE
    return PatternCheck(
        header, int(differences.size), first, f"{differences.size} bytes differ"
    )
