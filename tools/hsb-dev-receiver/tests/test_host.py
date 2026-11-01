# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Microchip Technology Inc. and its subsidiaries.
"""Control protocol and data-plane configuration against a fake HSB device on localhost."""

from __future__ import annotations

import socket
import struct
import threading

import pytest

from hsb_tools import host
from hsb_tools import protocol as hsb
from hsb_tools.coe import QP_ENABLE_1722B


class FakeDevice:
    """Answers WR_DWORD/RD_DWORD like the emulator and records writes in order."""

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.registers: dict[int, int] = {}
        self.writes: list[tuple[int, int]] = []
        self.drop_next = 0
        self._running = True
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while self._running:
            try:
                request, peer = self.sock.recvfrom(2048)
            except TimeoutError:
                continue
            if self.drop_next:
                self.drop_next -= 1
                continue
            command, flags, sequence, _, _ = hsb.CONTROL_HEADER.unpack_from(request)
            (address,) = struct.unpack_from(">I", request, 6)
            if command == hsb.CMD_WR_DWORD:
                (value,) = struct.unpack_from(">I", request, 10)
                self.registers[address] = value
                self.writes.append((address, value))
            else:
                value = self.registers.get(address, 0)
            reply = hsb.CONTROL_HEADER.pack(
                command | hsb.REPLY_FLAG, flags, sequence, 0, 0
            )
            self.sock.sendto(
                reply + struct.pack(">II", address, value) + bytes(4), peer
            )

    def close(self) -> None:
        self._running = False
        self._thread.join()
        self.sock.close()


@pytest.fixture
def device():
    fake = FakeDevice()
    yield fake
    fake.close()


@pytest.fixture
def control(device):
    channel = host.ControlChannel("127.0.0.1", device.port)
    yield channel
    channel.close()


def test_write_then_read_back(control) -> None:
    control.write(0x1018, 1382400)

    assert control.read(0x1018) == 1382400


def test_lost_reply_is_retried(device, control) -> None:
    device.drop_next = 2

    control.write(0x1000, 7)

    assert device.registers[0x1000] == 7


def test_roce_configuration_opens_gate_last(device, control) -> None:
    payload = host.configure_roce(control, "10.0.0.2", 45000, frame_size=1382400)

    assert payload == 1408
    assert device.writes[-1] == (hsb.DP_HOST_UDP_PORT, 45000)
    registers = device.registers
    assert registers[hsb.DP_PACKET_SIZE] == 11
    assert registers[hsb.DP_BUFFER_LENGTH] == 1382400
    assert registers[hsb.DP_HOST_IP] == 0x0A000002
    assert registers[hsb.DP_VP_MASK] & hsb.VP_MASK_SENSOR_0
    gate_index = device.writes.index((hsb.DP_HOST_UDP_PORT, 45000))
    packet_size_index = device.writes.index((hsb.DP_PACKET_SIZE, 11))
    assert packet_size_index < gate_index


def test_coe_configuration_enables_1722b_last(device, control) -> None:
    mac = bytes.fromhex("02005e100001")

    host.configure_coe(control, mac, frame_size=1382400, bytes_per_line=1280, channel=2)

    address, qp = device.writes[-1]
    assert address == hsb.DP_QP
    assert qp & QP_ENABLE_1722B
    assert qp & 0x3F == 2
    assert (qp >> 25) & 0x7F == 11  # 2**11 = 2048 >= 1280
    assert device.registers[hsb.DP_HOST_MAC_HIGH] == 0x0200
    assert device.registers[hsb.DP_HOST_MAC_LOW] == 0x5E100001


def test_stop_closes_both_gates(device, control) -> None:
    host.configure_roce(control, "10.0.0.2", 45000, frame_size=3072)

    host.stop_streaming(control)

    assert device.registers[hsb.DP_HOST_UDP_PORT] == 0
    assert device.registers[hsb.DP_QP] == 0


@pytest.fixture
def silent_control(monkeypatch):
    monkeypatch.setattr(host, "CONTROL_TIMEOUT_S", 0.01)
    gone = FakeDevice()
    gone.close()
    channel = host.ControlChannel("127.0.0.1", gone.port)
    yield channel
    channel.close()


def test_silent_device_raises_unreachable(silent_control) -> None:
    with pytest.raises(host.DeviceUnreachableError):
        silent_control.write(hsb.DP_QP, 0)


def test_stop_reports_device_already_gone(silent_control) -> None:
    assert host.stop_streaming_if_reachable(silent_control) is False


def test_stop_if_reachable_closes_gates(device, control) -> None:
    host.configure_roce(control, "10.0.0.2", 45000, frame_size=3072)

    assert host.stop_streaming_if_reachable(control) is True
    assert device.registers[hsb.DP_HOST_UDP_PORT] == 0


@pytest.mark.parametrize(
    ("text", "expected"),
    [("02:00:5e:10:00:01", "02005e100001"), ("02-00-5E-10-00-01", "02005e100001")],
)
def test_parse_mac(text: str, expected: str) -> None:
    assert host.parse_mac(text).hex() == expected


def test_parse_mac_rejects_bad_length() -> None:
    with pytest.raises(ValueError):
        host.parse_mac("02:00:5e")
