// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Microchip Technology Inc. and its subsidiaries.

#include "sensor_frame_rate_guard.hpp"

#include <fcntl.h>
#include <linux/videodev2.h>
#include <sys/ioctl.h>
#include <unistd.h>

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <filesystem>
#include <optional>
#include <string>

static constexpr auto MEASUREMENT_WINDOW = std::chrono::seconds(5);
// A rate within this share of the target counts as on target.
static constexpr double FRAME_RATE_TOLERANCE = 0.01;
// Keeps the new frame length a little below the target period, so rounding cannot leave it long.
static constexpr double FRAME_LENGTH_HEADROOM = 0.99;
static constexpr int MAX_ATTEMPTS = 3;
static constexpr const char *DEVICE_DIRECTORY = "/dev";
static constexpr const char *SUBDEVICE_PREFIX = "v4l-subdev";

namespace
{

struct SensorTiming
{
    int32_t exposure_lines;
    int32_t vertical_blanking_lines;
    int32_t minimum_vertical_blanking_lines;
};

class FileDescriptor
{
  public:
    explicit FileDescriptor(int descriptor) : m_descriptor(descriptor)
    {
    }
    ~FileDescriptor()
    {
        if (m_descriptor >= 0)
        {
            close(m_descriptor);
        }
    }
    FileDescriptor(const FileDescriptor &) = delete;
    FileDescriptor &operator=(const FileDescriptor &) = delete;

    int get() const
    {
        return m_descriptor;
    }

  private:
    int m_descriptor;
};

std::optional<int32_t> get_control(int descriptor, uint32_t control_id)
{
    v4l2_control control = {};
    control.id = control_id;
    if (ioctl(descriptor, VIDIOC_G_CTRL, &control) != 0)
    {
        return std::nullopt;
    }
    return control.value;
}

std::optional<SensorTiming> read_sensor_timing(int descriptor)
{
    v4l2_queryctrl vertical_blanking_range = {};
    vertical_blanking_range.id = V4L2_CID_VBLANK;
    if (ioctl(descriptor, VIDIOC_QUERYCTRL, &vertical_blanking_range) != 0)
    {
        return std::nullopt;
    }
    const auto exposure = get_control(descriptor, V4L2_CID_EXPOSURE);
    const auto vertical_blanking = get_control(descriptor, V4L2_CID_VBLANK);
    if (!exposure.has_value() || !vertical_blanking.has_value())
    {
        return std::nullopt;
    }
    return SensorTiming{*exposure, *vertical_blanking, vertical_blanking_range.minimum};
}

// Returns the exposure, in lines, that lets the frame shrink to the target frame period.
int32_t exposure_for_target_rate(const SensorTiming &timing, double measured_fps, uint32_t target_fps)
{
    const double frame_lines = timing.exposure_lines + timing.vertical_blanking_lines;
    const double target_frame_lines = frame_lines * measured_fps / target_fps * FRAME_LENGTH_HEADROOM;
    const auto exposure = static_cast<int32_t>(std::floor(target_frame_lines)) - timing.minimum_vertical_blanking_lines;
    // The driver acts only on a changed value.
    return std::min(exposure, timing.exposure_lines - 1);
}

} // namespace

SensorFrameRateGuard::SensorFrameRateGuard(uint32_t target_fps) : m_target_fps(target_fps)
{
}

void SensorFrameRateGuard::update(uint64_t sensor_frames, std::chrono::duration<double> elapsed)
{
    if (m_target_fps == 0 || m_gave_up)
    {
        return;
    }
    m_window_frames += sensor_frames;
    m_window_time += elapsed;
    if (m_window_time < MEASUREMENT_WINDOW)
    {
        return;
    }
    // The first window covers the pipeline start, when frames do not arrive at full rate yet.
    if (m_startup_window_done)
    {
        check_window();
    }
    m_startup_window_done = true;
    m_window_frames = 0;
    m_window_time = std::chrono::duration<double>::zero();
}

void SensorFrameRateGuard::check_window()
{
    const double measured_fps = m_window_frames / m_window_time.count();
    // No frames means the pipeline is not streaming, which is not a sensor timing problem.
    if (measured_fps == 0.0 || measured_fps >= m_target_fps * (1.0 - FRAME_RATE_TOLERANCE))
    {
        return;
    }
    if (m_attempts == MAX_ATTEMPTS || !shorten_sensor_frame(measured_fps))
    {
        std::printf("sensor frame rate guard: camera runs at %.1f fps instead of %u; giving up\n", measured_fps,
                    m_target_fps);
        std::fflush(stdout);
        m_gave_up = true;
        return;
    }
    m_attempts++;
}

bool SensorFrameRateGuard::shorten_sensor_frame(double measured_fps)
{
    for (const auto &entry : std::filesystem::directory_iterator(DEVICE_DIRECTORY))
    {
        const std::string device_name = entry.path().filename().string();
        if (!device_name.starts_with(SUBDEVICE_PREFIX))
        {
            continue;
        }
        FileDescriptor device(open(entry.path().c_str(), O_RDWR));
        if (device.get() < 0)
        {
            continue;
        }
        const auto timing = read_sensor_timing(device.get());
        if (!timing.has_value())
        {
            continue;
        }

        v4l2_control exposure = {};
        exposure.id = V4L2_CID_EXPOSURE;
        exposure.value = exposure_for_target_rate(*timing, measured_fps, m_target_fps);
        if (exposure.value <= 0 || ioctl(device.get(), VIDIOC_S_CTRL, &exposure) != 0)
        {
            return false;
        }
        std::printf("sensor frame rate guard: camera runs at %.1f fps instead of %u; set %s exposure from %d to "
                    "%d lines\n",
                    measured_fps, m_target_fps, entry.path().c_str(), timing->exposure_lines, exposure.value);
        std::fflush(stdout);
        return true;
    }
    return false;
}
