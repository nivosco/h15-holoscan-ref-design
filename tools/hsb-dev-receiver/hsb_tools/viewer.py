# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Microchip Technology Inc. and its subsidiaries.
"""Displays and saves received NV12 frames."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

STATUS_OK_BGR = (0, 200, 0)
STATUS_BAD_BGR = (0, 0, 255)
TEXT_SCALE = 0.8
TEXT_ORIGIN = (16, 40)
TEXT_THICKNESS = 2
# Saving costs milliseconds per frame; unbounded saving of failed frames on a lossy
# live link slows the receiver enough to cause more loss.
MAX_FAILED_SAVES = 10


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

    def handle(self, frame: bytes, frame_number: int, ok: bool) -> None:
        """Displays and/or saves one frame.

        Args:
            frame: NV12 bytes.
            frame_number: Frame number from the metadata.
            ok: Whether the frame passed verification.
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
        if save:
            stem = self.save_dir / f"frame_{frame_number:08d}_{status.lower()}"
            stem.with_suffix(".nv12").write_bytes(frame)
            self._cv2.imwrite(str(stem.with_suffix(".png")), image)
        if self.show:
            self._cv2.imshow("HSB dev receiver", image)
            self._cv2.waitKey(1)

    def close(self) -> None:
        """Closes the display window."""
        if self.show:
            self._cv2.destroyAllWindows()
