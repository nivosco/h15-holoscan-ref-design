# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Microchip Technology Inc. and its subsidiaries.
"""Minimal HSB host: enumeration, control-plane register access, data-plane setup."""

from __future__ import annotations

import socket
import struct
import time
from dataclasses import dataclass

from . import protocol as hsb
from .coe import COE_HEADER_LENGTH, QP_ENABLE_1722B, QP_LINE_SHIFT

CONTROL_TIMEOUT_S = 1.0
CONTROL_RETRIES = 5


@dataclass(frozen=True)
class Device:
    """An HSB device found by enumeration."""

    ip_address: str
    mac_address: bytes


def discover(timeout_s: float, device_ip: str | None = None) -> Device:
    """Waits for an HSB enumeration (BOOTP) broadcast.

    Args:
        timeout_s: How long to wait.
        device_ip: Accept only this device, or the first one seen if None.

    Returns:
        The discovered device.

    Raises:
        TimeoutError: No matching device was seen in time.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("", hsb.BOOTP_REQUEST_PORT))
    sock.settimeout(0.2)
    deadline = time.monotonic() + timeout_s
    try:
        while time.monotonic() < deadline:
            try:
                data, (source_ip, _) = sock.recvfrom(2048)
            except TimeoutError:
                continue
            if len(data) < hsb.BOOTP_PACKET_SIZE:
                continue
            if device_ip and source_ip != device_ip:
                continue
            mac_end = hsb.BOOTP_CHADDR_OFFSET + hsb.MAC_LENGTH
            return Device(source_ip, bytes(data[hsb.BOOTP_CHADDR_OFFSET : mac_end]))
    finally:
        sock.close()
    raise TimeoutError(f"no HSB enumeration packet within {timeout_s:.0f}s")


class DeviceUnreachableError(RuntimeError):
    """The device did not answer a control command after all retries."""


class ControlChannel:
    """Register read/write over the HSB control protocol (UDP 8192)."""

    def __init__(self, device_ip: str, port: int = hsb.CONTROL_PORT) -> None:
        """Opens the control socket.

        Args:
            device_ip: IP address of the HSB device.
            port: Control UDP port.
        """
        self._device = (device_ip, port)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.settimeout(CONTROL_TIMEOUT_S)
        self._sequence = 0x100

    def close(self) -> None:
        """Closes the control socket."""
        self._sock.close()

    def write(self, address: int, value: int) -> None:
        """Writes one 32-bit register.

        Args:
            address: Register address.
            value: Value to write.

        Raises:
            RuntimeError: The device rejected the write or did not answer.
        """
        payload = struct.pack(">II", address, value & 0xFFFFFFFF)
        self._transact(hsb.CMD_WR_DWORD, payload)

    def read(self, address: int) -> int:
        """Reads one 32-bit register.

        Args:
            address: Register address.

        Returns:
            The register value.

        Raises:
            RuntimeError: The device rejected the read or did not answer.
        """
        reply = self._transact(hsb.CMD_RD_DWORD, struct.pack(">I", address))
        _, value = struct.unpack_from(">II", reply, hsb.CONTROL_HEADER.size)
        return value

    def _transact(self, command: int, payload: bytes) -> bytes:
        self._sequence = (self._sequence + 1) & 0xFFFF
        header = hsb.CONTROL_HEADER.pack(
            command, hsb.FLAG_ACK_REQUEST, self._sequence, 0, 0
        )
        request = header + payload
        for _ in range(CONTROL_RETRIES):
            self._sock.sendto(request, self._device)
            try:
                while True:
                    reply = self._sock.recv(2048)
                    reply_command, _, sequence, status, _ = (
                        hsb.CONTROL_HEADER.unpack_from(reply)
                    )
                    if (
                        sequence != self._sequence
                        or reply_command != command | hsb.REPLY_FLAG
                    ):
                        continue
                    if status != hsb.STATUS_SUCCESS:
                        raise RuntimeError(
                            f"command 0x{command:02x} failed, status 0x{status:02x}"
                        )
                    return reply
            except TimeoutError:
                continue
        raise DeviceUnreachableError(
            f"no reply to command 0x{command:02x} from {self._device[0]}"
        )


def local_address_towards(device_ip: str) -> str:
    """Finds the local IP the OS would use to reach the device.

    Args:
        device_ip: IP address of the HSB device.

    Returns:
        The local IPv4 address.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((device_ip, hsb.CONTROL_PORT))
        return probe.getsockname()[0]
    finally:
        probe.close()


def ip_to_int(ip_address: str) -> int:
    """Converts dotted IPv4 to the host-order integer HSB registers expect.

    Args:
        ip_address: Dotted IPv4 address.

    Returns:
        The address as (a<<24)|(b<<16)|(c<<8)|d.
    """
    return struct.unpack(">I", socket.inet_aton(ip_address))[0]


def mac_registers(mac: bytes) -> tuple[int, int]:
    """Encodes a MAC address into the DP_HOST_MAC_LOW/HIGH register values.

    Args:
        mac: 6-byte MAC address.

    Returns:
        (low, high) register values.
    """
    return int.from_bytes(mac[2:6], "big"), int.from_bytes(mac[0:2], "big")


def parse_mac(text: str) -> bytes:
    """Parses aa:bb:cc:dd:ee:ff or aa-bb-cc-dd-ee-ff.

    Args:
        text: MAC address text.

    Returns:
        6 bytes.

    Raises:
        ValueError: Malformed address.
    """
    mac = bytes.fromhex(text.replace(":", "").replace("-", ""))
    if len(mac) != hsb.MAC_LENGTH:
        raise ValueError(f"bad MAC address: {text}")
    return mac


