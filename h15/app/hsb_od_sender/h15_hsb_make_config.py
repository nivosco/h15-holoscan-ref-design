#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Microchip Technology Inc. and its subsidiaries.
"""Builds the h15-hsb-sender media library configuration for this board.

Starts from Hailo's stock daylight profile for the detected SoC and sensor, and
changes only what the sender needs:

* the sensor runs at the HSB frame rate, and auto exposure keeps it there;
* frontend stream ``sink0`` is the raw NV12 stream sent over HSB;
* frontend stream ``sink1`` is the detection input;
* no frontend stream is encoded.

The stock files are not modified; the generated configuration is written to
--output-dir and every path inside it points into that directory.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

IMAGING_ROOT = Path("/etc/imaging/cfg")
SOC_MACHINE_FILE = Path("/sys/devices/soc0/machine")
I2C_DRIVERS = Path("/sys/bus/i2c/drivers")
DEFAULT_OUTPUT_DIR = Path("/run/h15-hsb")
DEFAULT_BASE_PROFILE = "face_landmarks_daylight"
HSB_STREAM_ID = "sink0"
AI_STREAM_ID = "sink1"
AI_WIDTH, AI_HEIGHT = 1920, 1080
STREAM_POOL_BUFFERS = 20
# Share of the frame period auto exposure may use as shutter time. The sensor
# lengthens the frame to fit a longer exposure, which lowers the frame rate.
MAX_SHUTTER_FRACTION = 0.9
SOC_BY_MACHINE = {
    "HAILO-15": "hailo15h",
    "HAILO-15H": "hailo15h",
    "HAILO-15L": "hailo15l",
}


@dataclass(frozen=True)
class StreamSpec:
    """Width, height and frame rate of a frontend stream."""

    width: int
    height: int
    fps: int

    @classmethod
    def parse(cls, text: str) -> StreamSpec:
        """Parses ``WIDTHxHEIGHT@FPS``.

        Args:
            text: Stream description, for example ``1280x720@60``.

        Returns:
            The parsed stream specification.

        Raises:
            ValueError: The text is not in ``WIDTHxHEIGHT@FPS`` form.
        """
        size, _, fps = text.partition("@")
        width, _, height = size.partition("x")
        if not (width.isdigit() and height.isdigit() and fps.isdigit()):
            raise ValueError(f"expected WIDTHxHEIGHT@FPS, got {text!r}")
        return cls(int(width), int(height), int(fps))


def detect_soc(machine_file: Path = SOC_MACHINE_FILE) -> str:
    """Returns the imaging-config SoC directory name for this board.

    Args:
        machine_file: The SoC machine name file.

    Returns:
        ``hailo15h`` or ``hailo15l``.

    Raises:
        RuntimeError: The machine name is not a known Hailo-15 variant.
    """
    machine = machine_file.read_text().strip("\0\n ")
    try:
        return SOC_BY_MACHINE[machine.upper()]
    except KeyError:
        raise RuntimeError(f"unknown SoC machine name {machine!r}") from None


def detect_sensor(drivers_dir: Path = I2C_DRIVERS) -> str:
    """Returns the name of the bound Sony image sensor driver.

    Args:
        drivers_dir: The I2C drivers directory in sysfs.

    Returns:
        The sensor name, for example ``imx678``.

    Raises:
        RuntimeError: No image sensor driver has a bound device.
    """
    for driver in sorted(drivers_dir.glob("imx*")):
        if any("-" in entry.name for entry in driver.iterdir()):
            return driver.name
    raise RuntimeError(f"no image sensor bound under {drivers_dir}")


def find_profile_dir(root: Path, soc: str, sensor: str, base_profile: str) -> Path:
    """Finds the stock daylight profile directory for a SoC and sensor.

    Args:
        root: Imaging configuration root.
        soc: SoC directory name.
        sensor: Sensor directory name.
        base_profile: Profile directory name under ``profiles/daylight``.

    Returns:
        The profile directory.

    Raises:
        RuntimeError: No such profile is installed.
    """
    pattern = f"{soc}/{sensor}/*/*/profiles/daylight/{base_profile}"
    matches = sorted(root.glob(pattern))
    if not matches:
        raise RuntimeError(f"no stock profile matching {root / pattern}")
    return matches[0]


def _edit_json(path: Path, edit) -> None:
    data = json.loads(path.read_text())
    edit(data)
    path.write_text(json.dumps(data, indent=2) + "\n")


def limit_auto_exposure(iq_settings: dict, fps: int) -> None:
    """Keeps auto exposure from lowering the sensor frame rate.

    Caps every shutter range in the adaptive auto exposure priority tables to
    MAX_SHUTTER_FRACTION of the frame period, so darker scenes get more gain
    instead of longer frames, and turns off automatic frame rate reduction.

    Args:
        iq_settings: Parsed ``iq_settings.json``; edited in place.
        fps: Sensor frame rate.
    """
    auto_exposure = iq_settings["automatic_algorithms"]["AdaptiveAe"]
    max_shutter_s = MAX_SHUTTER_FRACTION / fps
    for table in ("priority", "priorityHdr"):
        for entry in auto_exposure.get(table, []):
            entry[0] = [min(limit, max_shutter_s) for limit in entry[0]]
    auto_exposure["antiFlickerAfps"] = False


def _rewrite_paths(directory: Path, old_prefix: str, new_prefix: str) -> None:
    for path in directory.glob("*.json"):
        path.write_text(path.read_text().replace(old_prefix, new_prefix))


def build_config(
    profile_dir: Path, output_dir: Path, soc: str, hsb: StreamSpec, ai: StreamSpec
) -> Path:
    """Writes the sender configuration derived from a stock profile.

    Args:
        profile_dir: Stock profile directory.
        output_dir: Directory for the generated configuration.
        soc: SoC directory name, recorded as the configuration architecture.
        hsb: HSB stream; its frame rate is also the sensor frame rate.
        ai: Detection input stream.

    Returns:
        Path of the generated top-level media library configuration.
    """
    profile_out = output_dir / "profile"
    if profile_out.exists():
        shutil.rmtree(profile_out)
    shutil.copytree(profile_dir, profile_out)
    _rewrite_paths(profile_out, str(profile_dir), str(profile_out))

    _edit_json(
        profile_out / "sensor_config.json",
        lambda data: data["input_video"]["resolution"].update(framerate=hsb.fps),
    )

    _edit_json(
        profile_out / "iq_settings.json",
        lambda data: limit_auto_exposure(data, hsb.fps),
    )

    def set_streams(data: dict) -> None:
        streams = data["application_input_streams"]
        streams["resolutions"] = [
            _stream_entry(HSB_STREAM_ID, hsb),
            _stream_entry(AI_STREAM_ID, ai),
        ]

    _edit_json(profile_out / "application_settings.json", set_streams)

    profile_file = profile_out / f"{profile_dir.name}_profile.json"
    _edit_json(profile_file, lambda data: data.update(encoded_output_streams=[]))

    top = {
        "version": "2.0.0",
        "metadata": {
            **json.loads(profile_file.read_text())["metadata"],
            "architecture": soc,
        },
        "backup_folder_path": str(output_dir / "backup"),
        "default_profile": "Daylight",
        "profiles": [{"name": "Daylight", "config_file": str(profile_file)}],
    }
    config_path = output_dir / "medialib_config.json"
    config_path.write_text(json.dumps(top, indent=2) + "\n")
    return config_path


def _stream_entry(stream_id: str, spec: StreamSpec) -> dict:
    return {
        "stream_id": stream_id,
        "width": spec.width,
        "height": spec.height,
        "framerate": spec.fps,
        "pool_max_buffers": STREAM_POOL_BUFFERS,
    }


def parse_arguments() -> argparse.Namespace:
    """Parses the command line.

    Returns:
        Parsed arguments.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--soc", help="hailo15h or hailo15l (default: detect)")
    parser.add_argument("--sensor", help="for example imx678 (default: detect)")
    parser.add_argument("--base-profile", default=DEFAULT_BASE_PROFILE)
    parser.add_argument(
        "--hsb-stream",
        type=StreamSpec.parse,
        default=StreamSpec(1280, 720, 60),
        help="HSB stream as WIDTHxHEIGHT@FPS (default 1280x720@60)",
    )
    return parser.parse_args()


def main() -> int:
    """Generates the configuration and prints its path.

    Returns:
        Process exit code.
    """
    args = parse_arguments()
    try:
        soc = args.soc or detect_soc()
        sensor = args.sensor or detect_sensor()
        profile_dir = find_profile_dir(IMAGING_ROOT, soc, sensor, args.base_profile)
    except (OSError, RuntimeError) as error:
        print(f"h15-hsb-make-config: {error}", file=sys.stderr)
        return 1
    args.output_dir.mkdir(parents=True, exist_ok=True)
    ai = StreamSpec(AI_WIDTH, AI_HEIGHT, args.hsb_stream.fps)
    config_path = build_config(profile_dir, args.output_dir, soc, args.hsb_stream, ai)
    print(config_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
