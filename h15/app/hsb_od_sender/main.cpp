// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Microchip Technology Inc. and its subsidiaries.
//
// Hailo-15 Holoscan Sensor Bridge sender: runs Hailo's object detection and tracking pipeline and
// sends a raw NV12 frontend stream over HSB, with the detections published as protobuf over ZMQ.
// The pipeline wiring follows the Hailo analytics detection case study.

#include "h15_hsb/system_utils.hpp"
#include "hsb_sink_stage.hpp"

#include "hailo_analytics/analytics/ai_models_config.hpp"
#include "hailo_postprocess_tools/labels/hailo_yolov8n.hpp"
#include "hailo_analytics/analytics/analytic_metadata_zmq_sender.hpp"
#include "hailo_analytics/analytics/tiling.hpp"
#include "hailo_analytics/analytics/vision.hpp"
#include "hailo_analytics/pipeline/core/pipeline.hpp"
#include "hailo_analytics/pipeline/core/pipeline_builder.hpp"
#include "hailo_analytics/utils/platform_utils.hpp"
#include "media_library/media_library.hpp"
#include "media_library/signal_utils.hpp"

#include <cxxopts/cxxopts.hpp>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <iostream>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <vector>

using hailo_analytics::pipeline::AppStatus;
using hailo_analytics::pipeline::PipelinePtr;

static constexpr const char *APP_NAME = "h15_hsb_sender";
static constexpr const char *VISION_PIPELINE = "vision_pipeline";
static constexpr const char *TILING_PIPELINE = "tiling_detection_pipeline";
static constexpr const char *METADATA_PIPELINE = "analytic_metadata_sender_pipeline";
static constexpr const char *HSB_STAGE = "hsb_sink";
static constexpr const char *DEFAULT_CONFIG = "/run/h15-hsb/medialib_config.json";
static constexpr const char *DEFAULT_INTERFACE = "eth0";
static constexpr const char *DEFAULT_HSB_STREAM = "sink0";
static constexpr const char *DEFAULT_AI_STREAM = "sink1";
static constexpr const char *DEFAULT_ZMQ_PORT = "7000";
static constexpr const char *DEFAULT_TRANSPORT = "coe";
static constexpr size_t HSB_QUEUE_SIZE = 2;
static constexpr size_t METADATA_QUEUE_SIZE = 1;
static constexpr double BITS_PER_BYTE = 8.0;
static constexpr double MEGA = 1e6;
static constexpr double PERCENT = 100.0;
static constexpr auto STATS_INTERVAL = std::chrono::seconds(1);

struct AppOptions
{
    std::string config_path;
    HsbTransport transport = HsbTransport::COE;
    std::string interface_name;
    std::string hsb_stream;
    std::string ai_stream;
    std::string zmq_port;
    int timeout_seconds = 0;
    bool print_stats = false;
};

static std::mutex g_stop_mutex;
static std::condition_variable g_stop_cv;
static std::atomic<bool> g_stop_requested = false;

static std::optional<AppOptions> parse_options(int argc, char **argv)
{
    cxxopts::Options parser(APP_NAME, "Hailo-15 object detection + tracking with raw NV12 video over HSB");
    // clang-format off
    parser.add_options()
        ("h,help", "Show this help")
        ("c,config", "Media library configuration", cxxopts::value<std::string>()->default_value(DEFAULT_CONFIG))
        ("t,transport", "HSB transport: coe (Jetson AGX Thor) or roce (RoCEv2 over UDP, development)",
            cxxopts::value<std::string>()->default_value(DEFAULT_TRANSPORT))
        ("i,interface", "Network interface to send from", cxxopts::value<std::string>()->default_value(DEFAULT_INTERFACE))
        ("hsb-stream", "Frontend stream sent over HSB", cxxopts::value<std::string>()->default_value(DEFAULT_HSB_STREAM))
        ("ai-stream", "Frontend stream fed to detection", cxxopts::value<std::string>()->default_value(DEFAULT_AI_STREAM))
        ("z,zmq-port", "Detection metadata ZMQ publisher port", cxxopts::value<std::string>()->default_value(DEFAULT_ZMQ_PORT))
        ("s,timeout", "Stop after this many seconds (0: run until interrupted)", cxxopts::value<int>()->default_value("0"))
        ("p,print-stats", "Print HSB send statistics every second");
    // clang-format on

    const auto result = parser.parse(argc, argv);
    if (result.count("help"))
    {
        std::cout << parser.help() << std::endl;
        return std::nullopt;
    }

    AppOptions options;
    options.config_path = result["config"].as<std::string>();
    options.interface_name = result["interface"].as<std::string>();
    options.hsb_stream = result["hsb-stream"].as<std::string>();
    options.ai_stream = result["ai-stream"].as<std::string>();
    options.zmq_port = result["zmq-port"].as<std::string>();
    options.timeout_seconds = result["timeout"].as<int>();
    options.print_stats = result.count("print-stats") > 0;

    const auto transport = result["transport"].as<std::string>();
    if (transport != "coe" && transport != "roce")
    {
        std::cerr << "Unknown transport '" << transport << "'; use coe or roce" << std::endl;
        return std::nullopt;
    }
    options.transport = transport == "coe" ? HsbTransport::COE : HsbTransport::RoCEv2;
    return options;
}