def _disable_virtual_port(control: ControlChannel) -> None:
    control.write(hsb.DP_VP_MASK, control.read(hsb.DP_VP_MASK) & ~hsb.VP_MASK_SENSOR_0)


def _enable_virtual_port(control: ControlChannel) -> None:
    control.write(hsb.DP_VP_MASK, control.read(hsb.DP_VP_MASK) | hsb.VP_MASK_SENSOR_0)


def configure_roce(
    control: ControlChannel,
    host_ip: str,
    host_port: int,
    frame_size: int,
    mtu: int = hsb.DEFAULT_MTU,
    host_mac: bytes = bytes(hsb.MAC_LENGTH),
) -> int:
    """Points the device's RoCEv2 data plane at this host.

    HOST_UDP_PORT is written last: a nonzero HOST_IP and HOST_UDP_PORT is what opens the
    emulator's transmit gate, and PACKET_SIZE must already be valid by then.

    Args:
        control: Control channel to the device.
        host_ip: Local IP the frames are sent to.
        host_port: Local UDP port the frames are sent to.
        frame_size: Frame size in bytes.
        mtu: Link MTU.
        host_mac: Local MAC (not used by the Linux emulator's RoCE path).

    Returns:
        The payload size per packet in bytes.
    """
    payload_size = hsb.payload_size_for_mtu(mtu)
    page_size = hsb.roundup(frame_size, hsb.ALIGNMENT) + hsb.ALIGNMENT
    mac_low, mac_high = mac_registers(host_mac)

    control.write(hsb.DP_HOST_UDP_PORT, 0)
    control.write(hsb.DP_QP, 0)
    _disable_virtual_port(control)
    for address, value in (
        (hsb.DP_PACKET_SIZE, payload_size // hsb.ALIGNMENT),
        (hsb.DP_PACKET_UDP_PORT, hsb.HSB_SOURCE_UDP_PORT),
        (hsb.DP_BUFFER_LENGTH, frame_size),
        (hsb.DP_QP, hsb.DEFAULT_QP),
        (hsb.DP_RKEY, hsb.DEFAULT_RKEY),
        (hsb.DP_PAGE_LSB, 0),
        (hsb.DP_PAGE_MSB, 0),
        (hsb.DP_PAGE_INC, page_size // hsb.ALIGNMENT),
        (hsb.DP_MAX_BUFF, 0),
        (hsb.DP_HOST_MAC_LOW, mac_low),
        (hsb.DP_HOST_MAC_HIGH, mac_high),
        (hsb.DP_HOST_IP, ip_to_int(host_ip)),
    ):
        control.write(address, value)
    _enable_virtual_port(control)
    control.write(hsb.DP_HOST_UDP_PORT, host_port)
    return payload_size


def coe_line_shift(bytes_per_line: int) -> int:
    """Returns the smallest n with 2**n >= bytes_per_line, as NVIDIA's host computes it.

    Args:
        bytes_per_line: Bytes per image line.

    Returns:
        log2 of the line threshold.
    """
    return max(0, (bytes_per_line - 1).bit_length())


def configure_coe(
    control: ControlChannel,
    host_mac: bytes,
    frame_size: int,
    bytes_per_line: int,
    channel: int = 0,
    mtu: int = hsb.DEFAULT_MTU,
) -> int:
    """Points the device's COE (IEEE 1722B) data plane at a host MAC.

    DP_QP is written last because its 1722B-enable bit is the emulator's transmit gate.

    Args:
        control: Control channel to the device.
        host_mac: Destination MAC for the L2 frames.
        frame_size: Frame size in bytes.
        bytes_per_line: Bytes per image line (sets the LINE_END cadence).
        channel: 6-bit COE channel.
        mtu: Link MTU.

    Returns:
        The payload size per packet in bytes.
    """
    payload_size = (mtu - COE_HEADER_LENGTH) // hsb.ALIGNMENT * hsb.ALIGNMENT
    mac_low, mac_high = mac_registers(host_mac)

    control.write(hsb.DP_HOST_UDP_PORT, 0)
    control.write(hsb.DP_QP, 0)
    _disable_virtual_port(control)
    for address, value in (
        (hsb.DP_PACKET_SIZE, payload_size // hsb.ALIGNMENT),
        (hsb.DP_PACKET_UDP_PORT, hsb.HSB_SOURCE_UDP_PORT),
        (hsb.DP_BUFFER_LENGTH, frame_size),
        (hsb.DP_HOST_MAC_LOW, mac_low),
        (hsb.DP_HOST_MAC_HIGH, mac_high),
    ):
        control.write(address, value)
    _enable_virtual_port(control)
    qp = (
        QP_ENABLE_1722B
        | (coe_line_shift(bytes_per_line) << QP_LINE_SHIFT)
        | (channel & 0x3F)
    )
    control.write(hsb.DP_QP, qp)
    return payload_size


def stop_streaming(control: ControlChannel) -> None:
    """Closes both transmit gates (RoCE: HOST_UDP_PORT, COE: QP enable bit).

    Args:
        control: Control channel to the device.
    """
    control.write(hsb.DP_HOST_UDP_PORT, 0)
    control.write(hsb.DP_QP, 0)


def stop_streaming_if_reachable(control: ControlChannel) -> bool:
    """Closes both transmit gates unless the device has already gone away.

    A sender that exited first has nothing left to stop, so its silence is not an
    error at shutdown.

    Args:
        control: Control channel to the device.

    Returns:
        True if the gates were closed, False if the device did not answer.

    Raises:
        RuntimeError: The device answered but rejected a write.
    """
    try:
        stop_streaming(control)
    except DeviceUnreachableError:
        return False
    return True
