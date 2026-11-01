# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Microchip Technology Inc. and its subsidiaries.
"""Offline verification of a captured HSB stream (RoCEv2 or COE).

Checks every packet's framing, sequence and placement, every frame's metadata and
content against the synthetic sender's test pattern, and for RoCEv2 every packet's iCRC.

Captures taken on the sender (e.g. tcpdump on the Hailo-15) can miss packets when the
capture itself cannot keep up; such gaps show up as PSN gaps and incomplete frames.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from hsb_tools.assembler import FrameAssembler, StreamExpectations
from hsb_tools.checks import StreamChecker
from hsb_tools.coe import CoeAssembler, CoeExpectations, CoePacket
from hsb_tools.host import parse_mac
from hsb_tools.patterns import nv12_frame_size
from hsb_tools.pcap import coe_ethernet_frame, icrc_matches, read_capture, roce_datagram
from hsb_tools.protocol import (
    ALIGNMENT,
    DEFAULT_MTU,
    DEFAULT_QP,
    DEFAULT_RKEY,
    RocePacket,
    payload_size_for_mtu,
    roundup,
)
from hsb_tools.viewer import FrameSink

COE_HEADER_OVERHEAD = 46


def parse_arguments() -> argparse.Namespace:
    """Parses the command line.

    Returns:
        Parsed arguments.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("capture", type=Path, help="pcap or pcapng file")
    parser.add_argument("--transport", choices=("roce", "coe"), default="roce")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--mtu", type=int, default=DEFAULT_MTU)
    parser.add_argument(
        "--udp-port", type=int, help="RoCE destination port (default: any)"
    )
    parser.add_argument(
        "--host-mac", help="expected COE destination MAC (default: first seen)"
    )
    parser.add_argument("--channel", type=int, default=0, help="expected COE channel")
    parser.add_argument(
        "--no-verify", action="store_true", help="skip test-pattern comparison"
    )
    parser.add_argument(
        "--check-crc",
        action="store_true",
        help="verify each complete frame against the CRC-32 in its metadata",
    )
    parser.add_argument(
        "--save-dir", type=Path, help="save frames as .nv12 and .png here"
    )
    parser.add_argument("--save-every", type=int, default=60)
    parser.add_argument("--report", type=Path, help="write a JSON summary here")
    return parser.parse_args()


def check_roce(
    args: argparse.Namespace, checker: StreamChecker, sink: FrameSink
) -> dict:
    """Verifies a RoCEv2 capture.

    Args:
        args: Parsed arguments.
        checker: Stream checker to feed.
        sink: Frame sink for saving.

    Returns:
        Packet-level counters.
    """
    frame_size = nv12_frame_size(args.width, args.height)
    assembler = FrameAssembler(
        StreamExpectations(
            frame_size=frame_size,
            payload_size=payload_size_for_mtu(args.mtu),
            qp=DEFAULT_QP,
            rkey=DEFAULT_RKEY,
            page_increment=roundup(frame_size, ALIGNMENT) + ALIGNMENT,
        )
    )
    counters = {"datagrams": 0, "icrc_ok": 0, "icrc_bad": 0, "unparsed": 0}
    for captured in read_capture(args.capture):
        datagram = roce_datagram(captured, args.udp_port)
        if datagram is None:
            continue
        counters["datagrams"] += 1
        counters["icrc_ok" if icrc_matches(datagram) else "icrc_bad"] += 1
        packet = RocePacket.parse(memoryview(datagram.udp_payload))
        if packet is None:
            counters["unparsed"] += 1
            continue
        frame = assembler.handle(packet)
        if frame is not None:
            sink.handle(frame.data, frame.metadata.frame_number, checker.add(frame))
    return counters


def check_coe(
    args: argparse.Namespace, checker: StreamChecker, sink: FrameSink
) -> dict:
    """Verifies a COE capture.

    Args:
        args: Parsed arguments.
        checker: Stream checker to feed.
        sink: Frame sink for saving.

    Returns:
        Packet-level counters.
    """
    frame_size = nv12_frame_size(args.width, args.height)
    payload_size = (args.mtu - COE_HEADER_OVERHEAD) // ALIGNMENT * ALIGNMENT
    assembler: CoeAssembler | None = None
    counters = {"coe_frames": 0, "unparsed": 0, "line_end_flags": 0}
    for captured in read_capture(args.capture):
        ethernet = coe_ethernet_frame(captured)
        if ethernet is None:
            continue
        packet = CoePacket.parse(memoryview(ethernet))
        if packet is None:
            counters["unparsed"] += 1
            continue
        counters["coe_frames"] += 1
        counters["line_end_flags"] += 1 if packet.frame_flags & 0x10 else 0
        if assembler is None:
            destination = (
                parse_mac(args.host_mac) if args.host_mac else packet.destination
            )
            assembler = CoeAssembler(
                CoeExpectations(frame_size, payload_size, args.channel, destination)
            )
        frame = assembler.handle(packet)
        if frame is not None:
            sink.handle(frame.data, frame.metadata.frame_number, checker.add(frame))
    return counters


def main() -> int:
    """Runs the offline check.

    Returns:
        0 when every frame and (for RoCE) every iCRC passed.
    """
    args = parse_arguments()
    checker = StreamChecker(
        args.width,
        args.height,
        verify_pattern=not args.no_verify,
        verify_crc=args.check_crc,
    )
    sink = FrameSink(
        args.width, args.height, False, args.save_dir, args.save_every, True
    )
    if args.transport == "roce":
        counters = check_roce(args, checker, sink)
    else:
        counters = check_coe(args, checker, sink)

    summary = {"transport": args.transport, **counters, **checker.report.summary()}
    passed = checker.report.passed and counters.get("icrc_bad", 0) == 0
    summary["passed"] = passed
    print(
        json.dumps(
            {key: value for key, value in summary.items() if key != "issues"}, indent=2
        )
    )
    for issue in checker.report.issues[:15]:
        print(f"  issue: {issue}")
    if args.report:
        args.report.write_text(json.dumps(summary, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
