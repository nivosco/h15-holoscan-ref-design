# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Microchip Technology Inc. and its subsidiaries.
"""Displays and saves received NV12 frames, with detections drawn on them."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

from .detections import Detection, DetectionFrame

STATUS_OK_BGR = (0, 200, 0)
STATUS_BAD_BGR = (0, 0, 255)
TEXT_SCALE = 0.8
TEXT_ORIGIN = (16, 40)
TEXT_THICKNESS = 2
# Saving costs milliseconds per frame; unbounded saving of failed frames on a lossy
# live link slows the receiver enough to cause more loss.
MAX_FAILED_SAVES = 10
BOX_BGR = (255, 160, 0)
BOX_THICKNESS = 2
LABEL_SCALE = 0.6
LABEL_THICKNESS = 2
LABEL_OFFSET_PX = 6


def nv12_to_bgr(frame: bytes, width: int, height: int) -> np.ndarray:
    """Converts NV12 to BGR.

    Args:
        frame: NV12 bytes.
        width: Frame width.
        height: Frame height.

    Returns:
        HxWx3 uint8 BGR image.
    """
    import cv2

    nv12 = np.frombuffer(frame, dtype=np.uint8, count=width * height * 3 // 2)
    return cv2.cvtColor(nv12.reshape(height * 3 // 2, width), cv2.COLOR_YUV2BGR_NV12)


class FrameSink:
    """Optional live display and on-disk saving of frames."""

    def __init__(
        self,
        width: int,
        height: int,
        show: bool,
        save_dir: Path | None,
        save_every: int,
        save_failed: bool,
    ) -> None:
        """Creates the sink.

        Args:
            width: Frame width.
            height: Frame height.
            show: Open a display window.
            save_dir: Directory for saved frames, or None.
            save_every: Save one frame in this many.
            save_failed: Also save the first MAX_FAILED_SAVES failed frames.
        """
        self.width = width
        self.height = height
        self.save_dir = save_dir
        self.save_every = max(1, save_every)
        self.save_failed = save_failed
        self._frames = 0
        self._failed_saved = 0
        self._cv2 = None
        if show or save_dir:
            try:
                import cv2

                self._cv2 = cv2
            except ImportError:
                print("display/saving needs opencv-python; disabled", file=sys.stderr)
        self.show = show and self._cv2 is not None
        if self.save_dir:
            self.save_dir.mkdir(parents=True, exist_ok=True)

    @property
    def active(self) -> bool:
        """True when frames are displayed or saved."""
        return self._cv2 is not None and (self.show or self.save_dir is not None)

    def handle(
        self,
        frame: bytes,
        frame_number: int,
        ok: bool,
        detections: DetectionFrame | None = None,
    ) -> None:
        """Displays and/or saves one frame.

        Args:
            frame: NV12 bytes.
            frame_number: Frame number from the metadata.
            ok: Whether the frame passed verification.
            detections: Detections of this capture, drawn on the frame.
        """
        self._frames += 1
        save_failed = (
            self.save_failed and not ok and self._failed_saved < MAX_FAILED_SAVES
        )
        save = self.save_dir is not None and (
            self._frames % self.save_every == 1 or save_failed
        )
        if save and save_failed:
            self._failed_saved += 1
        if self._cv2 is None or not (self.show or save):
            return
        image = nv12_to_bgr(frame, self.width, self.height)
        status = "OK" if ok else "FAILED"
        self._cv2.putText(
            image,
            f"frame {frame_number} {status}",
            TEXT_ORIGIN,
            self._cv2.FONT_HERSHEY_SIMPLEX,
            TEXT_SCALE,
            STATUS_OK_BGR if ok else STATUS_BAD_BGR,
            TEXT_THICKNESS,
        )
        if detections is not None:
            self._draw_detections(image, detections)
        if save:
            stem = self.save_dir / f"frame_{frame_number:08d}_{status.lower()}"
            stem.with_suffix(".nv12").write_bytes(frame)
            self._cv2.imwrite(str(stem.with_suffix(".png")), image)
        if self.show:
            self._cv2.imshow("HSB dev receiver", image)
            self._cv2.waitKey(1)

    def _draw_detections(self, image: np.ndarray, frame: DetectionFrame) -> None:
        if not frame.frame_width or not frame.frame_height:
            return
        # Detection boxes are in pixels of the detection input stream, which shows
        # the same field of view as this stream at a different resolution.
        scale_x = self.width / frame.frame_width
        scale_y = self.height / frame.frame_height
        for detection in frame.detections:
            self._draw_detection(image, detection, scale_x, scale_y)

    def _draw_detection(
        self, image: np.ndarray, detection: Detection, scale_x: float, scale_y: float
    ) -> None:
        cv2 = self._cv2
        box = detection.bbox
        top_left = (round(box.xmin * scale_x), round(box.ymin * scale_y))
        bottom_right = (round(box.xmax * scale_x), round(box.ymax * scale_y))
        cv2.rectangle(image, top_left, bottom_right, BOX_BGR, BOX_THICKNESS)
        track = "" if detection.tracking_id is None else f" #{detection.tracking_id}"
        label = f"{detection.label}{track} {detection.confidence:.2f}"
        label_origin = (top_left[0], max(top_left[1] - LABEL_OFFSET_PX, TEXT_ORIGIN[1]))
        cv2.putText(
            image,
            label,
            label_origin,
            cv2.FONT_HERSHEY_SIMPLEX,
            LABEL_SCALE,
            BOX_BGR,
            LABEL_THICKNESS,
        )
        for child in detection.detections:
            self._draw_detection(image, child, scale_x, scale_y)

    def close(self) -> None:
        """Closes the display window."""
        if self.show:
            self._cv2.destroyAllWindows()
