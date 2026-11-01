#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Microchip Technology Inc. and its subsidiaries.
#
# Data-correctness test matrix for the Hailo-15 HSB sender, run from a Linux host (or WSL)
# connected to the board. No Jetson is needed.
#
#   1. golden      sender's pattern dump on the board == Python reference (bit-exact)
#   2. roce-live   live RoCEv2 receive at 30 fps, every complete frame verified, loss reported
#   3. roce-pcap   capture on the board, offline check incl. per-packet iCRC
#   4. coe-pcap    COE (IEEE 1722B) capture on the board, offline check
#
# Usage: scripts/run_data_tests.sh [-b BOARD_IP] [-s SECONDS] [-o OUTPUT_DIR]

set -euo pipefail

BOARD_IP=10.0.0.1
SECONDS_PER_TEST=15
OUTPUT_DIR="data-test-results/$(date +%Y%m%d-%H%M%S)"
CAPTURE_FPS=10
# A Python receiver on a laptop drops packets at 720p60; data checks do not need the rate.
LIVE_FPS=30
CAPTURE_PACKETS=20000
PATTERNS=(prbs gradient bars)

while getopts "b:s:o:h" option; do
    case "${option}" in
        b) BOARD_IP="${OPTARG}" ;;
        s) SECONDS_PER_TEST="${OPTARG}" ;;
        o) OUTPUT_DIR="${OPTARG}" ;;
        *) sed -n '5,14p' "$0"; exit 2 ;;
    esac
done

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TOOLS="${REPO_ROOT}/tools/hsb-dev-receiver"
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=5 "root@${BOARD_IP}")
mkdir -p "${OUTPUT_DIR}"
OUTPUT_DIR="$(cd "${OUTPUT_DIR}" && pwd)"
declare -A RESULTS=()

log() { printf '\n=== %s\n' "$*"; }
record() { RESULTS["$1"]="$2"; printf '%-22s %s\n' "$1" "$2"; }

host_mac() {
    local interface
    interface="$(ip route get "${BOARD_IP}" | sed -n 's/.* dev \([^ ]*\).*/\1/p')"
    cat "/sys/class/net/${interface}/address"
}

start_sender() {
    # Runs the sender in the background on the board for the given number of seconds.
    local seconds="$1"
    shift
    "${SSH[@]}" "timeout -s INT ${seconds} hsb-synthetic-sender $*" \
        > "${OUTPUT_DIR}/sender_${TEST_NAME}.log" 2>&1 &
    SENDER_PID=$!
    sleep 2
}

capture_on_board() {
    local filter="$1"
    "${SSH[@]}" "rm -f /tmp/hsb.pcap; timeout 60 tcpdump -i eth0 -s 0 -B 32768 \
        -c ${CAPTURE_PACKETS} -w /tmp/hsb.pcap '${filter}' 2>&1 | tail -3" \
        > "${OUTPUT_DIR}/tcpdump_${TEST_NAME}.log"
    scp -q -o BatchMode=yes "root@${BOARD_IP}:/tmp/hsb.pcap" "${OUTPUT_DIR}/${TEST_NAME}.pcap"
}

pass_fail() { if [[ "$1" -eq 0 ]]; then echo PASS; else echo FAIL; fi; }

log "preflight: ${BOARD_IP}"
"${SSH[@]}" 'ifconfig eth0 broadcast 10.0.0.255 >/dev/null 2>&1 || true;
    command -v hsb-synthetic-sender >/dev/null && command -v tcpdump >/dev/null' \
    || { echo "board unreachable or tools missing"; exit 1; }
HOST_MAC="$(host_mac)"
echo "host MAC ${HOST_MAC}, results in ${OUTPUT_DIR}"

log "1. golden pattern dumps (aarch64 sender vs Python reference)"
for pattern in "${PATTERNS[@]}"; do
    board_hash="$("${SSH[@]}" "hsb-synthetic-sender --pattern ${pattern} --dump /tmp/dump.nv12 \
        && sha256sum /tmp/dump.nv12" | cut -d' ' -f1)"
    python_hash="$(cd "${TOOLS}" && python3 -c "
