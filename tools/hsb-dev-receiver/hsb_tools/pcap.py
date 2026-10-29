# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Microchip Technology Inc. and its subsidiaries.
"""Minimal pcap/pcapng reader and RoCEv2 iCRC verification (no third-party deps)."""

from __future__ import annotations

import struct
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from .coe import ETHERNET_HEADER_LENGTH, ETHERTYPE_COE
from .protocol import HSB_SOURCE_UDP_PORT, ICRC_LENGTH

LINKTYPE_ETHERNET = 1
PCAP_MAGIC_MICROSECONDS = 0xA1B2C3D4
PCAP_MAGIC_NANOSECONDS = 0xA1B23C4D
PCAPNG_SECTION_HEADER = 0x0A0D0D0A
PCAPNG_INTERFACE_DESCRIPTION = 0x00000001
PCAPNG_ENHANCED_PACKET = 0x00000006
PCAPNG_BYTE_ORDER_MAGIC = 0x1A2B3C4D

ETHERTYPE_IPV4 = 0x0800
ETHERTYPE_VLAN = 0x8100
IP_PROTOCOL_UDP = 17
IPV4_MIN_HEADER = 20
UDP_HEADER_LENGTH = 8
ICRC_SEED = b"\xff" * 8


@dataclass(frozen=True)
class CapturedFrame:
    """One captured Ethernet frame."""

    timestamp_ns: int
    data: bytes


@dataclass(frozen=True)
class RoceDatagram:
    """A RoCEv2 UDP datagram extracted from a capture."""

    timestamp_ns: int
    ip_header: bytes
    udp_header: bytes
    udp_payload: bytes


def _read_pcap(stream: bytes) -> Iterator[CapturedFrame]:
    (magic,) = struct.unpack_from("<I", stream)
    endian = "<"
    if magic not in (PCAP_MAGIC_MICROSECONDS, PCAP_MAGIC_NANOSECONDS):
        endian = ">"
        (magic,) = struct.unpack_from(">I", stream)
    nanosecond_resolution = magic == PCAP_MAGIC_NANOSECONDS
    linktype = struct.unpack_from(f"{endian}I", stream, 20)[0]
    if linktype != LINKTYPE_ETHERNET:
        raise ValueError(f"unsupported pcap link type {linktype}")
    offset = 24
    record = struct.Struct(f"{endian}IIII")
    while offset + record.size <= len(stream):
        seconds, fraction, captured, _ = record.unpack_from(stream, offset)
        offset += record.size
        scale = 1 if nanosecond_resolution else 1000
        yield CapturedFrame(
            seconds * 1_000_000_000 + fraction * scale,
            stream[offset : offset + captured],
        )
        offset += captured


def _read_pcapng(stream: bytes) -> Iterator[CapturedFrame]:
    offset = 0
    endian = "<"
    linktypes: list[int] = []
    while offset + 12 <= len(stream):
        block_type, block_length = struct.unpack_from(f"{endian}II", stream, offset)
        if block_type == PCAPNG_SECTION_HEADER:
            (byte_order,) = struct.unpack_from("<I", stream, offset + 8)
            endian = "<" if byte_order == PCAPNG_BYTE_ORDER_MAGIC else ">"
            block_type, block_length = struct.unpack_from(f"{endian}II", stream, offset)
            linktypes = []
        elif block_type == PCAPNG_INTERFACE_DESCRIPTION:
            (linktype,) = struct.unpack_from(f"{endian}H", stream, offset + 8)
            linktypes.append(linktype)
        elif block_type == PCAPNG_ENHANCED_PACKET:
            interface, high, low, captured, _ = struct.unpack_from(
                f"{endian}IIIII", stream, offset + 8
            )
            if interface < len(linktypes) and linktypes[interface] == LINKTYPE_ETHERNET:
                # Default if_tsresol is microseconds.
                timestamp_ns = ((high << 32) | low) * 1000
                start = offset + 28
                yield CapturedFrame(timestamp_ns, stream[start : start + captured])
        if block_length < 12:
            raise ValueError("corrupt pcapng block")
        offset += block_length


def read_capture(path: Path) -> Iterator[CapturedFrame]:
    """Yields Ethernet frames from a pcap or pcapng file.

    Args:
        path: Capture file.

    Yields:
        Captured frames in file order.

    Raises:
        ValueError: Unsupported file format or link type.
    """
    stream = path.read_bytes()
    if len(stream) < 24:
        raise ValueError(f"{path} is too short to be a capture")
    (magic,) = struct.unpack_from("<I", stream)
    if magic == PCAPNG_SECTION_HEADER:
        yield from _read_pcapng(stream)
    else:
        yield from _read_pcap(stream)


