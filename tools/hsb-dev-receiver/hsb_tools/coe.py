# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Microchip Technology Inc. and its subsidiaries.
"""IEEE 1722B Camera-over-Ethernet (COE) frames as emitted by the HSB Emulator 2.7."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from .protocol import METADATA_LENGTH, FrameMetadata, roundup

ETHERTYPE_COE = 0x22F0
ETHERNET_HEADER_LENGTH = 14
COE_HEADER_LENGTH = 46
AVTP_SUBTYPE_NTSCF = 0x82
ACF_MESSAGE_TYPE_BYTE = 0x0C
STREAM_ID = bytes.fromhex("00000000AAAAAAAA")
PSN_MODULO = 128
HEADER_FRAME_NUMBER_MODULO = 4
CHANNEL_MASK = 0x3F
ADDRESS_OFFSET_MASK = 0x0FFFFFFF
ADDRESS_FRAME_SHIFT = 28
ADDRESS_FRAME_MASK = 0x3

FLAG_FRAME_START = 0x01
FLAG_FRAME_END = 0x02
FLAG_LINE_END = 0x10

QP_ENABLE_1722B = 1 << 24
QP_LINE_SHIFT = 25

# dst(6) src(6) ethertype subtype sv_ver len_low seq stream_id(8) acf(2) reserved sensor
# ts_sec ts_nsec psn flags channel frame_flags address
COE_HEADER = struct.Struct(">6s6sHBBBB8sHBBIIBBBBI")
assert COE_HEADER.size == COE_HEADER_LENGTH


@dataclass(frozen=True)
class CoePacket:
    """One COE Ethernet frame."""

    destination: bytes
    source: bytes
    acf_message_type_byte: int
    stream_id: bytes
    sensor: int
    timestamp_ns: int
    psn: int
    channel: int
    frame_flags: int
    header_frame_number: int
    offset: int
    payload: memoryview

    @classmethod
    def parse(cls, frame: memoryview) -> CoePacket | None:
        """Parses an Ethernet frame.

        Args:
            frame: Ethernet frame without FCS.

        Returns:
            The packet, or None if it is not an HSB COE frame.
        """
        if len(frame) < COE_HEADER_LENGTH:
            return None
        (
            destination,
            source,
            ethertype,
            subtype,
            _,
            _,
            _,
            stream_id,
            acf,
            _,
            sensor,
            seconds,
            nanoseconds,
            psn,
            _,
            channel,
            frame_flags,
            address,
        ) = COE_HEADER.unpack_from(frame)
        if ethertype != ETHERTYPE_COE or subtype != AVTP_SUBTYPE_NTSCF:
            return None
        return cls(
            destination=destination,
            source=source,
            acf_message_type_byte=acf >> 8,
            stream_id=stream_id,
            sensor=sensor,
            timestamp_ns=seconds * 1_000_000_000 + nanoseconds,
            psn=psn,
            channel=channel & CHANNEL_MASK,
            frame_flags=frame_flags,
            header_frame_number=(address >> ADDRESS_FRAME_SHIFT) & ADDRESS_FRAME_MASK,
            offset=address & ADDRESS_OFFSET_MASK,
            payload=frame[COE_HEADER_LENGTH:],
        )


@dataclass
class CoeExpectations:
    """What the host configured for the COE stream."""

    frame_size: int
    payload_size: int
    channel: int
    destination_mac: bytes


@dataclass
class CoeFrame:
    """A COE frame closed by a FRAME_END packet."""

    data: bytes
    metadata: FrameMetadata
    packets: int
    bytes_received: int
    psn_gaps: int
    issues: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """True when every byte arrived and no protocol issue was found."""
        return self.bytes_received == self.metadata.bytes_written and not self.issues


class CoeAssembler:
    """Reassembles COE packets into frames with strict per-packet checks."""

    def __init__(self, expectations: CoeExpectations) -> None:
        """Allocates the frame buffer.

        Args:
            expectations: Configured stream parameters.
        """
        self.expectations = expectations
        self._metadata_offset = roundup(expectations.frame_size, 128)
        self._buffer = bytearray(self._metadata_offset + METADATA_LENGTH)
        self._view = memoryview(self._buffer)
        self._next_psn: int | None = None
        self._synchronized = False
        self._bytes_received = 0
        self._packets = 0
        self._psn_gaps = 0
        self._next_offset = 0
        self._issues: list[str] = []

    def _reset_frame(self) -> None:
        self._bytes_received = 0
        self._packets = 0
        self._psn_gaps = 0
        self._next_offset = 0
        self._issues = []

    def _issue(self, text: str) -> None:
        if len(self._issues) < 8:
            self._issues.append(text)

    def handle(self, packet: CoePacket) -> CoeFrame | None:
        """Processes one packet.

        Args:
            packet: Parsed COE packet.

        Returns:
            The frame this packet closed, or None.
        """
        is_end = bool(packet.frame_flags & FLAG_FRAME_END)
        if not self._synchronized:
            # Captures start mid-frame: skip until a packet starts a frame at offset 0.
            if is_end or packet.offset != 0:
                return None
            self._synchronized = True
        self._packets += 1
        self._check_header(packet)
        payload_length = len(packet.payload)

        if is_end:
            if payload_length < METADATA_LENGTH:
                self._issue(
                    f"psn {packet.psn}: FRAME_END payload shorter than metadata"
                )
                self._reset_frame()
                return None
            if (
                packet.offset + payload_length
                != self._metadata_offset + METADATA_LENGTH
            ):
                self._issue(
                    f"psn {packet.psn}: FRAME_END does not end at the metadata slot"
                )
            data_length = max(
                0, min(payload_length, self.expectations.frame_size - packet.offset)
            )
            data_length = min(data_length, payload_length - METADATA_LENGTH)
        else:
            data_length = payload_length
            if packet.offset + payload_length < self.expectations.frame_size and (
                payload_length != self.expectations.payload_size
            ):
                self._issue(
                    f"psn {packet.psn}: short middle packet ({payload_length} bytes)"
                )

        if data_length > 0:
            self._store(packet, data_length)
        if not is_end:
            return None

        metadata = FrameMetadata.parse(packet.payload, payload_length - METADATA_LENGTH)
        padding = packet.payload[data_length : payload_length - METADATA_LENGTH]
        if any(padding):
            self._issue("non-zero padding before metadata")
        self._check_frame_end(packet, metadata)
        frame = CoeFrame(
            data=bytes(self._view[: self.expectations.frame_size]),
            metadata=metadata,
            packets=self._packets,
            bytes_received=self._bytes_received,
            psn_gaps=self._psn_gaps,
            issues=self._issues,
        )
        self._reset_frame()
        return frame

    def _check_header(self, packet: CoePacket) -> None:
        if self._next_psn is not None and packet.psn != self._next_psn:
            self._psn_gaps += 1
            self._issue(f"psn {packet.psn}: expected {self._next_psn}")
        self._next_psn = (packet.psn + 1) % PSN_MODULO
        if packet.destination != self.expectations.destination_mac:
            self._issue(f"psn {packet.psn}: destination {packet.destination.hex(':')}")
        if packet.acf_message_type_byte != ACF_MESSAGE_TYPE_BYTE:
            self._issue(
                f"psn {packet.psn}: ACF byte 0x{packet.acf_message_type_byte:02x}"
            )
        if packet.channel != self.expectations.channel:
            self._issue(f"psn {packet.psn}: channel {packet.channel}")
        if packet.stream_id != STREAM_ID:
            self._issue(f"psn {packet.psn}: stream id {packet.stream_id.hex()}")
        if self._packets == 1:
            single_packet = bool(packet.frame_flags & FLAG_FRAME_END)
            if packet.offset != 0:
                self._issue(f"psn {packet.psn}: frame starts at offset {packet.offset}")
            if not single_packet and not packet.frame_flags & FLAG_FRAME_START:
                self._issue(f"psn {packet.psn}: first packet without FRAME_START")
        elif packet.frame_flags & FLAG_FRAME_START:
            self._issue(f"psn {packet.psn}: FRAME_START in the middle of a frame")

    def _store(self, packet: CoePacket, length: int) -> None:
        if packet.offset != self._next_offset:
            self._issue(
                f"psn {packet.psn}: offset {packet.offset}, "
                f"expected {self._next_offset}"
            )
        end = packet.offset + length
        if end > self.expectations.frame_size:
            self._issue(
                f"psn {packet.psn}: write [{packet.offset}, {end}) outside frame"
            )
            return
        self._view[packet.offset : end] = packet.payload[:length]
        self._bytes_received += length
        self._next_offset = end

    def _check_frame_end(self, packet: CoePacket, metadata: FrameMetadata) -> None:
        if metadata.psn != packet.psn:
            self._issue(f"metadata psn {metadata.psn} != packet psn {packet.psn}")
        if (
            packet.header_frame_number
            != metadata.frame_number % HEADER_FRAME_NUMBER_MODULO
        ):
            self._issue(
                f"header frame number {packet.header_frame_number} != "
                f"metadata frame number {metadata.frame_number} mod 4"
            )
        if metadata.bytes_written != self.expectations.frame_size:
            self._issue(
                f"bytes_written {metadata.bytes_written} "
                f"!= {self.expectations.frame_size}"
            )
        if self._bytes_received != metadata.bytes_written:
            self._issue(
                f"received {self._bytes_received} of {metadata.bytes_written} bytes"
            )
