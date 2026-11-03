// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Microchip Technology Inc. and its subsidiaries.

#include "hsb_sink_stage.hpp"

#include "h15_hsb/frame_metadata.hpp"
#include "hololink/emulation/coe_data_plane.hpp"
#include "hololink/emulation/linux_data_plane.hpp"
#include "hololink/emulation/net.hpp"

#include <pthread.h>
#include <sched.h>

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <utility>

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

HsbSinkStage::HsbSinkStage(std::string name, HsbSinkConfig config, size_t queue_size)
    : ThreadedStage(std::move(name), queue_size, LEAKY_QUEUE, TRACE_PROCESSING), m_config(std::move(config)),
      m_frame(static_cast<size_t>(m_config.width) * (m_config.height + m_config.height / UV_ROWS_DIVISOR)),
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

void HsbSinkStage::apply_thread_policy()
{
    // process() runs on the stage's own thread, so the policy is applied on its first call.
    m_thread_policy_applied = true;
    if (m_config.send_cpu >= 0)
    {
        cpu_set_t cpus;
        CPU_ZERO(&cpus);
        CPU_SET(m_config.send_cpu, &cpus);
        const int error = pthread_setaffinity_np(pthread_self(), sizeof(cpus), &cpus);
        if (error != 0)
        {
            std::fprintf(stderr, "hsb_sink: cannot pin to CPU %d: %s\n", m_config.send_cpu, std::strerror(error));
        }
    }
    if (m_config.send_priority > 0)
    {
        const sched_param parameters{.sched_priority = m_config.send_priority};
        const int error = pthread_setschedparam(pthread_self(), SCHED_FIFO, &parameters);
        if (error != 0)
        {
            std::fprintf(stderr, "hsb_sink: cannot set SCHED_FIFO priority %d: %s\n", m_config.send_priority,
                         std::strerror(error));
        }
    }
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

void HsbSinkStage::pack_frame(const HailoMediaLibraryBufferPtr &buffer)
{
    const uint32_t width = m_config.width;
    const uint32_t plane_rows[NV12_PLANE_COUNT] = {m_config.height, m_config.height / UV_ROWS_DIVISOR};
    uint8_t *destination = m_frame.data();
    for (uint32_t plane : {Y_PLANE, UV_PLANE})
    {
        const auto *source = static_cast<const uint8_t *>(buffer->get_plane_ptr(plane));
        const uint32_t stride = buffer->get_plane_stride(plane);
        const size_t plane_size = static_cast<size_t>(width) * plane_rows[plane];
        if (stride == width)
        {
            std::memcpy(destination, source, plane_size);
            destination += plane_size;
            continue;
        }
        for (uint32_t row = 0; row < plane_rows[plane]; row++)
        {
            std::memcpy(destination, source + static_cast<size_t>(row) * stride, width);
            destination += width;
        }
    }
}

bool HsbSinkStage::send_frame(uint64_t isp_timestamp_ns, uint64_t &bytes_sent)
{
    const uint32_t crc = h15_hsb::frame_crc32(0, m_frame.data(), m_frame.size());
    auto metadata = h15_hsb::make_frame_metadata(crc, isp_timestamp_ns);
    const int64_t sent = m_data_plane->send(m_frame.data(), m_frame.size(), &metadata);
    if (sent < 0)
    {
        return false;
    }
    bytes_sent = static_cast<uint64_t>(sent);
    return true;
}

AppStatus HsbSinkStage::process(hailo_analytics::pipeline::BufferPtr data)
{
    if (!m_thread_policy_applied)
    {
        apply_thread_policy();
    }
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
    pack_frame(buffer);
    buffer->sync_end();
    uint64_t bytes_sent = 0;
    const bool sent_ok = send_frame(buffer->isp_timestamp_ns, bytes_sent);
    const auto send_time = std::chrono::steady_clock::now() - send_start;

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