import hashlib, sys
sys.path.insert(0, '.')
from hsb_tools.patterns import PatternType, build_frame
print(hashlib.sha256(build_frame(PatternType['${pattern^^}'], 0, 1280, 720)).hexdigest())")"
    if [[ -n "${board_hash}" && "${board_hash}" == "${python_hash}" ]]; then
        record "golden-${pattern}" PASS
    else
        record "golden-${pattern}" FAIL
    fi
done

log "2. live RoCEv2 at ${LIVE_FPS} fps: every complete frame verified, loss reported"
for pattern in "${PATTERNS[@]}"; do
    TEST_NAME="roce-live-${pattern}"
    start_sender $((SECONDS_PER_TEST + 6)) --pattern "${pattern}" --fps "${LIVE_FPS}"
    set +e
    (cd "${TOOLS}" && python3 hsb_dev_receiver.py --check-crc --device-ip "${BOARD_IP}" \
        --seconds "${SECONDS_PER_TEST}" --report "${OUTPUT_DIR}/${TEST_NAME}.json" \
        --save-dir "${OUTPUT_DIR}/frames-${pattern}" --save-every 300 --save-failed --allow-loss \
        > "${OUTPUT_DIR}/${TEST_NAME}.log" 2>&1)
    status=$?
    set -e
    wait "${SENDER_PID}" || true
    record "${TEST_NAME}" "$(pass_fail ${status})"
done

log "3. RoCEv2 capture on the board (${CAPTURE_FPS} fps), offline check with iCRC"
TEST_NAME="roce-pcap"
start_sender 40 --pattern prbs --fps "${CAPTURE_FPS}"
(cd "${TOOLS}" && python3 hsb_dev_receiver.py --check-crc --device-ip "${BOARD_IP}" --seconds 30 \
    --no-verify > "${OUTPUT_DIR}/${TEST_NAME}_receiver.log" 2>&1) &
RECEIVER_PID=$!
sleep 4
capture_on_board "udp src port 12288"
wait "${RECEIVER_PID}" || true
wait "${SENDER_PID}" || true
set +e
(cd "${TOOLS}" && python3 hsb_pcap_check.py --check-crc "${OUTPUT_DIR}/${TEST_NAME}.pcap" \
    --report "${OUTPUT_DIR}/${TEST_NAME}.json" > "${OUTPUT_DIR}/${TEST_NAME}.log" 2>&1)
record "${TEST_NAME}" "$(pass_fail $?)"
set -e

log "4. COE capture on the board (${CAPTURE_FPS} fps), offline check"
for pattern in prbs bars; do
    TEST_NAME="coe-pcap-${pattern}"
    start_sender 40 --transport coe --pattern "${pattern}" --fps "${CAPTURE_FPS}"
    (cd "${TOOLS}" && python3 hsb_dev_receiver.py --check-crc --device-ip "${BOARD_IP}" \
        --transport coe --host-mac "${HOST_MAC}" --seconds 30 \
        > "${OUTPUT_DIR}/${TEST_NAME}_host.log" 2>&1) &
    HOST_PID=$!
    sleep 4
    capture_on_board "ether proto 0x22f0"
    wait "${HOST_PID}" || true
    wait "${SENDER_PID}" || true
    set +e
    (cd "${TOOLS}" && python3 hsb_pcap_check.py --check-crc "${OUTPUT_DIR}/${TEST_NAME}.pcap" \
        --transport coe --host-mac "${HOST_MAC}" --save-dir "${OUTPUT_DIR}/frames-${TEST_NAME}" \
        --save-every 100 --report "${OUTPUT_DIR}/${TEST_NAME}.json" \
        > "${OUTPUT_DIR}/${TEST_NAME}.log" 2>&1)
    record "${TEST_NAME}" "$(pass_fail $?)"
    set -e
done

log "summary"
failures=0
for name in $(printf '%s\n' "${!RESULTS[@]}" | sort); do
    printf '%-22s %s\n' "${name}" "${RESULTS[${name}]}"
    [[ "${RESULTS[${name}]}" == PASS ]] || failures=$((failures + 1))
done
echo "results: ${OUTPUT_DIR}"
exit $((failures > 0))
