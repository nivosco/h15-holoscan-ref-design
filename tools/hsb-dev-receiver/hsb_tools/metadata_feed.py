# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Microchip Technology Inc. and its subsidiaries.
"""Receives detection metadata and joins it to HSB frames by ISP timestamp.

The sender stamps every HSB frame with the ISP timestamp of the capture, and the
detection metadata of that capture carries the same timestamp. Frames with no
detections publish no metadata, so a frame without metadata is not an error.
"""

from __future__ import annotations

import bisect
import queue
import threading
from collections import OrderedDict
from dataclasses import dataclass, field

from .detections import Detection, DetectionFrame, MetadataDecodeError, decode_frame

MAX_RECORDED_ISSUES = 50
# Frames and metadata kept for joining; about four seconds at 60 fps.
JOIN_WINDOW_FRAMES = 240
RECEIVE_TIMEOUT_MS = 200
# Boxes are allowed to touch the frame edge with a little float slack.
BBOX_TOLERANCE_PX = 1.0
NANOSECONDS_PER_MILLISECOND = 1_000_000


@dataclass
class MetadataReport:
    """Accumulated metadata and join results."""

    messages: int = 0
    decode_errors: int = 0
    detections: int = 0
    detections_without_track: int = 0
    bbox_out_of_bounds: int = 0
    confidence_out_of_range: int = 0
    frame_size_changes: int = 0
    timestamp_regressions: int = 0
    duplicate_timestamps: int = 0
    joined: int = 0
    frame_not_received: int = 0
    between_consecutive_frames: int = 0
    track_ids: set[int] = field(default_factory=set)
    issues: list[str] = field(default_factory=list)

    def record(self, text: str) -> None:
        """Keeps the first issues for the final report.

        Args:
            text: Issue description.
        """
        if len(self.issues) < MAX_RECORDED_ISSUES:
            self.issues.append(text)

    @property
    def passed(self) -> bool:
        """True when metadata arrived and was consistent with itself and the video.

        Metadata for a frame the receiver lost is tolerated; metadata whose timestamp
        falls between two consecutive received frames matches no capture and fails.
        """
        return (
            self.messages > 0
            and self.decode_errors == 0
            and self.bbox_out_of_bounds == 0
            and self.confidence_out_of_range == 0
            and self.frame_size_changes == 0
            and self.timestamp_regressions == 0
            and self.duplicate_timestamps == 0
            and self.between_consecutive_frames == 0
        )

    def summary(self) -> dict[str, object]:
        """Returns the report as JSON-friendly values.

        Returns:
            Summary dictionary.
        """
        resolved = self.joined + self.frame_not_received
        return {
            "messages": self.messages,
            "decode_errors": self.decode_errors,
            "detections": self.detections,
            "detections_without_track": self.detections_without_track,
            "unique_track_ids": len(self.track_ids),
            "bbox_out_of_bounds": self.bbox_out_of_bounds,
            "confidence_out_of_range": self.confidence_out_of_range,
            "frame_size_changes": self.frame_size_changes,
            "timestamp_regressions": self.timestamp_regressions,
            "duplicate_timestamps": self.duplicate_timestamps,
            "joined": self.joined,
            "frame_not_received": self.frame_not_received,
            "between_consecutive_frames": self.between_consecutive_frames,
            "join_percent": round(100.0 * self.joined / resolved, 2)
            if resolved
            else None,
            "passed": self.passed,
            "issues": self.issues,
        }


def _iter_detections(detections: tuple[Detection, ...]):
    for detection in detections:
        yield detection
        yield from _iter_detections(detection.detections)


