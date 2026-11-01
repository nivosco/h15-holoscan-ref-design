# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Microchip Technology Inc. and its subsidiaries.
"""Python model of the HSB Emulator 2.7 transmitters, for tests.

Mirrors rocev2_transmitter.cpp / coe_transmitter.cpp segmentation: data packets of up to
payload_size bytes, then the 128-byte FrameMetadata either embedded after zero padding in
the last data packet or sent in its own frame-end packet.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from hsb_tools.coe import (
    ACF_MESSAGE_TYPE_BYTE,
    AVTP_SUBTYPE_NTSCF,
    COE_HEADER,
    ETHERTYPE_COE,
    FLAG_FRAME_END,
    FLAG_FRAME_START,
    STREAM_ID,
)
from hsb_tools.pcap import compute_icrc
from hsb_tools.protocol import (
    BTH_RETH,
    HSB_SOURCE_UDP_PORT,
    IMMEDIATE,
    METADATA_LENGTH,
    OPCODE_WRITE_ONLY,
    OPCODE_WRITE_ONLY_IMMEDIATE,
    FrameMetadata,
    roundup,
)

ICRC_SIZE = 4
ROCE_PSN_MODULO = 1 << 20
COE_PSN_MODULO = 128
FRAME_PERIOD_NS = 16_666_667


def _segments(frame_size: int, payload_size: int) -> tuple[list[tuple[int, int]], bool]:
    """Splits a frame into (offset, length) chunks and decides metadata embedding."""
    chunks = []
    offset = 0
    while offset < frame_size:
        length = min(payload_size, frame_size - offset)
        chunks.append((offset, length))
        offset += length
    last_length = chunks[-1][1]
    zeros = roundup(frame_size, 128) - frame_size
    embedded = (
        payload_size - last_length - METADATA_LENGTH - ICRC_SIZE - zeros
        >= METADATA_LENGTH
    )
    return chunks, embedded and zeros <= METADATA_LENGTH


@dataclass
class RoceEmulatorModel:
    """Generates RoCEv2 UDP payloads for consecutive frames."""

    payload_size: int = 1408
    qp: int = 0xCAF0
    rkey: int = 0xBEEF
    psn: int = 0
    frame_number: int = 0
    timestamp_ns: int = 1_700_000_000_000_000_000
    _frame_size: int = 0

    def frame_packets(self, frame: bytes) -> list[bytes]:
        """Returns the UDP payloads (with iCRC placeholder) for one frame."""
        self._frame_size = len(frame)
        chunks, embedded = _segments(len(frame), self.payload_size)
        metadata_offset = roundup(len(frame), 128)
        packets = []
        for index, (offset, length) in enumerate(chunks):
            is_last = index == len(chunks) - 1
            data = frame[offset : offset + length]
            if is_last and embedded:
                padding = bytes(metadata_offset - (offset + length))
                packets.append(self._packet(offset, data + padding, with_metadata=True))
            else:
                packets.append(self._packet(offset, data, with_metadata=False))
        if not embedded:
            packets.append(self._packet(metadata_offset, b"", with_metadata=True))
        self.frame_number += 1
        self.timestamp_ns += FRAME_PERIOD_NS
        return packets

    def _packet(self, offset: int, data: bytes, with_metadata: bool) -> bytes:
        psn = self.psn
        self.psn = (self.psn + 1) % ROCE_PSN_MODULO
        opcode = OPCODE_WRITE_ONLY_IMMEDIATE if with_metadata else OPCODE_WRITE_ONLY
        body = data
        if with_metadata:
            body += FrameMetadata(
                flags=0,
                psn=psn,
                crc=0,
                timestamp_ns=self.timestamp_ns,
                bytes_written=self._frame_size,
                frame_number=self.frame_number,
                metadata_timestamp_ns=self.timestamp_ns + 1000,
            ).pack()
        header = BTH_RETH.pack(
            opcode, 0, 0xFFFF, 0xFF000000 | self.qp, psn, offset, self.rkey, len(body)
        )
        immediate = IMMEDIATE.pack((psn & 0xFFFFF) << 12) if with_metadata else b""
        return header + immediate + body + bytes(ICRC_SIZE)

    def stream(self, frames: list[bytes]) -> list[bytes]:
        """Returns the UDP payloads for several frames."""
        packets = []
        for frame in frames:
            packets.extend(self.frame_packets(frame))
        return packets


def ipv4_udp_ethernet(udp_payload_with_icrc: bytes, destination_port: int) -> bytes:
    """Wraps a RoCEv2 UDP payload into Ethernet/IPv4/UDP with a valid iCRC."""
    total_length = 20 + 8 + len(udp_payload_with_icrc)
    ip_header = struct.pack(
        ">BBHHHBBH4s4s",
        0x45,
        0,
        total_length,
        0x1234,
        0x4000,
        64,
        17,
        0,
        bytes([10, 0, 0, 1]),
        bytes([10, 0, 0, 2]),
    )
    udp_header = struct.pack(
        ">HHHH",
        HSB_SOURCE_UDP_PORT,
        destination_port,
        8 + len(udp_payload_with_icrc),
        0,
    )
    body = udp_payload_with_icrc[:-ICRC_SIZE]
    icrc = compute_icrc(ip_header, udp_header, body).to_bytes(4, "little")
    ethernet = (
        bytes.fromhex("02005e100001") + bytes.fromhex("02005e100002") + b"\x08\x00"
    )
    return ethernet + ip_header + udp_header + body + icrc


@dataclass
class CoeEmulatorModel:
    """Generates COE Ethernet frames for consecutive frames."""

    payload_size: int = 1408
    destination: bytes = bytes.fromhex("02005e100001")
    source: bytes = bytes.fromhex("02005e100002")
    channel: int = 0
    psn: int = 0
    frame_number: int = 0
    timestamp_ns: int = 1_700_000_000_000_000_000

    def frame_packets(self, frame: bytes) -> list[bytes]:
        """Returns the Ethernet frames for one frame."""
        chunks, embedded = _segments(len(frame), self.payload_size)
        metadata_offset = roundup(len(frame), 128)
        packets = []
        for index, (offset, length) in enumerate(chunks):
            is_last = index == len(chunks) - 1
            data = frame[offset : offset + length]
            if is_last and embedded:
                padding = bytes(metadata_offset - (offset + length))
                packets.append(
                    self._packet(offset, data + padding, len(frame), end=True)
                )
            else:
                packets.append(self._packet(offset, data, len(frame), end=False))
        if not embedded:
            packets.append(self._packet(metadata_offset, b"", len(frame), end=True))
        self.frame_number += 1
        self.timestamp_ns += FRAME_PERIOD_NS
        return packets

    def _packet(self, offset: int, data: bytes, frame_size: int, end: bool) -> bytes:
        psn = self.psn
        self.psn = (self.psn + 1) % COE_PSN_MODULO
        flags = FLAG_FRAME_END if end else (FLAG_FRAME_START if offset == 0 else 0)
        body = data
        if end:
            body += FrameMetadata(
                flags=0,
                psn=psn,
                crc=0,
                timestamp_ns=self.timestamp_ns,
                bytes_written=frame_size,
                frame_number=self.frame_number,
                metadata_timestamp_ns=self.timestamp_ns + 1000,
            ).pack()
        seconds, nanoseconds = divmod(self.timestamp_ns, 1_000_000_000)
        address = ((self.frame_number & 3) << 28) | offset
        header = COE_HEADER.pack(
            self.destination,
            self.source,
            ETHERTYPE_COE,
            AVTP_SUBTYPE_NTSCF,
            0x80,
            0,
            0,
            STREAM_ID,
            ACF_MESSAGE_TYPE_BYTE << 8,
            0x20,
            0,
            seconds & 0xFFFFFFFF,
            nanoseconds,
            psn,
            0,
            self.channel,
            flags,
            address,
        )
        return header + body

    def stream(self, frames: list[bytes]) -> list[bytes]:
        """Returns the Ethernet frames for several frames."""
        packets = []
        for frame in frames:
            packets.extend(self.frame_packets(frame))
        return packets


def write_pcap(path, frames: list[bytes]) -> None:
    """Writes Ethernet frames to a classic (microsecond) pcap file."""
    header = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)
    records = []
    for index, frame in enumerate(frames):
        records.append(
            struct.pack("<IIII", 1_700_000_000, index, len(frame), len(frame)) + frame
        )
    path.write_bytes(header + b"".join(records))
