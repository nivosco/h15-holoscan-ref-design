// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Microchip Technology Inc. and its subsidiaries.
//
// Small Linux helpers shared by the Hailo-15 HSB senders.
#pragma once

#include <arpa/inet.h>
#include <ifaddrs.h>
#include <netinet/in.h>
#include <unistd.h>

#include <fstream>
#include <optional>
#include <string>

namespace h15_hsb
{

/**
 * @brief Returns the first IPv4 address assigned to an interface.
 */
inline std::optional<std::string> get_interface_ipv4(const std::string &interface_name)
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

/**
 * @brief Returns the user plus system CPU time consumed by this process, in seconds.
 */
inline double process_cpu_seconds()
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

} // namespace h15_hsb