static std::optional<frontend_output_stream_t> find_stream(const std::vector<frontend_output_stream_t> &streams,
                                                           const std::string &stream_id)
{
    const auto match = std::find_if(streams.begin(), streams.end(),
                                    [&stream_id](const auto &stream) { return stream.id == stream_id; });
    if (match == streams.end())
    {
        return std::nullopt;
    }
    return *match;
}

static hailo_analytics::analytics::ai_models::model_bundle_t detection_model()
{
    namespace ai_models = hailo_analytics::analytics::ai_models;
    // Same model choice per SoC as the Hailo webserver detection pipeline.
    if (hailo_analytics::utils::get_hailo_architecture() == hailo_analytics::utils::Architecture::Hailo15H)
    {
        return ai_models::YOLOV8S;
    }
    return ai_models::YOLOV8N;
}

static tl::expected<PipelinePtr, AppStatus> create_tiling_pipeline()
{
    auto tiling_config = hailo_analytics::analytics::tiling::base_config();
    hailo_analytics::analytics::ai_models::apply_to(detection_model(), tiling_config.detection_config);
    tiling_config.tracker_config.enabled = true;
    // The tracker cannot be created without a label map. Hailo's YOLOv8n and YOLOv8s share the
    // person / vehicle / face / license_plate classes.
    tiling_config.tracker_config.labels_map = common::hailo_yolov8n;
    return hailo_analytics::analytics::tiling::generate_tiling_detection_pipeline(TILING_PIPELINE, tiling_config);
}

static tl::expected<PipelinePtr, AppStatus> create_metadata_pipeline(const std::string &zmq_port)
{
    namespace zmq_sender = hailo_analytics::analytics::analytic_metadata_zmq_sender;
    zmq_sender::analytic_metadata_zmq_sender_config_t config;
    config.analytic_metadata_config.queue_size = METADATA_QUEUE_SIZE;
    config.zeromq_config.queue_size = METADATA_QUEUE_SIZE;
    config.zeromq_config.pub_address = "tcp://*:" + zmq_port;
    return zmq_sender::generate_analytic_metadata_zmq_sender_pipeline(METADATA_PIPELINE, config);
}

static PipelinePtr build_pipeline(MediaLibraryInterfacePtr media_library, const AppOptions &options,
                                  const std::vector<frontend_output_stream_t> &output_streams,
                                  const frontend_output_stream_t &hsb_stream, std::shared_ptr<HsbSinkStage> &hsb_stage)
{
    // Without any vision outputs no frontend stream is encoded; the HSB and AI streams are
    // connected to their stages below.
    auto vision_config = hailo_analytics::analytics::vision::base_vision_config(output_streams);
    vision_config.outputs.clear();
    auto vision_pipeline =
        hailo_analytics::analytics::vision::generate_vision_pipeline(media_library, VISION_PIPELINE, vision_config);
    if (!vision_pipeline.has_value())
    {
        std::cerr << "Failed to create the vision pipeline" << std::endl;
        return nullptr;
    }

    auto tiling_pipeline = create_tiling_pipeline();
    if (!tiling_pipeline.has_value())
    {
        std::cerr << "Failed to create the detection pipeline" << std::endl;
        return nullptr;
    }

    auto metadata_pipeline = create_metadata_pipeline(options.zmq_port);
    if (!metadata_pipeline.has_value())
    {
        std::cerr << "Failed to create the metadata sender pipeline" << std::endl;
        return nullptr;
    }

    auto source_ip = h15_hsb::get_interface_ipv4(options.interface_name);
    if (!source_ip.has_value())
    {
        std::cerr << "No IPv4 address on " << options.interface_name << std::endl;
        return nullptr;
    }
    HsbSinkConfig hsb_config{options.transport, *source_ip, hsb_stream.width, hsb_stream.height,
                             hsb_stream.target_fps};
    hsb_stage = std::make_shared<HsbSinkStage>(HSB_STAGE, hsb_config, HSB_QUEUE_SIZE);

    hailo_analytics::pipeline::PipelineBuilder builder;
    builder.add_stage(vision_pipeline.value(), hailo_analytics::pipeline::StageType::SOURCE)
        .add_stage(tiling_pipeline.value())
        .add_stage(metadata_pipeline.value(), hailo_analytics::pipeline::StageType::SINK)
        .add_stage(hsb_stage, hailo_analytics::pipeline::StageType::SINK);
    builder.connect_frontend(VISION_PIPELINE, options.ai_stream, TILING_PIPELINE);
    builder.connect(TILING_PIPELINE, METADATA_PIPELINE);
    builder.connect_frontend(VISION_PIPELINE, options.hsb_stream, HSB_STAGE);
    return builder.build(APP_NAME, true);
}

