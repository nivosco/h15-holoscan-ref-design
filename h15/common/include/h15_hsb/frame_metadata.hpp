// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Microchip Technology Inc. and its subsidiaries.
//
// Per-frame integrity data carried in the HSB FrameMetadata: a CRC-32 (IEEE, as zlib) of the
// frame payload in transmit order, and the frame timestamp. Receivers recompute the CRC to
// verify live frames whose content is not known in advance.
#pragma once

#include "hololink/emulation/base_transmitter.hpp"

#include <arm_acle.h>
#include <sys/auxv.h>
#include <zlib.h>

#include <asm/hwcap.h>
#include <cstddef>
#include <cstdint>
#include <cstring>

namespace h15_hsb
{

static constexpr uint64_t NANOSECONDS_PER_SECOND = 1000000000ULL;
static constexpr unsigned UPPER_WORD_SHIFT = 32;

__attribute__((target("+crc"))) inline uint32_t crc32_armv8(uint32_t crc, const uint8_t *data, size_t length)
{
    crc = ~crc;
    while (length >= sizeof(uint64_t))
    {
        uint64_t word;
        std::memcpy(&word, data, sizeof(word));
        crc = __crc32d(crc, word);
        data += sizeof(word);
        length -= sizeof(word);
    }
    while (length--)
    {
        crc = __crc32b(crc, *data++);
    }
    return ~crc;
}

inline bool cpu_has_crc32()
{
    static const bool has_crc32 = (getauxval(AT_HWCAP) & HWCAP_CRC32) != 0;
    return has_crc32;
}

/**
 * @brief Continues a CRC-32 over the next part of a frame; start from 0.
 *
 * Bit-identical to zlib's crc32(), so receivers can verify with zlib.crc32.
 */
inline uint32_t frame_crc32(uint32_t crc, const uint8_t *data, size_t length)
{
    if (cpu_has_crc32())
    {
        return crc32_armv8(crc, data, length);
    }
    return static_cast<uint32_t>(crc32_z(crc, data, length));
}

/**
 * @brief Builds the caller-owned part of the HSB FrameMetadata, in host byte order.
 *
 * The emulator fills psn, frame_number, bytes_written and the metadata timestamp.
 */
inline hololink::emulation::FrameMetadata make_frame_metadata(uint32_t crc, uint64_t timestamp_ns)
{
    hololink::emulation::FrameMetadata metadata{};
    const uint64_t seconds = timestamp_ns / NANOSECONDS_PER_SECOND;
    metadata.crc = crc;
    metadata.timestamp_s_high = static_cast<uint32_t>(seconds >> UPPER_WORD_SHIFT);
    metadata.timestamp_s_low = static_cast<uint32_t>(seconds);
    metadata.timestamp_ns = static_cast<uint32_t>(timestamp_ns % NANOSECONDS_PER_SECOND);
    return metadata;
}

} // namespace h15_hsb
