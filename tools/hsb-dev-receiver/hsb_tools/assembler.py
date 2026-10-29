# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Microchip Technology Inc. and its subsidiaries.
"""Reassembles RoCEv2 RDMA WRITE packets into frames and checks per-packet context."""

from __future__ import annotations

from dataclasses import dataclass, field

from .protocol import (
    IMMEDIATE_PAGE_MASK,
    METADATA_LENGTH,
    PSN_MODULO,
    FrameMetadata,
    RocePacket,
    roundup,
)

MAX_ISSUES_PER_FRAME = 8


@dataclass
class StreamExpectations:
    """What the host configured, used to validate every packet."""

    frame_size: int
    payload_size: int
    qp: int
    rkey: int
    page_base: int = 0
    page_increment: int = 0


@dataclass
class AssembledFrame:
    """A frame closed by a metadata packet."""

    data: bytes
    metadata: FrameMetadata
    packets: int
    bytes_received: int
    psn_gaps: int
    issues: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """True when every byte the sender reported arrived exactly once."""
        return self.bytes_received == self.metadata.bytes_written and not self.issues


class FrameAssembler:
    """Turns RoCEv2 packets into frames, recording any protocol inconsistency."""

    def __init__(self, expectations: StreamExpectations) -> None:
        """Allocates the frame buffer.

        Args:
            expectations: Configured stream parameters.
        """
        self.expectations = expectations
        self._buffer = bytearray(
            roundup(expectations.frame_size, 128) + METADATA_LENGTH
        )
        self._view = memoryview(self._buffer)
        self._next_psn: int | None = None
        self._synchronized = False
        self._reset_frame()

    def _reset_frame(self) -> None:
        self._bytes_received = 0
        self._packets = 0
        self._psn_gaps = 0
        self._next_offset = 0
        self._issues: list[str] = []

    def _issue(self, text: str) -> None:
        if len(self._issues) < MAX_ISSUES_PER_FRAME:
            self._issues.append(text)

    def handle(self, packet: RocePacket) -> AssembledFrame | None:
        """Processes one packet.

        Args:
            packet: Parsed RoCEv2 packet.

        Returns:
            The frame this packet closed, or None.
        """
        if not self._synchronized:
            # Captures and late joins start mid-frame: skip until a frame starts.
            starts_frame = packet.virtual_address == self._page_base(packet)
            if packet.is_frame_end or not starts_frame:
                self._next_psn = None
                return None
            self._synchronized = True
        self._packets += 1
        self._check_header(packet)

        data_length = packet.dma_length
        if packet.is_frame_end:
            data_length -= METADATA_LENGTH
            if data_length < 0:
                self._issue(f"psn {packet.psn}: frame-end packet shorter than metadata")
                self._reset_frame()
                return None
        if data_length > 0:
            self._store(packet, data_length)
        if not packet.is_frame_end:
            return None

        metadata = FrameMetadata.parse(packet.payload, data_length)
        self._check_frame_end(packet, metadata, data_length)
        frame = AssembledFrame(
            data=bytes(self._view[: self.expectations.frame_size]),
            metadata=metadata,
            packets=self._packets,
            bytes_received=self._bytes_received,
            psn_gaps=self._psn_gaps,
            issues=self._issues,
        )
        self._reset_frame()
        return frame

    def _check_header(self, packet: RocePacket) -> None:
        if self._next_psn is not None and packet.psn != self._next_psn:
            self._psn_gaps += 1
            self._issue(f"psn {packet.psn}: expected {self._next_psn}")
        self._next_psn = (packet.psn + 1) % PSN_MODULO
        if packet.qp != self.expectations.qp:
            self._issue(
                f"psn {packet.psn}: qp 0x{packet.qp:x} != 0x{self.expectations.qp:x}"
            )
        if packet.rkey != self.expectations.rkey:
            self._issue(
                f"psn {packet.psn}: rkey 0x{packet.rkey:x} "
                f"!= 0x{self.expectations.rkey:x}"
            )
        if packet.dma_length > self.expectations.payload_size:
            self._issue(f"psn {packet.psn}: dmalen {packet.dma_length} > payload size")

    def _page_base(self, packet: RocePacket) -> int:
        page = (packet.immediate or 0) & IMMEDIATE_PAGE_MASK
        return self.expectations.page_base + page * self.expectations.page_increment

    def _store(self, packet: RocePacket, length: int) -> None:
        offset = packet.virtual_address - self.expectations.page_base
        if offset != self._next_offset:
            self._issue(
                f"psn {packet.psn}: write at offset {offset}, "
                f"expected {self._next_offset}"
            )
        end = offset + length
        if offset < 0 or end > self.expectations.frame_size:
            self._issue(f"psn {packet.psn}: write [{offset}, {end}) outside frame")
            return
        self._view[offset:end] = packet.payload[:length]
        self._bytes_received += length
        self._next_offset = end

    def _check_frame_end(
        self, packet: RocePacket, metadata: FrameMetadata, data_length: int
    ) -> None:
        # The metadata is always the last 128 bytes of the frame-end payload and must
        # land at page_base + roundup(frame_size, 128), embedded or sent alone.
        metadata_address = packet.virtual_address + packet.dma_length - METADATA_LENGTH
        expected_address = self._page_base(packet) + roundup(
            self.expectations.frame_size, 128
        )
        if metadata_address != expected_address:
            self._issue(
                f"metadata at 0x{metadata_address:x}, expected 0x{expected_address:x}"
            )
        padding = packet.payload[data_length : packet.dma_length - METADATA_LENGTH]
        if any(padding):
            self._issue("non-zero padding before metadata")
        if metadata.psn != packet.psn:
            self._issue(f"metadata psn {metadata.psn} != packet psn {packet.psn}")
        if metadata.bytes_written != self.expectations.frame_size:
            self._issue(
                f"bytes_written {metadata.bytes_written} "
                f"!= {self.expectations.frame_size}"
            )
        if self._bytes_received != metadata.bytes_written:
            self._issue(
                f"received {self._bytes_received} of {metadata.bytes_written} bytes"
            )
