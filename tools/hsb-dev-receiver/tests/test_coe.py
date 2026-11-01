# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Microchip Technology Inc. and its subsidiaries.
"""COE (IEEE 1722B) parsing and reassembly against a model of the emulator."""

from __future__ import annotations

import struct

from emulator_model import CoeEmulatorModel

from hsb_tools.checks import StreamChecker
from hsb_tools.coe import (
    FLAG_FRAME_START,
    CoeAssembler,
    CoeExpectations,
    CoePacket,
)

DESTINATION = bytes.fromhex("02005e100001")


def run(
    packets: list[bytes], width: int, height: int, channel: int = 0
) -> StreamChecker:
    assembler = CoeAssembler(
        CoeExpectations(width * height * 3 // 2, 1408, channel, DESTINATION)
    )
    checker = StreamChecker(width, height)
    for raw in packets:
        packet = CoePacket.parse(memoryview(raw))
        assert packet is not None
        frame = assembler.handle(packet)
        if frame is not None:
            checker.add(frame)
    return checker


def test_header_fields_parse() -> None:
    raw = CoeEmulatorModel(channel=5, frame_number=6).frame_packets(bytes(3072))[0]

    packet = CoePacket.parse(memoryview(raw))

    assert packet.destination == DESTINATION
    assert packet.channel == 5
    assert packet.header_frame_number == 6 & 3
    assert packet.offset == 0
    assert packet.frame_flags & FLAG_FRAME_START


def test_clean_stream_passes(prbs_frames, small_size) -> None:
    report = run(CoeEmulatorModel().stream(prbs_frames), *small_size).report

    assert report.frames == len(prbs_frames)
    assert report.passed, report.issues


def test_psn_wraps_at_7_bits(prbs_frames, small_size) -> None:
    report = run(CoeEmulatorModel(psn=126).stream(prbs_frames), *small_size).report

    assert report.psn_gaps == 0
    assert report.passed, report.issues


def test_dropped_packet_is_detected(prbs_frames, small_size) -> None:
    packets = CoeEmulatorModel().stream(prbs_frames)
    del packets[4]

    report = run(packets, *small_size).report

    assert report.psn_gaps == 1
    assert not report.passed


def test_wrong_destination_mac_is_reported(prbs_frames, small_size) -> None:
    model = CoeEmulatorModel(destination=bytes(6))

    report = run(model.stream(prbs_frames), *small_size).report

    assert any("destination" in issue for issue in report.issues)


def test_wrong_channel_is_reported(prbs_frames, small_size) -> None:
    report = run(
        CoeEmulatorModel(channel=3).stream(prbs_frames), *small_size, channel=0
    ).report

    assert any("channel" in issue for issue in report.issues)


def test_header_frame_number_mismatch_is_reported(prbs_frames, small_size) -> None:
    packets = CoeEmulatorModel().stream(prbs_frames)
    last = bytearray(packets[2])
    (address,) = struct.unpack_from(">I", last, 42)
    struct.pack_into(">I", last, 42, address ^ (1 << 28))
    packets[2] = bytes(last)

    report = run(packets, *small_size).report

    assert any("header frame number" in issue for issue in report.issues)


def test_missing_frame_start_flag_is_reported(prbs_frames, small_size) -> None:
    packets = CoeEmulatorModel().stream(prbs_frames)
    first = bytearray(packets[0])
    first[41] = 0
    packets[0] = bytes(first)

    report = run(packets, *small_size).report

    assert any("FRAME_START" in issue for issue in report.issues)


def test_corrupted_payload_fails_content_check(prbs_frames, small_size) -> None:
    packets = CoeEmulatorModel().stream(prbs_frames)
    corrupted = bytearray(packets[1])
    corrupted[200] ^= 0x55
    packets[1] = bytes(corrupted)

    report = run(packets, *small_size).report

    assert report.pattern_failures == 1


def test_non_coe_frame_is_ignored() -> None:
    assert CoePacket.parse(memoryview(bytes(60))) is None
