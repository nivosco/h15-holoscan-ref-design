# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Microchip Technology Inc. and its subsidiaries.
"""Capture parsing, iCRC verification and the offline checker end to end."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from emulator_model import (
    CoeEmulatorModel,
    RoceEmulatorModel,
    ipv4_udp_ethernet,
    write_pcap,
)

from hsb_tools.pcap import (
    CapturedFrame,
    compute_icrc,
    icrc_matches,
    read_capture,
    roce_datagram,
)

TOOL = Path(__file__).resolve().parents[1] / "hsb_pcap_check.py"
UDP_PORT = 45000


def roce_capture(path: Path, frames: list[bytes]) -> None:
    payloads = RoceEmulatorModel().stream(frames)
    write_pcap(path, [ipv4_udp_ethernet(payload, UDP_PORT) for payload in payloads])


def run_checker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TOOL), *args], capture_output=True, text=True, check=False
    )


def test_icrc_detects_single_bit_flip(prbs_frames) -> None:
    payload = RoceEmulatorModel().stream(prbs_frames)[0]
    frame = ipv4_udp_ethernet(payload, UDP_PORT)
    good = roce_datagram(CapturedFrame(0, frame))
    corrupted = bytearray(frame)
    corrupted[100] ^= 0x01
    bad = roce_datagram(CapturedFrame(0, bytes(corrupted)))

    assert icrc_matches(good)
    assert not icrc_matches(bad)


def test_icrc_ignores_variant_fields(prbs_frames) -> None:
    payload = RoceEmulatorModel().stream(prbs_frames)[0]
    frame = bytearray(ipv4_udp_ethernet(payload, UDP_PORT))
    frame[14 + 8] = 1  # TTL changed by a router
    frame[14 + 10 : 14 + 12] = b"\x12\x34"  # IP checksum filled by the kernel

    assert icrc_matches(roce_datagram(CapturedFrame(0, bytes(frame))))


def test_icrc_known_answer() -> None:
    # Independent bitwise CRC-32 over the masked pseudo-header, to pin the masking rules.
    ip_header = bytes.fromhex("45000034123440004011000" + "00a0000010a000002")
    udp_header = bytes.fromhex("3000afc800200000")
    body = bytes(range(16))
    masked = bytearray(b"\xff" * 8 + ip_header + udp_header + body)
    masked[8 + 1] = 0xFF
    masked[8 + 8] = 0xFF
    masked[8 + 10 : 8 + 12] = b"\xff\xff"
    masked[8 + 20 + 6 : 8 + 20 + 8] = b"\xff\xff"
    crc = 0xFFFFFFFF
    for byte in masked:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0xEDB88320 if crc & 1 else 0)

    assert compute_icrc(ip_header, udp_header, body) == crc ^ 0xFFFFFFFF


def test_pcap_reader_yields_frames(tmp_path, prbs_frames) -> None:
    capture = tmp_path / "roce.pcap"
    roce_capture(capture, prbs_frames)

    frames = list(read_capture(capture))

    assert len(frames) == len(RoceEmulatorModel().stream(prbs_frames))


def test_offline_checker_passes_clean_roce_capture(
    tmp_path, prbs_frames, small_size
) -> None:
    capture = tmp_path / "roce.pcap"
    roce_capture(capture, prbs_frames)

    result = run_checker(
        str(capture), "--width", str(small_size[0]), "--height", str(small_size[1])
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert '"icrc_bad": 0' in result.stdout


def test_offline_checker_fails_corrupted_roce_capture(
    tmp_path, prbs_frames, small_size
) -> None:
    payloads = RoceEmulatorModel().stream(prbs_frames)
    frames = [ipv4_udp_ethernet(payload, UDP_PORT) for payload in payloads]
    corrupted = bytearray(frames[2])
    corrupted[80] ^= 0xFF
    frames[2] = bytes(corrupted)
    capture = tmp_path / "bad.pcap"
    write_pcap(capture, frames)

    result = run_checker(
        str(capture), "--width", str(small_size[0]), "--height", str(small_size[1])
    )

    assert result.returncode == 1
    assert '"icrc_bad": 1' in result.stdout


def test_offline_checker_passes_clean_coe_capture(
    tmp_path, prbs_frames, small_size
) -> None:
    capture = tmp_path / "coe.pcap"
    write_pcap(capture, CoeEmulatorModel().stream(prbs_frames))

    result = run_checker(
        str(capture),
        "--transport",
        "coe",
        "--width",
        str(small_size[0]),
        "--height",
        str(small_size[1]),
    )

    assert result.returncode == 0, result.stdout + result.stderr
