# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Microchip Technology Inc. and its subsidiaries.
"""HSB wire formats as emitted by the HSB Emulator 2.7 (RoCEv2 data plane)."""

from __future__ import annotations

import struct
from dataclasses import dataclass

BOOTP_REQUEST_PORT = 12267
CONTROL_PORT = 8192
HSB_SOURCE_UDP_PORT = 12288
BOOTP_PACKET_SIZE = 300
BOOTP_CHADDR_OFFSET = 28
MAC_LENGTH = 6

CMD_WR_DWORD = 0x04
CMD_RD_DWORD = 0x14
REPLY_FLAG = 0x80
FLAG_ACK_REQUEST = 0x01
STATUS_SUCCESS = 0x00
CONTROL_HEADER = struct.Struct(">BBHBB")

# Data plane 0 / sensor 0 registers (vp_address 0x1000, hif_address 0x02000300).
DP_QP = 0x00001000
DP_RKEY = 0x00001004
DP_PAGE_LSB = 0x00001008
DP_PAGE_MSB = 0x0000100C
DP_PAGE_INC = 0x00001010
DP_MAX_BUFF = 0x00001014
DP_BUFFER_LENGTH = 0x00001018
DP_HOST_MAC_LOW = 0x00001020
DP_HOST_MAC_HIGH = 0x00001024
DP_HOST_IP = 0x00001028
DP_HOST_UDP_PORT = 0x0000102C
DP_PACKET_SIZE = 0x02000304
DP_PACKET_UDP_PORT = 0x02000308
DP_VP_MASK = 0x0200030C
VP_MASK_SENSOR_0 = 0x1

ALIGNMENT = 128
ROCE_HEADER_OVERHEAD = 74
DEFAULT_MTU = 1500
DEFAULT_QP = 0xCAF0
DEFAULT_RKEY = 0xBEEF

OPCODE_WRITE_ONLY = 0x2A
OPCODE_WRITE_ONLY_IMMEDIATE = 0x2B
BTH_RETH = struct.Struct(
    ">BBHIIQII"
)  # opcode flags pkey resv+qp resv+psn va rkey dmalen
BTH_RETH_LENGTH = BTH_RETH.size
IMMEDIATE = struct.Struct(">I")
IMMEDIATE_LENGTH = IMMEDIATE.size
ICRC_LENGTH = 4
PSN_MODULO = 1 << 20
QP_MASK = 0xFFFFFF
PSN_MASK = 0xFFFFFF
IMMEDIATE_PAGE_MASK = 0xFFF

METADATA_LENGTH = 128
METADATA = struct.Struct(">IIIIIIIIIIII")


def roundup(value: int, alignment: int) -> int:
    """Rounds value up to a multiple of alignment.

    Args:
        value: Value to round.
        alignment: Alignment, must be positive.

    Returns:
        The smallest multiple of alignment that is >= value.
    """
    return (value + alignment - 1) // alignment * alignment


def payload_size_for_mtu(mtu: int) -> int:
    """Returns the RoCEv2 payload bytes per packet NVIDIA's host programs for an MTU.

    Args:
        mtu: Link MTU in bytes.

    Returns:
        Payload size in bytes, a multiple of 128.
    """
    return (mtu - ROCE_HEADER_OVERHEAD) // ALIGNMENT * ALIGNMENT


@dataclass(frozen=True)
class FrameMetadata:
    """The 128-byte frame-end metadata block (big-endian on the wire)."""

    flags: int
    psn: int
    crc: int
    timestamp_ns: int
    bytes_written: int
    frame_number: int
    metadata_timestamp_ns: int

    @classmethod
    def parse(cls, data: bytes | memoryview, offset: int = 0) -> FrameMetadata:
        """Parses a metadata block.

        Args:
            data: Buffer holding the block.
            offset: Offset of the block in data.

        Returns:
            The parsed metadata.
        """
        (
            flags,
            psn,
            crc,
            ts_s_high,
            ts_s_low,
            ts_ns,
            written_high,
            written_low,
            frame_number,
            meta_s_high,
            meta_s_low,
            meta_ns,
        ) = METADATA.unpack_from(data, offset)
        return cls(
            flags=flags,
            psn=psn,
            crc=crc,
            timestamp_ns=((ts_s_high << 32) | ts_s_low) * 1_000_000_000 + ts_ns,
            bytes_written=(written_high << 32) | written_low,
            frame_number=frame_number,
            metadata_timestamp_ns=((meta_s_high << 32) | meta_s_low) * 1_000_000_000
            + meta_ns,
        )

    def pack(self) -> bytes:
        """Serializes the block as the emulator does (used by tests).

        Returns:
            128 bytes.
        """
        seconds, nanoseconds = divmod(self.timestamp_ns, 1_000_000_000)
        meta_seconds, meta_nanoseconds = divmod(
            self.metadata_timestamp_ns, 1_000_000_000
        )
        fields = METADATA.pack(
            self.flags,
            self.psn,
            self.crc,
            seconds >> 32,
            seconds & 0xFFFFFFFF,
            nanoseconds,
            self.bytes_written >> 32,
            self.bytes_written & 0xFFFFFFFF,
            self.frame_number,
            meta_seconds >> 32,
            meta_seconds & 0xFFFFFFFF,
            meta_nanoseconds,
        )
        return fields + bytes(METADATA_LENGTH - len(fields))


@dataclass(frozen=True)
class RocePacket:
    """One RoCEv2 RDMA WRITE (ONLY or ONLY with Immediate) UDP payload."""

    opcode: int
    qp: int
    psn: int
    virtual_address: int
    rkey: int
    dma_length: int
    immediate: int | None
    payload: memoryview

    @classmethod
    def parse(cls, data: memoryview) -> RocePacket | None:
        """Parses a UDP payload.

        Args:
            data: UDP payload including the trailing iCRC.

        Returns:
            The packet, or None if it is not a supported RDMA WRITE.
        """
        if len(data) < BTH_RETH_LENGTH + ICRC_LENGTH:
            return None
        opcode, _, _, qp_field, psn_field, virtual_address, rkey, dma_length = (
            BTH_RETH.unpack_from(data)
        )
        offset = BTH_RETH_LENGTH
        immediate = None
        if opcode == OPCODE_WRITE_ONLY_IMMEDIATE:
            (immediate,) = IMMEDIATE.unpack_from(data, offset)
            offset += IMMEDIATE_LENGTH
        elif opcode != OPCODE_WRITE_ONLY:
            return None
        if offset + dma_length + ICRC_LENGTH > len(data):
            return None
        return cls(
            opcode=opcode,
            qp=qp_field & QP_MASK,
            psn=psn_field & PSN_MASK,
            virtual_address=virtual_address,
            rkey=rkey,
            dma_length=dma_length,
            immediate=immediate,
            payload=data[offset : offset + dma_length],
        )

    @property
    def is_frame_end(self) -> bool:
        """True for the WRITE-with-Immediate packet that carries the metadata."""
        return self.opcode == OPCODE_WRITE_ONLY_IMMEDIATE
