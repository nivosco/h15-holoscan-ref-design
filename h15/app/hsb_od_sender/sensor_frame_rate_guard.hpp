// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Microchip Technology Inc. and its subsidiaries.
//
// Restores the sensor frame rate when the sensor driver leaves frames longer than the mode needs.
#pragma once

#include <chrono>
#include <cstdint>

/**
 * @brief Watches the camera frame rate and shortens the sensor frame when it runs slow.
 *
 * Some sensor drivers set the frame length from the exposure only when the exposure control
 * changes, and start the first stream with a default exposure longer than the frame period.
 * The ISP then adjusts exposure through the sensor registers, so the frame stays long and the
 * camera runs below its mode's frame rate. When the measured rate stays below the target, the
 * guard writes an exposure that fits the target frame period once, which makes the driver
 * recompute the frame length. Auto exposure takes over the exposure again on its next update.
 */
class SensorFrameRateGuard
{
  public:
    explicit SensorFrameRateGuard(uint32_t target_fps);

    /**
     * @brief Adds the camera frames counted over an interval and corrects a slow sensor.
     *
     * @param sensor_frames Frames the sensor produced in the interval, including dropped ones.
     * @param elapsed Length of the interval.
     */
    void update(uint64_t sensor_frames, std::chrono::duration<double> elapsed);

  private:
    void check_window();
    bool shorten_sensor_frame(double measured_fps);

    uint32_t m_target_fps;
    uint64_t m_window_frames = 0;
    std::chrono::duration<double> m_window_time{0};
    bool m_startup_window_done = false;
    int m_attempts = 0;
    bool m_gave_up = false;
};
