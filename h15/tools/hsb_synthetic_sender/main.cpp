// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Microchip Technology Inc. and its subsidiaries.

#include "hololink/emulation/coe_data_plane.hpp"
#include "hololink/emulation/hsb_emulator.hpp"
#include "hololink/emulation/linux_data_plane.hpp"
#include "hololink/emulation/net.hpp"

#include "h15_hsb/frame_metadata.hpp"

#include <arpa/inet.h>
#include <getopt.h>
#include <ifaddrs.h>
#include <netinet/in.h>
#include <unistd.h>

#include <atomic>
#include <chrono>
#include <csignal>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <memory>
#include <optional>
#include <string>
#include <thread>
#include <vector>

namespace emulation = hololink::emulation;
using steady_clock = std::chrono::steady_clock;

namespace
{

static constexpr uint32_t DEFAULT_WIDTH = 1280;
static constexpr uint32_t DEFAULT_HEIGHT = 720;
static constexpr uint32_t DEFAULT_FPS = 60;
static constexpr const char *DEFAULT_INTERFACE = "eth0";
static constexpr uint8_t DATA_PLANE_ID = 0;
static constexpr uint8_t SENSOR_ID = 0;
static constexpr uint16_t PATTERN_FRAME_COUNT = 4;
static constexpr uint8_t UV_NEUTRAL = 128;
static constexpr uint32_t PATTERN_STEP_PIXELS = 8;

// Test-pattern header in the first luma bytes of every frame (big-endian). Keep in sync with
// tools/hsb-dev-receiver/hsb_tools/patterns.py.
static constexpr uint32_t PATTERN_HEADER_MAGIC = 0x48534254; // "HSBT"
static constexpr uint8_t PATTERN_HEADER_VERSION = 1;
static constexpr size_t PATTERN_HEADER_SIZE = 16;
static constexpr size_t BAR_COUNT = 8;
static constexpr uint64_t SPLITMIX_GOLDEN = 0x9E3779B97F4A7C15ULL;
static constexpr uint64_t SPLITMIX_MULTIPLIER_1 = 0xBF58476D1CE4E5B9ULL;
static constexpr uint64_t SPLITMIX_MULTIPLIER_2 = 0x94D049BB133111EBULL;

struct YuvColor
{
    uint8_t y;
    uint8_t u;
    uint8_t v;
};

// BT.601 limited-range 100% bars: white, yellow, cyan, green, magenta, red, blue, black.
static constexpr YuvColor BAR_COLORS[BAR_COUNT] = {
    {235, 128, 128}, {210, 16, 146}, {170, 166, 16}, {145, 54, 34},
    {106, 202, 222}, {81, 90, 240},  {41, 240, 110}, {16, 128, 128},
};

enum class PatternType : uint8_t
{
    GRADIENT = 1,
    PRBS = 2,
    BARS = 3,
};
static constexpr double BITS_PER_BYTE = 8.0;
static constexpr double MEGA = 1e6;
static constexpr uint64_t NANOSECONDS_PER_SECOND = 1'000'000'000ULL;

enum class Transport
{
    ROCE,
    COE,
};

struct SenderConfig
{
    Transport transport = Transport::ROCE;
    std::string interface_name = DEFAULT_INTERFACE;
    std::string source_ip;
    uint32_t width = DEFAULT_WIDTH;
    uint32_t height = DEFAULT_HEIGHT;
    uint32_t fps = DEFAULT_FPS;
    uint64_t frame_limit = 0;
    PatternType pattern = PatternType::PRBS;
    std::string dump_path;
};

struct IntervalStats
{
    uint64_t frames_sent = 0;
    uint64_t frames_not_sent = 0;
    uint64_t send_errors = 0;
    uint64_t late_frames = 0;
    uint64_t bytes_sent = 0;
    std::chrono::nanoseconds send_time_total{0};
    std::chrono::nanoseconds send_time_max{0};
};

std::atomic<bool> g_stop_requested{false};

void handle_signal(int)
{
    g_stop_requested = true;
}

void print_usage(const char *program_name)
{
    std::printf("Usage: %s [options]\n"
                "  -t, --transport roce|coe   data plane transport (default: roce)\n"
                "  -i, --interface NAME       interface to take the source IP from (default: eth0)\n"
                "  -a, --ip A.B.C.D           source IP (overrides --interface)\n"
                "  -W, --width N              frame width (default: 1280)\n"
                "  -H, --height N             frame height (default: 720)\n"
                "  -f, --fps N                frames per second (default: 60)\n"
                "  -n, --frames N             stop after N frames (default: run until Ctrl-C)\n"
                "  -p, --pattern NAME         prbs|gradient|bars (default: prbs)\n"
                "  -d, --dump FILE            write frame 0 of the pattern to FILE and exit\n",
                program_name);
}

std::optional<std::string> get_interface_ipv4(const std::string &interface_name)
{
    ifaddrs *interfaces = nullptr;
    if (getifaddrs(&interfaces) != 0)
    {
        return std::nullopt;
    }

    std::optional<std::string> address;
    for (ifaddrs *entry = interfaces; entry != nullptr; entry = entry->ifa_next)
    {
        if (entry->ifa_addr == nullptr || entry->ifa_addr->sa_family != AF_INET)
        {
            continue;
        }
        if (interface_name != entry->ifa_name)
        {
            continue;
        }
        char text[INET_ADDRSTRLEN] = {};
        auto *ipv4 = reinterpret_cast<sockaddr_in *>(entry->ifa_addr);
        inet_ntop(AF_INET, &ipv4->sin_addr, text, sizeof(text));
        address = text;
        break;
    }
    freeifaddrs(interfaces);
    return address;
}

size_t nv12_frame_size(uint32_t width, uint32_t height)
{
    const size_t luma_size = static_cast<size_t>(width) * height;
    return luma_size + luma_size / 2;
}

std::optional<PatternType> parse_pattern(const std::string &name)
{
    if (name == "prbs")
    {
        return PatternType::PRBS;
    }
    if (name == "gradient")
    {
        return PatternType::GRADIENT;
    }
    if (name == "bars")
    {
        return PatternType::BARS;
    }
    return std::nullopt;
}

std::optional<SenderConfig> parse_arguments(int argc, char **argv)
{
    static const option long_options[] = {
        {"transport", required_argument, nullptr, 't'}, {"interface", required_argument, nullptr, 'i'},
        {"ip", required_argument, nullptr, 'a'},        {"width", required_argument, nullptr, 'W'},
        {"height", required_argument, nullptr, 'H'},    {"fps", required_argument, nullptr, 'f'},
        {"frames", required_argument, nullptr, 'n'},    {"pattern", required_argument, nullptr, 'p'},
        {"dump", required_argument, nullptr, 'd'},      {"help", no_argument, nullptr, 'h'},
        {nullptr, 0, nullptr, 0},
    };

    SenderConfig config;
    int option_char = 0;
    while ((option_char = getopt_long(argc, argv, "t:i:a:W:H:f:n:p:d:h", long_options, nullptr)) != -1)
    {
        switch (option_char)
        {
        case 't':
            if (std::string(optarg) == "coe")
            {
                config.transport = Transport::COE;
            }
            else if (std::string(optarg) != "roce")
            {
                std::fprintf(stderr, "unknown transport: %s\n", optarg);
                return std::nullopt;
            }
            break;
        case 'i':
            config.interface_name = optarg;
            break;
        case 'a':
            config.source_ip = optarg;
            break;
        case 'W':
            config.width = std::stoul(optarg);
            break;
        case 'H':
            config.height = std::stoul(optarg);
            break;
        case 'f':
            config.fps = std::stoul(optarg);
            break;
        case 'n':
            config.frame_limit = std::stoull(optarg);
            break;
        case 'd':
            config.dump_path = optarg;
            break;
        case 'p': {
            auto pattern = parse_pattern(optarg);
            if (!pattern)
            {
                std::fprintf(stderr, "unknown pattern: %s\n", optarg);
                return std::nullopt;
            }
            config.pattern = *pattern;
            break;
        }
        case 'h':
            print_usage(argv[0]);
            std::exit(0);
        default:
            print_usage(argv[0]);
            return std::nullopt;
        }
    }

    if (config.width == 0 || config.height == 0 || config.fps == 0 || (config.height % 2) != 0 ||
        (config.width % 2) != 0 || nv12_frame_size(config.width, config.height) <= PATTERN_HEADER_SIZE)
    {
        std::fprintf(stderr, "width, height and fps must be positive and width/height must be even (NV12)\n");
        return std::nullopt;
    }

    if (!config.dump_path.empty())
    {
        return config;
    }

    if (config.source_ip.empty())
    {
        auto interface_ip = get_interface_ipv4(config.interface_name);
        if (!interface_ip)
        {
            std::fprintf(stderr, "no IPv4 address on interface %s\n", config.interface_name.c_str());
            return std::nullopt;
        }
        config.source_ip = *interface_ip;
    }
    return config;
}

// Diagonal luma ramp shifted per pattern index, so motion and tearing are visible on the receiver.
void fill_gradient(std::vector<uint8_t> &frame, uint32_t width, uint32_t height, uint16_t pattern_index)
{
    const size_t luma_size = static_cast<size_t>(width) * height;
    const uint32_t shift = static_cast<uint32_t>(pattern_index) * PATTERN_STEP_PIXELS;
    for (uint32_t row = 0; row < height; row++)
    {
        uint8_t *line = frame.data() + static_cast<size_t>(row) * width;
        for (uint32_t column = 0; column < width; column++)
        {
            line[column] = static_cast<uint8_t>(row + column + shift);
        }
    }
    std::memset(frame.data() + luma_size, UV_NEUTRAL, luma_size / 2);
}

uint64_t splitmix64(uint64_t value)
{
    value ^= value >> 30;
    value *= SPLITMIX_MULTIPLIER_1;
    value ^= value >> 27;
    value *= SPLITMIX_MULTIPLIER_2;
    value ^= value >> 31;
    return value;
}

// Stateless per-word noise: every 8-byte word is unique, so any misplaced or stale packet is detectable.
void fill_prbs(std::vector<uint8_t> &frame, uint16_t pattern_index)
{
    const uint64_t seed = (static_cast<uint64_t>(pattern_index) + 1) * SPLITMIX_GOLDEN;
    for (size_t offset = 0; offset < frame.size(); offset += sizeof(uint64_t))
    {
        uint64_t word = splitmix64(seed + offset / sizeof(uint64_t));
        for (size_t byte_index = 0; byte_index < sizeof(word) && offset + byte_index < frame.size(); byte_index++)
        {
            frame[offset + byte_index] = static_cast<uint8_t>(word >> (8 * byte_index));
        }
    }
}

void fill_bars(std::vector<uint8_t> &frame, uint32_t width, uint32_t height)
{
    const size_t luma_size = static_cast<size_t>(width) * height;
    for (uint32_t column = 0; column < width; column++)
    {
        const YuvColor &color = BAR_COLORS[column * BAR_COUNT / width];
        for (uint32_t row = 0; row < height; row++)
        {
            frame[static_cast<size_t>(row) * width + column] = color.y;
        }
    }
    // NV12 chroma: height/2 rows of interleaved U,V pairs, one pair per two luma columns.
    for (uint32_t chroma_column = 0; chroma_column < width / 2; chroma_column++)
    {
        const YuvColor &color = BAR_COLORS[(2 * chroma_column) * BAR_COUNT / width];
        for (uint32_t chroma_row = 0; chroma_row < height / 2; chroma_row++)
        {
            uint8_t *pair = frame.data() + luma_size + static_cast<size_t>(chroma_row) * width + 2 * chroma_column;
            pair[0] = color.u;
            pair[1] = color.v;
        }
    }
}

uint16_t pattern_frame_count(PatternType pattern)
{
    return pattern == PatternType::BARS ? 1 : PATTERN_FRAME_COUNT;
}

std::vector<std::vector<uint8_t>> make_pattern_frames(PatternType pattern, uint32_t width, uint32_t height)
{
    std::vector<std::vector<uint8_t>> frames(pattern_frame_count(pattern));
    for (uint16_t pattern_index = 0; pattern_index < frames.size(); pattern_index++)
    {
        auto &frame = frames[pattern_index];
        frame.resize(nv12_frame_size(width, height));
        switch (pattern)
        {
        case PatternType::GRADIENT:
            fill_gradient(frame, width, height, pattern_index);
            break;
        case PatternType::PRBS:
            fill_prbs(frame, pattern_index);
            break;
        case PatternType::BARS:
            fill_bars(frame, width, height);
            break;
        }
    }
    return frames;
}

void put_big_endian(uint8_t *destination, uint64_t value, size_t size)
{
    for (size_t byte_index = 0; byte_index < size; byte_index++)
    {
        destination[byte_index] = static_cast<uint8_t>(value >> (8 * (size - 1 - byte_index)));
    }
}

// Header: magic(4) version(1) pattern(1) reserved(2) counter(4) pattern_index(2) pattern_count(2).
void stamp_pattern_header(std::vector<uint8_t> &frame, PatternType pattern, uint32_t counter,
                          uint16_t pattern_index, uint16_t pattern_count)
{
    uint8_t *header = frame.data();
    put_big_endian(header, PATTERN_HEADER_MAGIC, 4);
    header[4] = PATTERN_HEADER_VERSION;
    header[5] = static_cast<uint8_t>(pattern);
    put_big_endian(header + 6, 0, 2);
    put_big_endian(header + 8, counter, 4);
    put_big_endian(header + 12, pattern_index, 2);
    put_big_endian(header + 14, pattern_count, 2);
}

double process_cpu_seconds()
{
    static const double clock_ticks_per_second = static_cast<double>(sysconf(_SC_CLK_TCK));
    std::ifstream stat_file("/proc/self/stat");
    std::string field;
    // utime and stime are fields 14 and 15 of /proc/self/stat
    static constexpr int UTIME_FIELD_INDEX = 14;
    unsigned long long user_ticks = 0;
    unsigned long long system_ticks = 0;
    for (int field_index = 1; field_index < UTIME_FIELD_INDEX && stat_file >> field; field_index++)
    {
    }
    stat_file >> user_ticks >> system_ticks;
    return static_cast<double>(user_ticks + system_ticks) / clock_ticks_per_second;
}

void print_interval(const IntervalStats &stats, std::chrono::duration<double> elapsed, double cpu_seconds)
{
    const double seconds = elapsed.count();
    const double average_send_us =
        stats.frames_sent ? std::chrono::duration<double, std::micro>(stats.send_time_total).count() /
                                static_cast<double>(stats.frames_sent)
                          : 0.0;
    std::printf("fps=%.1f  Mbit/s=%.1f  send_avg_us=%.0f  send_max_us=%.0f  late=%llu  not_sent=%llu  "
                "errors=%llu  cpu=%.0f%%\n",
                static_cast<double>(stats.frames_sent) / seconds,
                static_cast<double>(stats.bytes_sent) * BITS_PER_BYTE / MEGA / seconds, average_send_us,
                std::chrono::duration<double, std::micro>(stats.send_time_max).count(),
                static_cast<unsigned long long>(stats.late_frames),
                static_cast<unsigned long long>(stats.frames_not_sent),
                static_cast<unsigned long long>(stats.send_errors), 100.0 * cpu_seconds / seconds);
    std::fflush(stdout);
}

std::unique_ptr<emulation::DataPlane> make_data_plane(emulation::HSBEmulator &hsb, const SenderConfig &config)
{
    const auto source_ip = emulation::IPAddress_from_string(config.source_ip);
    if (config.transport == Transport::COE)
    {
        return std::make_unique<emulation::COEDataPlane>(hsb, source_ip, DATA_PLANE_ID, SENSOR_ID);
    }
    return std::make_unique<emulation::RoCEv2DataPlane>(hsb, source_ip, DATA_PLANE_ID, SENSOR_ID);
}

int run_sender(const SenderConfig &config)
{
    emulation::HSBEmulator hsb;
    auto data_plane = make_data_plane(hsb, config);
    auto frames = make_pattern_frames(config.pattern, config.width, config.height);
    const auto pattern_count = static_cast<uint16_t>(frames.size());
    const size_t frame_size = frames.front().size();

    std::printf("HSB synthetic sender: %s from %s, %ux%u NV12 @ %u fps, pattern %u, %.1f Mbit/s payload\n",
                config.transport == Transport::COE ? "COE" : "RoCEv2", config.source_ip.c_str(), config.width,
                config.height, config.fps, static_cast<unsigned>(config.pattern),
                static_cast<double>(frame_size) * config.fps * BITS_PER_BYTE / MEGA);

    hsb.start();

    const auto frame_interval = std::chrono::nanoseconds(NANOSECONDS_PER_SECOND / config.fps);
    auto next_deadline = steady_clock::now();
    auto interval_start = next_deadline;
    double interval_cpu_start = process_cpu_seconds();
    IntervalStats stats;

    for (uint64_t frame_number = 0; !g_stop_requested; frame_number++)
    {
        if (config.frame_limit != 0 && frame_number >= config.frame_limit)
        {
            break;
        }

        const auto pattern_index = static_cast<uint16_t>(frame_number % pattern_count);
        auto &frame = frames[pattern_index];
        stamp_pattern_header(frame, config.pattern, static_cast<uint32_t>(frame_number), pattern_index,
                             pattern_count);

        const auto capture_time = steady_clock::now();
        auto metadata = h15_hsb::make_frame_metadata(
            h15_hsb::frame_crc32(0, frame.data(), frame.size()),
            static_cast<uint64_t>(
                std::chrono::duration_cast<std::chrono::nanoseconds>(capture_time.time_since_epoch()).count()));
        const auto send_start = steady_clock::now();
        const int64_t sent_bytes = data_plane->send(frame.data(), frame.size(), &metadata);
        const auto send_time = steady_clock::now() - send_start;

        if (sent_bytes < 0)
        {
            stats.send_errors++;
        }
        else if (sent_bytes == 0)
        {
            stats.frames_not_sent++;
        }
        else
        {
            stats.frames_sent++;
            stats.bytes_sent += static_cast<uint64_t>(sent_bytes);
            stats.send_time_total += send_time;
            stats.send_time_max = std::max(stats.send_time_max, std::chrono::nanoseconds(send_time));
        }

        next_deadline += frame_interval;
        const auto now = steady_clock::now();
        if (now > next_deadline)
        {
            stats.late_frames++;
            next_deadline = now;
        }
        else
        {
            std::this_thread::sleep_until(next_deadline);
        }

        const auto elapsed = steady_clock::now() - interval_start;
        if (elapsed >= std::chrono::seconds(1))
        {
            const double cpu_now = process_cpu_seconds();
            print_interval(stats, elapsed, cpu_now - interval_cpu_start);
            stats = IntervalStats{};
            interval_start = steady_clock::now();
            interval_cpu_start = cpu_now;
        }
    }

    hsb.stop();
    return 0;
}

// Writes the first frame exactly as it would be sent, so other tools can check bit-exactness.
int dump_first_frame(const SenderConfig &config)
{
    auto frames = make_pattern_frames(config.pattern, config.width, config.height);
    stamp_pattern_header(frames.front(), config.pattern, 0, 0, static_cast<uint16_t>(frames.size()));
    std::ofstream output(config.dump_path, std::ios::binary);
    output.write(reinterpret_cast<const char *>(frames.front().data()),
                 static_cast<std::streamsize>(frames.front().size()));
    if (!output)
    {
        std::fprintf(stderr, "failed to write %s\n", config.dump_path.c_str());
        return 1;
    }
    return 0;
}

} // namespace

int main(int argc, char **argv)
{
    auto config = parse_arguments(argc, argv);
    if (!config)
    {
        return 1;
    }
    if (!config->dump_path.empty())
    {
        return dump_first_frame(*config);
    }

    std::signal(SIGINT, handle_signal);
    std::signal(SIGTERM, handle_signal);
    return run_sender(*config);
}
