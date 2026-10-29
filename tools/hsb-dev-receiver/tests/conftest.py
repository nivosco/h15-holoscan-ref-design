# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Microchip Technology Inc. and its subsidiaries.
"""Shared fixtures; small frames keep the suite fast while exercising every code path."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from hsb_tools.patterns import PatternType, build_frame  # noqa: E402

SMALL_WIDTH = 64
SMALL_HEIGHT = 32  # 3072-byte NV12 frames: three 1408-byte packets


@pytest.fixture
def small_size() -> tuple[int, int]:
    """Width and height used by stream-level tests."""
    return SMALL_WIDTH, SMALL_HEIGHT


@pytest.fixture
def prbs_frames() -> list[bytes]:
    """Five consecutive PRBS frames as the sender would emit them."""
    return [
        build_frame(PatternType.PRBS, counter, SMALL_WIDTH, SMALL_HEIGHT)
        for counter in range(5)
    ]