class MetadataJoiner:
    """Validates metadata and matches it to received frames.

    Not thread-safe: call it from the thread that receives frames.
    """

    def __init__(self) -> None:
        """Creates an empty joiner."""
        self.report = MetadataReport()
        self._frame_numbers: OrderedDict[int, int] = OrderedDict()
        self._metadata: OrderedDict[int, DetectionFrame] = OrderedDict()
        self._pending: list[int] = []
        self._frame_size: tuple[int, int] | None = None
        self._last_timestamp_ns: int | None = None

    def add_frame(self, timestamp_ns: int, frame_number: int) -> None:
        """Records a received HSB frame.

        Args:
            timestamp_ns: The frame's ISP timestamp.
            frame_number: The frame's HSB frame number.
        """
        self._frame_numbers[timestamp_ns] = frame_number
        while len(self._frame_numbers) > JOIN_WINDOW_FRAMES:
            self._frame_numbers.popitem(last=False)
        self._resolve(final=False)

    def add_message(self, message: bytes) -> None:
        """Decodes and validates one metadata message.

        Args:
            message: Raw ZMQ message.
        """
        report = self.report
        report.messages += 1
        try:
            frame = decode_frame(message)
        except MetadataDecodeError as error:
            report.decode_errors += 1
            report.record(f"message {report.messages}: {error}")
            return
        self._validate(frame)
        self._metadata[frame.isp_timestamp_ns] = frame
        while len(self._metadata) > JOIN_WINDOW_FRAMES:
            self._metadata.popitem(last=False)
        self._pending.append(frame.isp_timestamp_ns)
        self._resolve(final=False)

    def finish(self) -> None:
        """Resolves metadata still waiting for a later frame."""
        self._resolve(final=True)

    def detections_at(self, timestamp_ns: int) -> DetectionFrame | None:
        """Returns the metadata of exactly this capture, if any.

        Args:
            timestamp_ns: ISP timestamp of a frame.

        Returns:
            The detections for that capture, or None when none were published.
        """
        return self._metadata.get(timestamp_ns)

    def _validate(self, frame: DetectionFrame) -> None:
        report = self.report
        timestamp = frame.isp_timestamp_ns
        if self._last_timestamp_ns is not None:
            if timestamp == self._last_timestamp_ns:
                report.duplicate_timestamps += 1
                report.record(f"metadata {timestamp}: duplicate timestamp")
            elif timestamp < self._last_timestamp_ns:
                report.timestamp_regressions += 1
                report.record(f"metadata {timestamp}: timestamp went back")
        self._last_timestamp_ns = timestamp

        size = (frame.frame_width, frame.frame_height)
        if self._frame_size is not None and size != self._frame_size:
            report.frame_size_changes += 1
            report.record(f"metadata {timestamp}: frame size changed to {size}")
        self._frame_size = size

        for detection in _iter_detections(frame.detections):
            self._validate_detection(timestamp, detection, size)

    def _validate_detection(
        self, timestamp: int, detection: Detection, size: tuple[int, int]
    ) -> None:
        report = self.report
        report.detections += 1
        if detection.tracking_id is None:
            report.detections_without_track += 1
        else:
            report.track_ids.add(detection.tracking_id)
        if not 0.0 <= detection.confidence <= 1.0:
            report.confidence_out_of_range += 1
            report.record(f"metadata {timestamp}: confidence {detection.confidence}")
        box = detection.bbox
        width, height = size
        inside = (
            -BBOX_TOLERANCE_PX <= box.xmin <= box.xmax <= width + BBOX_TOLERANCE_PX
            and -BBOX_TOLERANCE_PX <= box.ymin <= box.ymax <= height + BBOX_TOLERANCE_PX
        )
        if not inside:
            report.bbox_out_of_bounds += 1
            report.record(f"metadata {timestamp}: box {box} outside {width}x{height}")

    def _resolve(self, final: bool) -> None:
        if not self._pending:
            return
        received = sorted(self._frame_numbers)
        newest_frame = received[-1] if received else None
        still_pending = []
        for timestamp in self._pending:
            if timestamp in self._frame_numbers:
                self.report.joined += 1
            elif not final and (newest_frame is None or timestamp > newest_frame):
                # Metadata can overtake its frame; wait for a later frame.
                still_pending.append(timestamp)
            else:
                self._classify_unjoined(timestamp, received)
        self._pending = still_pending

    def _classify_unjoined(self, timestamp: int, received: list[int]) -> None:
        index = bisect.bisect_left(received, timestamp)
        if 0 < index < len(received):
            before = self._frame_numbers[received[index - 1]]
            after = self._frame_numbers[received[index]]
            if (after - before) & 0xFFFFFFFF == 1:
                self.report.between_consecutive_frames += 1
                self.report.record(
                    f"metadata {timestamp}: between consecutive frames "
                    f"{before} and {after}"
                )
                return
        self.report.frame_not_received += 1


class MetadataSubscriber:
    """Receives metadata messages on a background thread."""

    def __init__(self, address: str) -> None:
        """Connects to the sender's ZMQ publisher.

        Args:
            address: ZMQ endpoint, for example ``tcp://10.0.0.1:7000``.

        Raises:
            ImportError: pyzmq is not installed.
        """
        import zmq

        self._zmq = zmq
        self._context = zmq.Context.instance()
        self._socket = self._context.socket(zmq.SUB)
        self._socket.setsockopt(zmq.SUBSCRIBE, b"")
        self._socket.setsockopt(zmq.RCVTIMEO, RECEIVE_TIMEOUT_MS)
        self._socket.connect(address)
        self._messages: queue.SimpleQueue[bytes] = queue.SimpleQueue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._messages.put(self._socket.recv())
            except self._zmq.Again:
                continue

    def drain(self) -> list[bytes]:
        """Returns the messages received since the last call.

        Returns:
            Raw messages in arrival order.
        """
        messages = []
        while True:
            try:
                messages.append(self._messages.get_nowait())
            except queue.Empty:
                return messages

    def close(self) -> None:
        """Stops the receive thread and closes the socket."""
        self._stop.set()
        self._thread.join()
        self._socket.close()
