# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Microchip Technology Inc. and its subsidiaries.
"""Configuration generation against a fake imaging tree and sysfs."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import h15_hsb_make_config as make_config  # noqa: E402

PROFILE = "face_landmarks_daylight"


@pytest.fixture
def stock_profile(tmp_path: Path) -> Path:
    profile_dir = (
        tmp_path / "cfg/hailo15h/imx678/theia_sl410m/4k/profiles/daylight" / PROFILE
    )
    profile_dir.mkdir(parents=True)
    (profile_dir / "sensor_config.json").write_text(
        json.dumps(
            {
                "input_video": {
                    "resolution": {"framerate": 30, "height": 2160, "width": 3840}
                },
                "sensor_calibration_file": str(profile_dir / "calib.json"),
            }
        )
    )
    (profile_dir / "application_settings.json").write_text(
        json.dumps(
            {
                "application_input_streams": {
                    "format": "IMAGE_FORMAT_NV12",
                    "resolutions": [{"stream_id": "sink0", "width": 3840}],
                }
            }
        )
    )
    (profile_dir / f"{PROFILE}_profile.json").write_text(
        json.dumps(
            {
                "metadata": {
                    "architecture": "hailo15h",
                    "content_hash": "0",
                    "description": "",
                    "generation_timestamp": "2026-01-01T00:00:00+00:00",
                },
                "sensor_config": str(profile_dir / "sensor_config.json"),
                "encoded_output_streams": [
                    {"encoding": str(profile_dir / "encoder_sink0.json")}
                ],
            }
        )
    )
    (profile_dir / "iq_settings.json").write_text(
        json.dumps({"automatic_algorithms": {"AdaptiveAe": auto_exposure()}})
    )
    (profile_dir / "calib.json").write_text("{}")
    return profile_dir


def auto_exposure() -> dict:
    shutter_ranges = [[3.8e-05, 0.004], [0.004, 0.033], [0.033, 0.033]]

    def table() -> list:
        return [[list(limits), [1, 10], [1.0, 1.0]] for limits in shutter_ranges]

    return {"antiFlickerAfps": True, "priority": table(), "priorityHdr": table()}


def test_stream_spec_parses_size_and_rate() -> None:
    assert make_config.StreamSpec.parse("1280x720@60") == make_config.StreamSpec(
        1280, 720, 60
    )


def test_stream_spec_rejects_bad_text() -> None:
    with pytest.raises(ValueError):
        make_config.StreamSpec.parse("1280x720")


@pytest.mark.parametrize(
    ("machine", "soc"),
    [
        ("Hailo-15\n", "hailo15h"),
        ("Hailo-15l\n", "hailo15l"),
        ("Hailo-15L", "hailo15l"),
    ],
)
def test_detect_soc(tmp_path: Path, machine: str, soc: str) -> None:
    machine_file = tmp_path / "machine"
    machine_file.write_text(machine)

    assert make_config.detect_soc(machine_file) == soc


def test_detect_sensor_needs_a_bound_device(tmp_path: Path) -> None:
    (tmp_path / "imx662").mkdir()
    (tmp_path / "imx678").mkdir()
    (tmp_path / "imx678" / "0-001a").mkdir()

    assert make_config.detect_sensor(tmp_path) == "imx678"


def test_build_config_sets_streams_rate_and_paths(
    tmp_path: Path, stock_profile: Path
) -> None:
    output_dir = tmp_path / "run"
    hsb = make_config.StreamSpec(1280, 720, 60)
    ai = make_config.StreamSpec(1920, 1080, 60)

    config_path = make_config.build_config(
        stock_profile, output_dir, "hailo15h", hsb, ai
    )

    top = json.loads(config_path.read_text())
    profile_file = Path(top["profiles"][0]["config_file"])
    profile = json.loads(profile_file.read_text())
    sensor = json.loads((output_dir / "profile/sensor_config.json").read_text())
    streams = json.loads((output_dir / "profile/application_settings.json").read_text())
    resolutions = streams["application_input_streams"]["resolutions"]
    assert top["metadata"]["architecture"] == "hailo15h"
    assert top["metadata"]["description"] == ""
    assert profile_file.parent == output_dir / "profile"
    assert profile["encoded_output_streams"] == []
    assert profile["sensor_config"] == str(output_dir / "profile/sensor_config.json")
    assert sensor["input_video"]["resolution"]["framerate"] == 60
    assert sensor["sensor_calibration_file"] == str(output_dir / "profile/calib.json")
    assert [
        (r["stream_id"], r["width"], r["height"], r["framerate"]) for r in resolutions
    ] == [
        ("sink0", 1280, 720, 60),
        ("sink1", 1920, 1080, 60),
    ]
    assert (
        json.loads((stock_profile / "sensor_config.json").read_text())["input_video"][
            "resolution"
        ]["framerate"]
        == 30
    )


def test_limit_auto_exposure_caps_shutter_to_the_frame_period() -> None:
    iq_settings = {"automatic_algorithms": {"AdaptiveAe": auto_exposure()}}

    make_config.limit_auto_exposure(iq_settings, 60)

    auto = iq_settings["automatic_algorithms"]["AdaptiveAe"]
    max_shutter_s = make_config.MAX_SHUTTER_FRACTION / 60
    assert auto["antiFlickerAfps"] is False
    for table in ("priority", "priorityHdr"):
        assert [entry[0] for entry in auto[table]] == [
            [3.8e-05, 0.004],
            [0.004, max_shutter_s],
            [max_shutter_s, max_shutter_s],
        ]


def test_build_config_limits_auto_exposure(tmp_path: Path, stock_profile: Path) -> None:
    hsb = make_config.StreamSpec(1280, 720, 60)

    make_config.build_config(stock_profile, tmp_path / "run", "hailo15h", hsb, hsb)

    iq_settings = json.loads((tmp_path / "run/profile/iq_settings.json").read_text())
    stock = json.loads((stock_profile / "iq_settings.json").read_text())
    assert iq_settings["automatic_algorithms"]["AdaptiveAe"]["antiFlickerAfps"] is False
    assert stock["automatic_algorithms"]["AdaptiveAe"]["antiFlickerAfps"] is True
