# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Microchip Technology Inc. and its subsidiaries.
"""Minimal protobuf encoder for hailo_analytics.Frame test messages."""

from __future__ import annotations

import struct

from hsb_tools.detections import Bbox, Detection, DetectionFrame


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _key(number: int, wire_type: int) -> bytes:
    return _varint(number << 3 | wire_type)


def _uint(number: int, value: int) -> bytes:
    return _key(number, 0) + _varint(value)


def _float(number: int, value: float) -> bytes:
    return _key(number, 5) + struct.pack("<f", value)


def _bytes(number: int, value: bytes) -> bytes:
    return _key(number, 2) + _varint(len(value)) + value


def encode_bbox(box: Bbox) -> bytes:
    return (
        _float(1, box.xmin)
        + _float(2, box.ymin)
        + _float(3, box.xmax)
        + _float(4, box.ymax)
    )


def encode_detection(detection: Detection) -> bytes:
    out = _bytes(1, detection.label.encode())
    out += _float(2, detection.confidence)
    out += _bytes(3, encode_bbox(detection.bbox))
    if detection.tracking_id is not None:
        out += _uint(4, detection.tracking_id)
    for child in detection.detections:
        out += _bytes(5, encode_detection(child))
    return out


def encode_frame(frame: DetectionFrame) -> bytes:
    out = _uint(1, frame.isp_timestamp_ns)
    out += _uint(2, frame.frame_width)
    out += _uint(3, frame.frame_height)
    for detection in frame.detections:
        out += _bytes(4, encode_detection(detection))
    return out
