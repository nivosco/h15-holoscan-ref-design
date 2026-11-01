# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Microchip Technology Inc. and its subsidiaries.
"""Minimal Holoscan Sensor Bridge host for bring-up without a Jetson.

Discovers an HSB Emulator and configures its data plane towards this machine.

RoCEv2: receives over plain UDP, reassembles NV12 frames, verifies every frame
against the synthetic sender's test pattern and protocol contract, and optionally
displays or saves them.

COE: configures the stream towards --host-mac and holds it for --seconds so it can be
captured (for example with tcpdump on the board) and checked with hsb_pcap_check.py.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from pathlib import Path

from hsb_tools import host
from hsb_tools.assembler import FrameAssembler, StreamExpectations
from hsb_tools.checks import StreamChecker
from hsb_tools.patterns import nv12_frame_size
from hsb_tools.protocol import (
    ALIGNMENT,
    DEFAULT_MTU,
    DEFAULT_QP,
    DEFAULT_RKEY,
    RocePacket,
    roundup,
)
from hsb_tools.viewer import FrameSink

RECEIVE_BUFFER_BYTES = 128 * 1024 * 1024
MAX_DATAGRAM = 9000
STATS_INTERVAL_S = 1.0
BITS_PER_BYTE = 8
MEGA = 1e6


def parse_arguments() -> argparse.Namespace:
    """Parses the command line.

    Returns:
        Parsed arguments.
    """
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--device-ip", help="accept only this device (default: first discovered)"
    )
    parser.add_argument("--transport", choices=("roce", "coe"), default="roce")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--mtu", type=int, default=DEFAULT_MTU)
    parser.add_argument(
        "--port", type=int, default=0, help="local UDP port (default: ephemeral)"
    )
    parser.add_argument(
        "--host-mac", help="COE destination MAC (required for --transport coe)"
    )
    parser.add_argument("--seconds", type=float, default=0, help="stop after N seconds")
    parser.add_argument("--discover-timeout", type=float, default=10)
    parser.add_argument(
        "--no-verify", action="store_true", help="skip test-pattern comparison"
    )
    parser.add_argument("--show", action="store_true", help="display frames (OpenCV)")
    parser.add_argument(
        "--save-dir", type=Path, help="save frames as .nv12 and .png here"
    )
    parser.add_argument(
        "--save-every", type=int, default=60, help="save one frame in N"
    )
    parser.add_argument(
        "--save-failed", action="store_true", help="also save every failed frame"
    )
    parser.add_argument("--report", type=Path, help="write a JSON summary here")
    parser.add_argument(
        "--allow-loss",
        action="store_true",
        help="pass if every complete frame was correct (lossy links such as a laptop)",
    )
    return parser.parse_args()


def print_interval(stats: dict[str, float], elapsed_s: float) -> None:
    """Prints one stats line.

    Args:
        stats: Interval counters.
        elapsed_s: Interval length in seconds.
    """
    print(
        f"fps={stats['frames'] / elapsed_s:.1f}  "
        f"Mbit/s={stats['bytes'] * BITS_PER_BYTE / MEGA / elapsed_s:.1f}  "
        f"pkts/s={stats['packets'] / elapsed_s:.0f}  "
        f"ok={stats['ok']}  bad={stats['bad']}",
        flush=True,
    )


def hold_coe_stream(
    args: argparse.Namespace, control: host.ControlChannel, frame_size: int
) -> int:
    """Configures COE towards --host-mac and keeps it enabled.

    Args:
        args: Parsed arguments.
        control: Control channel to the device.
        frame_size: Frame size in bytes.

    Returns:
        Process exit code.
    """
    if not args.host_mac:
        print("--transport coe needs --host-mac", file=sys.stderr)
        return 2
    payload = host.configure_coe(
        control,
        host.parse_mac(args.host_mac),
        frame_size,
        bytes_per_line=args.width,
        mtu=args.mtu,
    )
    print(
        f"COE streaming to {args.host_mac}, {payload} bytes/packet; "
        "Ctrl-C or --seconds to stop"
    )
    deadline = time.monotonic() + args.seconds if args.seconds else None
    try:
        while deadline is None or time.monotonic() < deadline:
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    return 0


def receive_roce(
    args: argparse.Namespace, control: host.ControlChannel, device_ip: str
) -> int:
    """Receives and verifies a RoCEv2 stream.

    Args:
        args: Parsed arguments.
        control: Control channel to the device.
        device_ip: Device IP address.

    Returns:
        Process exit code: 0 when every frame passed.
    """
    frame_size = nv12_frame_size(args.width, args.height)
    host_ip = host.local_address_towards(device_ip)
    data_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    data_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RECEIVE_BUFFER_BYTES)
    data_sock.bind((host_ip, args.port))
    data_sock.settimeout(STATS_INTERVAL_S)
    host_port = data_sock.getsockname()[1]
    granted_kib = data_sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF) // 1024
    print(f"receiving on {host_ip}:{host_port}, SO_RCVBUF={granted_kib} KiB")

    payload_size = host.configure_roce(
        control, host_ip, host_port, frame_size, args.mtu
    )
    expectations = StreamExpectations(
        frame_size=frame_size,
        payload_size=payload_size,
        qp=DEFAULT_QP,
        rkey=DEFAULT_RKEY,
        page_increment=roundup(frame_size, ALIGNMENT) + ALIGNMENT,
    )
    assembler = FrameAssembler(expectations)
    checker = StreamChecker(args.width, args.height, verify_pattern=not args.no_verify)
    sink = FrameSink(
        args.width,
        args.height,
        args.show,
        args.save_dir,
        args.save_every,
        args.save_failed,
    )
    print(
        f"configured {args.width}x{args.height} NV12 ({frame_size} bytes), "
        f"{payload_size} bytes/packet"
    )

    buffer = bytearray(MAX_DATAGRAM)
    view = memoryview(buffer)
    interval = {"frames": 0, "bytes": 0, "packets": 0, "ok": 0, "bad": 0}
    start = interval_start = time.monotonic()
    try:
        while not args.seconds or time.monotonic() - start < args.seconds:
            try:
                length = data_sock.recv_into(buffer)
            except TimeoutError:
                length = 0
            packet = RocePacket.parse(view[:length]) if length else None
            frame = assembler.handle(packet) if packet else None
            interval["packets"] += 1 if packet else 0
            if frame is not None:
                ok = checker.add(frame)
                interval["frames"] += 1
                interval["bytes"] += frame.bytes_received
                interval["ok" if ok else "bad"] += 1
                sink.handle(frame.data, frame.metadata.frame_number, ok)
            now = time.monotonic()
            if now - interval_start >= STATS_INTERVAL_S:
                print_interval(interval, now - interval_start)
                interval = dict.fromkeys(interval, 0)
                interval_start = now
    except KeyboardInterrupt:
        pass
    finally:
        sink.close()
        data_sock.close()

    report = checker.report
    summary = report.summary()
    summary["passed_strict"] = report.passed
    summary["passed_allowing_loss"] = report.passed_allowing_loss
    passed = report.passed_allowing_loss if args.allow_loss else report.passed
    summary["passed"] = passed
    expected_frames = report.frames + report.frames_lost
    if expected_frames:
        summary["frame_loss_percent"] = round(
            100.0 * (expected_frames - report.frames_complete) / expected_frames, 2
        )
    print(
        json.dumps(
            {key: value for key, value in summary.items() if key != "issues"}, indent=2
        )
    )
    for issue in checker.report.issues[:10]:
        print(f"  issue: {issue}")
    if args.report:
        args.report.write_text(json.dumps(summary, indent=2))
    return 0 if passed else 1


def main() -> int:
    """Runs discovery, configuration and the selected mode.

    Returns:
        Process exit code.
    """
    args = parse_arguments()
    device = host.discover(args.discover_timeout, args.device_ip)
    print(f"found HSB device {device.ip_address} ({device.mac_address.hex(':')})")
    control = host.ControlChannel(device.ip_address)
    try:
        if args.transport == "coe":
            return hold_coe_stream(
                args, control, nv12_frame_size(args.width, args.height)
            )
        return receive_roce(args, control, device.ip_address)
    finally:
        if not host.stop_streaming_if_reachable(control):
            print("device no longer answering; stream already stopped")
        control.close()


if __name__ == "__main__":
    sys.exit(main())