def _strip_vlan(frame: bytes) -> tuple[int, int]:
    ethertype_offset = 12
    (ethertype,) = struct.unpack_from(">H", frame, ethertype_offset)
    while ethertype == ETHERTYPE_VLAN:
        ethertype_offset += 4
        (ethertype,) = struct.unpack_from(">H", frame, ethertype_offset)
    return ethertype, ethertype_offset + 2


def roce_datagram(
    frame: CapturedFrame, udp_port: int | None = None
) -> RoceDatagram | None:
    """Extracts an HSB RoCEv2 datagram from an Ethernet frame.

    Args:
        frame: Captured Ethernet frame.
        udp_port: Destination UDP port to accept, or None to accept any datagram whose
            source port is the HSB data source port.

    Returns:
        The datagram, or None if the frame is not HSB RoCEv2 traffic.
    """
    data = frame.data
    if len(data) < ETHERNET_HEADER_LENGTH + IPV4_MIN_HEADER + UDP_HEADER_LENGTH:
        return None
    ethertype, ip_offset = _strip_vlan(data)
    if ethertype != ETHERTYPE_IPV4:
        return None
    header_length = (data[ip_offset] & 0x0F) * 4
    if data[ip_offset + 9] != IP_PROTOCOL_UDP:
        return None
    (total_length,) = struct.unpack_from(">H", data, ip_offset + 2)
    udp_offset = ip_offset + header_length
    source_port, destination_port = struct.unpack_from(">HH", data, udp_offset)
    if source_port != HSB_SOURCE_UDP_PORT:
        return None
    if udp_port is not None and destination_port != udp_port:
        return None
    end = ip_offset + total_length
    if end > len(data):
        return None
    return RoceDatagram(
        timestamp_ns=frame.timestamp_ns,
        ip_header=data[ip_offset:udp_offset],
        udp_header=data[udp_offset : udp_offset + UDP_HEADER_LENGTH],
        udp_payload=data[udp_offset + UDP_HEADER_LENGTH : end],
    )


def coe_ethernet_frame(frame: CapturedFrame) -> bytes | None:
    """Returns the frame if it is COE (ethertype 0x22F0) traffic.

    Args:
        frame: Captured Ethernet frame.

    Returns:
        The Ethernet frame bytes, or None.
    """
    if len(frame.data) < ETHERNET_HEADER_LENGTH:
        return None
    (ethertype,) = struct.unpack_from(">H", frame.data, 12)
    return frame.data if ethertype == ETHERTYPE_COE else None


def compute_icrc(
    ip_header: bytes, udp_header: bytes, udp_payload_without_icrc: bytes
) -> int:
    """Computes the RoCEv2 invariant CRC the way the HSB Emulator does.

    The variant IP/UDP fields (TOS, TTL, IP checksum, UDP checksum) are replaced
    with ones and the CRC-32 is seeded with eight 0xFF bytes, per the InfiniBand
    RoCEv2 annex.

    Args:
        ip_header: IPv4 header as captured.
        udp_header: UDP header as captured.
        udp_payload_without_icrc: BTH onwards, excluding the 4-byte iCRC.

    Returns:
        The iCRC as an integer (stored little-endian on the wire).
    """
    masked_ip = bytearray(ip_header)
    masked_ip[1] = 0xFF
    masked_ip[8] = 0xFF
    masked_ip[10:12] = b"\xff\xff"
    masked_udp = bytearray(udp_header)
    masked_udp[6:8] = b"\xff\xff"
    crc = zlib.crc32(ICRC_SEED)
    crc = zlib.crc32(masked_ip, crc)
    crc = zlib.crc32(masked_udp, crc)
    return zlib.crc32(udp_payload_without_icrc, crc)


def icrc_matches(datagram: RoceDatagram) -> bool:
    """Checks a datagram's trailing iCRC.

    Args:
        datagram: RoCEv2 datagram.

    Returns:
        True when the transmitted iCRC equals the recomputed one.
    """
    body = datagram.udp_payload[:-ICRC_LENGTH]
    transmitted = int.from_bytes(datagram.udp_payload[-ICRC_LENGTH:], "little")
    return transmitted == compute_icrc(datagram.ip_header, datagram.udp_header, body)