static void print_stats(const HsbSinkStats &stats, std::chrono::duration<double> elapsed, double cpu_seconds)
{
    const double seconds = elapsed.count();
    const double average_us =
        stats.frames_sent
            ? std::chrono::duration<double, std::micro>(stats.send_time_total).count() / stats.frames_sent
            : 0.0;
    std::printf("fps=%.1f  Mbit/s=%.1f  send_avg_us=%.0f  send_max_us=%.0f  skipped=%llu  not_sent=%llu  "
                "errors=%llu  bad_buffers=%llu  cpu=%.0f%%\n",
                stats.frames_sent / seconds, stats.bytes_sent * BITS_PER_BYTE / MEGA / seconds, average_us,
                std::chrono::duration<double, std::micro>(stats.send_time_max).count(),
                static_cast<unsigned long long>(stats.frames_skipped),
                static_cast<unsigned long long>(stats.frames_not_sent),
                static_cast<unsigned long long>(stats.send_errors), static_cast<unsigned long long>(stats.bad_buffers),
                PERCENT * cpu_seconds / seconds);
    std::fflush(stdout);
}

static void wait_for_stop(const AppOptions &options, HsbSinkStage &hsb_stage)
{
    const auto start = std::chrono::steady_clock::now();
    auto interval_start = start;
    double interval_cpu_start = h15_hsb::process_cpu_seconds();
    std::unique_lock<std::mutex> lock(g_stop_mutex);
    while (!g_stop_requested)
    {
        g_stop_cv.wait_for(lock, STATS_INTERVAL, [] { return g_stop_requested.load(); });
        const auto now = std::chrono::steady_clock::now();
        if (options.print_stats)
        {
            const double cpu_now = h15_hsb::process_cpu_seconds();
            print_stats(hsb_stage.take_stats(), now - interval_start, cpu_now - interval_cpu_start);
            interval_start = now;
            interval_cpu_start = cpu_now;
        }
        if (options.timeout_seconds > 0 && now - start >= std::chrono::seconds(options.timeout_seconds))
        {
            break;
        }
    }
}

int main(int argc, char **argv)
{
    auto options = parse_options(argc, argv);
    if (!options.has_value())
    {
        return 1;
    }

    signal_utils::SignalHandler signal_handler(false);
    signal_handler.register_signal_handler([](int /*signal*/) {
        g_stop_requested = true;
        g_stop_cv.notify_all();
    });

    auto media_library_expected = MediaLibrary::create();
    if (!media_library_expected.has_value())
    {
        std::cerr << "Failed to create the media library" << std::endl;
        return 1;
    }
    MediaLibraryInterfacePtr media_library = media_library_expected.value();
    if (media_library->initialize(options->config_path) != media_library_return::MEDIA_LIBRARY_SUCCESS)
    {
        std::cerr << "Failed to initialize the media library from " << options->config_path << std::endl;
        return 1;
    }

    auto output_streams = media_library->get_frontend_output_streams();
    if (!output_streams.has_value())
    {
        std::cerr << "Failed to read the frontend output streams" << std::endl;
        return 1;
    }
    auto hsb_stream = find_stream(output_streams.value(), options->hsb_stream);
    if (!hsb_stream.has_value() || !find_stream(output_streams.value(), options->ai_stream).has_value())
    {
        std::cerr << "Frontend streams '" << options->hsb_stream << "' and '" << options->ai_stream
                  << "' must both exist in " << options->config_path << std::endl;
        return 1;
    }

    std::shared_ptr<HsbSinkStage> hsb_stage;
    PipelinePtr pipeline = build_pipeline(media_library, *options, output_streams.value(), *hsb_stream, hsb_stage);
    if (!pipeline)
    {
        return 1;
    }

    std::printf("%s: %ux%u NV12 @ %u fps over %s from %s, detections on ZMQ port %s\n", APP_NAME, hsb_stream->width,
                hsb_stream->height, hsb_stream->target_fps, options->transport == HsbTransport::COE ? "COE" : "RoCEv2",
                options->interface_name.c_str(), options->zmq_port.c_str());
    pipeline->start();
    wait_for_stop(*options, *hsb_stage);
    pipeline->stop();
    media_library->shutdown();
    return 0;
}
