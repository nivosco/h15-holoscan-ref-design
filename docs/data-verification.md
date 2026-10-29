# Data verification without a Jetson

These tools check that the Hailo-15 sender emits exactly the right bytes, in the right order and
framing, with correct metadata. They need only a Linux PC (or WSL) on the same link as the board.
Throughput and latency on a PC are not representative of the Jetson and are not pass criteria.

## Components

| Component | Where | Role |
|---|---|---|
| `hsb-synthetic-sender` | board | Sends NV12 test patterns over HSB (RoCEv2 or COE) using the HSB Emulator |
| `hsb_dev_receiver.py` | PC | Minimal HSB host: discovers the board, configures the stream, verifies RoCEv2 frames live |
| `hsb_pcap_check.py` | PC | Verifies a pcap/pcapng capture of RoCEv2 or COE traffic offline |
| `scripts/run_data_tests.sh` | PC | Runs the full test matrix and prints a PASS/FAIL table |

## Test patterns

Every frame starts with a 16-byte big-endian header, followed by the pattern:

| Offset | Size | Field |
|---|---|---|
| 0 | 4 | magic `HSBT` |
| 4 | 1 | version (1) |
| 5 | 1 | pattern: 1 gradient, 2 prbs, 3 bars |
| 6 | 2 | reserved |
| 8 | 4 | sender frame counter |
| 12 | 2 | pattern index (counter mod pattern count) |
| 14 | 2 | pattern count |

| Pattern | Purpose |
|---|---|
| `prbs` | Every 8-byte word is unique (stateless splitmix64), so any misplaced, duplicated or stale packet changes the frame |
| `gradient` | Moving diagonal ramp; tearing and offset errors are visible |
| `bars` | BT.601 100% color bars; checks NV12 plane order and U/V order |

The receiver regenerates the expected frame from the header and compares every byte. The Python
generator is bit-exact with the C++ sender (`hsb-synthetic-sender --dump FILE`), which the test
suite and the `golden-*` tests check.

## What is checked

Per packet, RoCEv2: PSN continuity (20-bit, as the emulator sends it), QP and rkey, write offset
contiguity and bounds, packet size, metadata placement at `roundup(frame_size, 128)`, zero padding,
and (offline) the iCRC over the masked IP/UDP pseudo-header.

Per packet, COE: PSN continuity (7-bit), destination MAC, ACF type, channel, stream ID,
FRAME_START/FRAME_END flags, offset contiguity, middle-packet size, 2-bit header frame number
against the metadata frame number, zero padding.

Per frame: `bytes_written` equals the frame size, every byte arrived once, metadata PSN matches the
packet, frame number continuity (lost frames), sender counter in step with the frame number,
timestamps monotonic, metadata timestamp not before frame start, frame period mean and jitter, and
the content check above.

## Running

```bash
# One-time on the PC: python3 -m pip install numpy opencv-python pytest
scripts/run_data_tests.sh -b 10.0.0.1            # full matrix, results in data-test-results/
```

| Test | What it proves |
|---|---|
| `golden-<pattern>` | The sender on the board generates exactly the reference pattern |
| `roce-live-<pattern>` | Frames received live over RoCEv2 are byte-exact; loss is reported, not failed (`--allow-loss`) |
| `roce-pcap` | Every packet the board sends is well formed, every iCRC is correct, every frame is byte-exact |
| `coe-pcap-<pattern>` | Same for COE (IEEE 1722B), the AGX Thor transport |

Captures are taken on the board with `tcpdump` at 10 fps so the capture itself does not drop
packets (`tcpdump_*.log` reports kernel drops).

Individual tools:

```bash
cd tools/hsb-dev-receiver
python3 hsb_dev_receiver.py --show                         # live view with OK/FAILED overlay
python3 hsb_dev_receiver.py --seconds 30 --report r.json --save-dir frames --allow-loss
python3 hsb_dev_receiver.py --transport coe --host-mac aa:bb:cc:dd:ee:ff --seconds 30
python3 hsb_pcap_check.py capture.pcap --transport coe --save-dir frames
python3 -m pytest                                          # unit tests, no hardware needed
```

## Known emulator behaviours (HSB 2.7.0)

These match NVIDIA's emulator and receivers and are accepted by the checker:

- COE NTSCF data length, ACF message length and NTSCF sequence number are always 0.
- COE `LINE_END` (0x10) is set when a byte counter reaches `2^ceil(log2(bytes_per_line))` and then
  resets, so it marks packet boundaries rather than image lines. Check against the AGX Thor
  hardware COE capture path before relying on it.
- The emulator numbers frames from the first frame actually sent; the sender's own counter also
  counts frames dropped before the host configured the stream, so only a change in their offset is
  an error.
- A caller-provided `FrameMetadata` is sent as raw bytes and must already be big-endian.
