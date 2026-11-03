// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Microchip Technology Inc. and its subsidiaries.
//
// Pipeline sink that sends each NV12 frontend buffer over Holoscan Sensor Bridge.
#pragma once

#include "hailo_analytics/pipeline/core/buffer.hpp"
#include "hailo_analytics/pipeline/core/stage.hpp"
#include "hololink/emulation/data_plane.hpp"
#include "hololink/emulation/hsb_emulator.hpp"

#include <atomic>
#include <chrono>
#include <cstdint>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <vector>

enum class HsbTransport
{
    RoCEv2,
    COE,
};

struct HsbSinkConfig
{
    HsbTransport transport = HsbTransport::RoCEv2;
    std::string source_ip;
    uint32_t width = 0;
    uint32_t height = 0;
    uint32_t fps = 0;
};

struct HsbSinkStats
{
    uint64_t frames_sent = 0;
    uint64_t frames_not_sent = 0;
    uint64_t send_errors = 0;
    uint64_t frames_skipped = 0;
    uint64_t bad_buffers = 0;
    uint64_t bytes_sent = 0;
    std::chrono::nanoseconds send_time_total{0};
    std::chrono::nanoseconds send_time_max{0};
};

/**
 * @brief Sends every received NV12 buffer as one HSB frame.
 *
 * Each frame carries a CRC-32 of its payload and the buffer's ISP timestamp in the HSB frame
 * metadata. The ISP timestamp is the same value the detection metadata carries as
 * isp_timestamp_ns, so receivers join video and detections on it. The input queue is leaky:
 * when sending falls behind, the oldest buffer is dropped rather than stalling the camera.
 */
class HsbSinkStage : public hailo_analytics::pipeline::ThreadedStage
{
  public:
    HsbSinkStage(std::string name, HsbSinkConfig config, size_t queue_size);

    hailo_analytics::pipeline::AppStatus init() override;
    hailo_analytics::pipeline::AppStatus deinit() override;
    hailo_analytics::pipeline::AppStatus process(hailo_analytics::pipeline::BufferPtr data) override;

    /**
     * @brief Returns the statistics gathered since the previous call and resets them.
     */
    HsbSinkStats take_stats();

  private:
    void pack_frame(const HailoMediaLibraryBufferPtr &buffer);
    bool send_frame(uint64_t isp_timestamp_ns, uint64_t &bytes_sent);
    void count_skipped_frames(uint64_t isp_timestamp_ns);

    HsbSinkConfig m_config;
    hololink::emulation::HSBEmulator m_hsb;
    std::unique_ptr<hololink::emulation::DataPlane> m_data_plane;
    // Y and UV live in separate DMA buffers; the emulator ends a packet and advances the frame
    // number on every send, so each frame is packed here and sent with a single call.
    std::vector<uint8_t> m_frame;
    std::chrono::nanoseconds m_frame_interval;
    std::optional<uint64_t> m_last_isp_timestamp_ns;
    std::mutex m_stats_mutex;
    HsbSinkStats m_stats;
};
