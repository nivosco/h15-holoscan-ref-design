// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Microchip Technology Inc. and its subsidiaries.

#include "hsb_sink_stage.hpp"

#include "h15_hsb/frame_metadata.hpp"
#include "hololink/emulation/coe_data_plane.hpp"
#include "hololink/emulation/linux_data_plane.hpp"
#include "hololink/emulation/net.hpp"

#include <algorithm>
#include <cmath>
#include <utility>
#include <vector>

namespace emulation = hololink::emulation;
using hailo_analytics::pipeline::AppStatus;

static constexpr uint8_t DATA_PLANE_ID = 0;
static constexpr uint8_t SENSOR_ID = 0;
static constexpr uint32_t NV12_PLANE_COUNT = 2;
static constexpr uint32_t Y_PLANE = 0;
static constexpr uint32_t UV_PLANE = 1;
static constexpr uint32_t UV_ROWS_DIVISOR = 2;
static constexpr bool LEAKY_QUEUE = true;
static constexpr bool TRACE_PROCESSING = false;
// A gap of more than one and a half frame intervals between ISP timestamps means frames were
// dropped before this stage (leaky queue or frontend).
static constexpr double SKIP_THRESHOLD_INTERVALS = 1.5;
static constexpr int64_t NANOSECONDS_PER_SECOND = 1000000000;

struct PlaneSegment
{
    const uint8_t *data;
    size_t size;
};

HsbSinkStage::HsbSinkStage(std::string name, HsbSinkConfig config, size_t queue_size)
    : ThreadedStage(std::move(name), queue_size, LEAKY_QUEUE, TRACE_PROCESSING), m_config(std::move(config)),
      m_frame_interval(NANOSECONDS_PER_SECOND / m_config.fps)
{
}

AppStatus HsbSinkStage::init()
{
    const auto source_ip = emulation::IPAddress_from_string(m_config.source_ip);
    if (m_config.transport == HsbTransport::COE)
    {
        m_data_plane = std::make_unique<emulation::COEDataPlane>(m_hsb, source_ip, DATA_PLANE_ID, SENSOR_ID);
    }
    else
    {
        m_data_plane = std::make_unique<emulation::RoCEv2DataPlane>(m_hsb, source_ip, DATA_PLANE_ID, SENSOR_ID);
    }
    m_hsb.start();
    return AppStatus::SUCCESS;
}

AppStatus HsbSinkStage::deinit()
{
    m_hsb.stop();
    m_data_plane.reset();
    return AppStatus::SUCCESS;
}

HsbSinkStats HsbSinkStage::take_stats()
{
    std::lock_guard<std::mutex> lock(m_stats_mutex);
    return std::exchange(m_stats, HsbSinkStats{});
}

void HsbSinkStage::count_skipped_frames(uint64_t isp_timestamp_ns)
{
    if (m_last_isp_timestamp_ns.has_value() && isp_timestamp_ns > *m_last_isp_timestamp_ns)
    {
        const double intervals = static_cast<double>(isp_timestamp_ns - *m_last_isp_timestamp_ns) /
                                 static_cast<double>(m_frame_interval.count());
        if (intervals > SKIP_THRESHOLD_INTERVALS)
        {
            std::lock_guard<std::mutex> lock(m_stats_mutex);
            m_stats.frames_skipped += static_cast<uint64_t>(std::lround(intervals)) - 1;
        }
    }
    m_last_isp_timestamp_ns = isp_timestamp_ns;
}

static std::vector<PlaneSegment> nv12_segments(const HailoMediaLibraryBufferPtr &buffer, uint32_t width,
                                               uint32_t height)
{
    std::vector<PlaneSegment> segments;
    const uint32_t plane_rows[NV12_PLANE_COUNT] = {height, height / UV_ROWS_DIVISOR};
    for (uint32_t plane : {Y_PLANE, UV_PLANE})
    {
        const auto *data = static_cast<const uint8_t *>(buffer->get_plane_ptr(plane));
        const uint32_t stride = buffer->get_plane_stride(plane);
        if (stride == width)
        {
            segments.push_back({data, static_cast<size_t>(width) * plane_rows[plane]});
            continue;
        }
        for (uint32_t row = 0; row < plane_rows[plane]; row++)
        {
            segments.push_back({data + static_cast<size_t>(row) * stride, width});
        }
    }
    return segments;
}

bool HsbSinkStage::send_frame(const HailoMediaLibraryBufferPtr &buffer, uint64_t &bytes_sent)
{
    const auto segments = nv12_segments(buffer, m_config.width, m_config.height);

    uint32_t crc = 0;
    for (const auto &segment : segments)
    {
        crc = h15_hsb::frame_crc32(crc, segment.data, segment.size);
    }
    auto metadata = h15_hsb::make_frame_metadata(crc, buffer->isp_timestamp_ns);

    // Every segment but the last is buffered by the emulator; the last one carries the metadata
    // and ends the frame.
    for (size_t index = 0; index < segments.size(); index++)
    {
        const bool last = index + 1 == segments.size();
        const int64_t sent =
            m_data_plane->send(segments[index].data, segments[index].size, last ? &metadata : nullptr);
        if (sent < 0)
        {
            return false;
        }
        bytes_sent += static_cast<uint64_t>(sent);
    }
    return true;
}

AppStatus HsbSinkStage::process(hailo_analytics::pipeline::BufferPtr data)
{
    const HailoMediaLibraryBufferPtr buffer = data->get_buffer();
    if (!buffer || buffer->get_num_of_planes() != NV12_PLANE_COUNT)
    {
        std::lock_guard<std::mutex> lock(m_stats_mutex);
        m_stats.bad_buffers++;
        return AppStatus::INVALID_ARGUMENT;
    }
    count_skipped_frames(buffer->isp_timestamp_ns);

    if (buffer->sync_start() != media_library_return::MEDIA_LIBRARY_SUCCESS)
    {
        std::lock_guard<std::mutex> lock(m_stats_mutex);
        m_stats.bad_buffers++;
        return AppStatus::DMA_ERROR;
    }
    const auto send_start = std::chrono::steady_clock::now();
    uint64_t bytes_sent = 0;
    const bool sent_ok = send_frame(buffer, bytes_sent);
    const auto send_time = std::chrono::steady_clock::now() - send_start;
    buffer->sync_end();

    std::lock_guard<std::mutex> lock(m_stats_mutex);
    if (!sent_ok)
    {
        m_stats.send_errors++;
        return AppStatus::PIPELINE_ERROR;
    }
    if (bytes_sent == 0)
    {
        // No host has configured the data plane yet.
        m_stats.frames_not_sent++;
        return AppStatus::SUCCESS;
    }
    m_stats.frames_sent++;
    m_stats.bytes_sent += bytes_sent;
    m_stats.send_time_total += send_time;
    m_stats.send_time_max = std::max(m_stats.send_time_max, std::chrono::nanoseconds(send_time));
    return AppStatus::SUCCESS;
}
