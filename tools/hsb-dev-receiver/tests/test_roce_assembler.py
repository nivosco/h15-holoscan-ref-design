# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Microchip Technology Inc. and its subsidiaries.
"""RoCEv2 reassembly against a model of the emulator's segmentation, with fault injection."""

from __future__ import annotations

import struct

import pytest
from emulator_model import RoceEmulatorModel

from hsb_tools.assembler import FrameAssembler, StreamExpectations
from hsb_tools.checks import StreamChecker
from hsb_tools.patterns import PatternType, build_frame
from hsb_tools.protocol import RocePacket, roundup

PAYLOAD_SIZE = 1408


def make_assembler(frame_size: int, payload_size: int = PAYLOAD_SIZE) -> FrameAssembler:
    return FrameAssembler(
        StreamExpectations(
            frame_size=frame_size,
            payload_size=payload_size,
            qp=0xCAF0,
            rkey=0xBEEF,
            page_increment=roundup(frame_size, 128) + 128,
        )
    )


def run(packets: list[bytes], width: int, height: int) -> StreamChecker:
    assembler = make_assembler(width * height * 3 // 2)
    checker = StreamChecker(width, height)
    for raw in packets:
        packet = RocePacket.parse(memoryview(raw))
        assert packet is not None
        frame = assembler.handle(packet)
        if frame is not None:
            checker.add(frame)
    return checker


def test_clean_stream_passes(prbs_frames, small_size) -> None:
    packets = RoceEmulatorModel().stream(prbs_frames)

    checker = run(packets, *small_size)

    assert checker.report.frames == len(prbs_frames)
    assert checker.report.passed, checker.report.issues


@pytest.mark.parametrize("payload_size", [1408, 512, 4096])
def test_embedded_and_separate_metadata_layouts(payload_size: int) -> None:
    # 720p with 1408-byte packets sends metadata separately; larger packets embed it.
    width, height = 1280, 720
    frames = [
        build_frame(PatternType.PRBS, counter, width, height) for counter in range(2)
    ]
    packets = RoceEmulatorModel(payload_size=payload_size).stream(frames)
    assembler = make_assembler(width * height * 3 // 2, payload_size)
    checker = StreamChecker(width, height)

    for raw in packets:
        frame = assembler.handle(RocePacket.parse(memoryview(raw)))
        if frame is not None:
            checker.add(frame)

    assert checker.report.frames == 2
    assert checker.report.passed, checker.report.issues


def test_dropped_packet_is_detected(prbs_frames, small_size) -> None:
    packets = RoceEmulatorModel().stream(prbs_frames)
    del packets[5]

    report = run(packets, *small_size).report

    assert not report.passed
    assert report.psn_gaps == 1
    assert report.frames_incomplete == 1


def test_dropped_frame_end_is_counted_as_lost_frame(prbs_frames, small_size) -> None:
    packets = RoceEmulatorModel().stream(prbs_frames)
    frame_ends = [i for i, raw in enumerate(packets) if raw[0] == 0x2B]
    del packets[frame_ends[1]]

    report = run(packets, *small_size).report

    assert report.frames_lost == 1


def test_reordered_packets_are_detected(prbs_frames, small_size) -> None:
    packets = RoceEmulatorModel().stream(prbs_frames)
    packets[4], packets[5] = packets[5], packets[4]

    report = run(packets, *small_size).report

    assert report.frames_with_protocol_issues >= 1
    assert any("offset" in issue or "psn" in issue for issue in report.issues)


def test_corrupted_payload_fails_content_check(prbs_frames, small_size) -> None:
    packets = RoceEmulatorModel().stream(prbs_frames)
    corrupted = bytearray(packets[1])
    corrupted[100] ^= 0xFF
    packets[1] = bytes(corrupted)

    report = run(packets, *small_size).report

    assert report.pattern_failures == 1


def test_wrong_virtual_address_is_detected(prbs_frames, small_size) -> None:
    packets = RoceEmulatorModel().stream(prbs_frames)
    moved = bytearray(packets[1])
    struct.pack_into(">Q", moved, 12, 64)  # RETH virtual address
    packets[1] = bytes(moved)

    report = run(packets, *small_size).report

    assert not report.passed


def test_wrong_qp_and_rkey_are_reported(prbs_frames, small_size) -> None:
    model = RoceEmulatorModel(qp=0x1234, rkey=0x5678)

    report = run(model.stream(prbs_frames), *small_size).report

    assert any("qp" in issue for issue in report.issues)


def test_stream_joined_mid_frame_skips_partial_frame(prbs_frames, small_size) -> None:
    packets = RoceEmulatorModel().stream(prbs_frames)

    report = run(packets[1:], *small_size).report

    assert report.frames == len(prbs_frames) - 1
    assert report.passed, report.issues


def test_psn_wraps_at_20_bits(prbs_frames, small_size) -> None:
    model = RoceEmulatorModel(psn=(1 << 20) - 2)

    report = run(model.stream(prbs_frames), *small_size).report

    assert report.psn_gaps == 0
    assert report.passed, report.issues


def test_non_rdma_payload_is_ignored() -> None:
    assert RocePacket.parse(memoryview(bytes(64))) is None
