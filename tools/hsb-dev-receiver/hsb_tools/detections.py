# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Microchip Technology Inc. and its subsidiaries.
"""Decodes the detection metadata the H15 sender publishes over ZMQ.

Each ZMQ message is one serialized ``hailo_analytics.Frame`` protobuf
(``analytics_metadata.proto`` in Hailo Media Library 1.13.0). Only the fields the
viewer uses are decoded; landmarks and unknown fields are skipped, so later schema
additions do not break decoding.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

WIRE_VARINT = 0
WIRE_FIXED64 = 1
WIRE_LENGTH_DELIMITED = 2
WIRE_FIXED32 = 5
FIXED64_SIZE = 8
FIXED32_SIZE = 4
VARINT_PAYLOAD_BITS = 7
VARINT_CONTINUATION = 0x80
VARINT_MAX_BITS = 64

# Field numbers from analytics_metadata.proto.
FRAME_ISP_TIMESTAMP = 1
FRAME_WIDTH = 2
FRAME_HEIGHT = 3
FRAME_DETECTIONS = 4
FRAME_CLASSIFICATIONS = 6
DETECTION_LABEL = 1
DETECTION_CONFIDENCE = 2
DETECTION_BBOX = 3
DETECTION_TRACKING_ID = 4
DETECTION_DETECTIONS = 5
DETECTION_CLASSIFICATIONS = 7
BBOX_XMIN, BBOX_YMIN, BBOX_XMAX, BBOX_YMAX = 1, 2, 3, 4
CLASSIFICATION_TYPE = 1
CLASSIFICATION_LABEL = 2
CLASSIFICATION_CONFIDENCE = 3


class MetadataDecodeError(ValueError):
    """The message is not a well-formed Frame."""


@dataclass(frozen=True)
class Bbox:
    """Axis-aligned box in pixels of the detection input frame."""

    xmin: float = 0.0
    ymin: float = 0.0
    xmax: float = 0.0
    ymax: float = 0.0


@dataclass(frozen=True)
class Classification:
    """A classification attached to a frame or detection."""

    type: str = ""
    label: str = ""
    confidence: float = 0.0


@dataclass(frozen=True)
class Detection:
    """One detected object."""

    label: str = ""
    confidence: float = 0.0
    bbox: Bbox = field(default_factory=Bbox)
    tracking_id: int | None = None
    detections: tuple[Detection, ...] = ()
    classifications: tuple[Classification, ...] = ()


@dataclass(frozen=True)
class DetectionFrame:
    """Detection results for one captured frame."""

    isp_timestamp_ns: int = 0
    frame_width: int = 0
    frame_height: int = 0
    detections: tuple[Detection, ...] = ()
    classifications: tuple[Classification, ...] = ()


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while shift < VARINT_MAX_BITS:
        if offset >= len(data):
            raise MetadataDecodeError("truncated varint")
        byte = data[offset]
        offset += 1
        value |= (byte & ~VARINT_CONTINUATION) << shift
        if not byte & VARINT_CONTINUATION:
            return value, offset
        shift += VARINT_PAYLOAD_BITS
    raise MetadataDecodeError("varint longer than 64 bits")


def _fields(data: bytes):
    """Yields (field number, wire type, value) for each field in a message.

    Varints yield ints, length-delimited fields yield bytes and fixed-size fields
    yield their raw little-endian bytes.
    """
    offset = 0
    while offset < len(data):
        key, offset = _read_varint(data, offset)
        number, wire_type = key >> 3, key & 0x7
        if wire_type == WIRE_VARINT:
            value, offset = _read_varint(data, offset)
        elif wire_type == WIRE_LENGTH_DELIMITED:
            length, offset = _read_varint(data, offset)
            value = data[offset : offset + length]
            offset += length
        elif wire_type in (WIRE_FIXED32, WIRE_FIXED64):
            size = FIXED32_SIZE if wire_type == WIRE_FIXED32 else FIXED64_SIZE
            value = data[offset : offset + size]
            offset += size
        else:
            raise MetadataDecodeError(f"unsupported wire type {wire_type}")
        if offset > len(data):
            raise MetadataDecodeError(f"field {number} runs past the message end")
        yield number, wire_type, value


def _float(wire_type: int, value: bytes) -> float:
    if wire_type != WIRE_FIXED32:
        raise MetadataDecodeError("expected a float field")
    return struct.unpack("<f", value)[0]


def _text(wire_type: int, value: bytes) -> str:
    if wire_type != WIRE_LENGTH_DELIMITED:
        raise MetadataDecodeError("expected a string field")
    return value.decode("utf-8", errors="replace")


def _bbox(data: bytes) -> Bbox:
    corners = {}
    for number, wire_type, value in _fields(data):
        if number in (BBOX_XMIN, BBOX_YMIN, BBOX_XMAX, BBOX_YMAX):
            corners[number] = _float(wire_type, value)
    return Bbox(
        corners.get(BBOX_XMIN, 0.0),
        corners.get(BBOX_YMIN, 0.0),
        corners.get(BBOX_XMAX, 0.0),
        corners.get(BBOX_YMAX, 0.0),
    )


def _classification(data: bytes) -> Classification:
    values: dict[str, object] = {}
    for number, wire_type, value in _fields(data):
        if number == CLASSIFICATION_TYPE:
            values["type"] = _text(wire_type, value)
        elif number == CLASSIFICATION_LABEL:
            values["label"] = _text(wire_type, value)
        elif number == CLASSIFICATION_CONFIDENCE:
            values["confidence"] = _float(wire_type, value)
    return Classification(**values)


def _detection(data: bytes) -> Detection:
    values: dict[str, object] = {}
    children: list[Detection] = []
    classifications: list[Classification] = []
    for number, wire_type, value in _fields(data):
        if number == DETECTION_LABEL:
            values["label"] = _text(wire_type, value)
        elif number == DETECTION_CONFIDENCE:
            values["confidence"] = _float(wire_type, value)
        elif number == DETECTION_BBOX:
            values["bbox"] = _bbox(value)
        elif number == DETECTION_TRACKING_ID and wire_type == WIRE_VARINT:
            values["tracking_id"] = value
        elif number == DETECTION_DETECTIONS:
            children.append(_detection(value))
        elif number == DETECTION_CLASSIFICATIONS:
            classifications.append(_classification(value))
    return Detection(
        **values, detections=tuple(children), classifications=tuple(classifications)
    )


def decode_frame(message: bytes) -> DetectionFrame:
    """Decodes one ZMQ metadata message.

    Args:
        message: A serialized ``hailo_analytics.Frame``.

    Returns:
        The decoded frame.

    Raises:
        MetadataDecodeError: The message is malformed.
    """
    values: dict[str, int] = {}
    detections: list[Detection] = []
    classifications: list[Classification] = []
    for number, wire_type, value in _fields(message):
        if wire_type == WIRE_VARINT and number == FRAME_ISP_TIMESTAMP:
            values["isp_timestamp_ns"] = value
        elif wire_type == WIRE_VARINT and number == FRAME_WIDTH:
            values["frame_width"] = value
        elif wire_type == WIRE_VARINT and number == FRAME_HEIGHT:
            values["frame_height"] = value
        elif number == FRAME_DETECTIONS:
            detections.append(_detection(value))
        elif number == FRAME_CLASSIFICATIONS:
            classifications.append(_classification(value))
    return DetectionFrame(
        **values,
        detections=tuple(detections),
        classifications=tuple(classifications),
    )
