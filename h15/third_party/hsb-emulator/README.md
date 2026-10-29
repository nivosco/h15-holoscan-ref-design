# HSB Emulator for Hailo-15

The Hailo-15 sender uses the
[NVIDIA Holoscan Sensor Bridge](https://github.com/nvidia-holoscan/holoscan-sensor-bridge)
HSB Emulator (Apache-2.0) to send video over the HSB protocol.

| Item | Value |
|---|---|
| Upstream | https://github.com/nvidia-holoscan/holoscan-sensor-bridge |
| Version | `2.7.0` |
| Source dir | `src/hololink/emulation` |

The patches in [`meta-hailo15-hsb/recipes-hsb/hsb-emulator/files/`](../../../meta-hailo15-hsb/recipes-hsb/hsb-emulator/files/) apply on top of tag `2.7.0`:

1. `0001` adds `HSB_EMULATOR_ENABLE_CUDA` (default ON). Set it to OFF to build for hosts that have no CUDA toolkit, such as Hailo-15. Host-memory frames work as usual; GPU-resident frames are rejected.
2. `0002` adds toolchain portability fixes: the `sockaddr_in` initializer and explicit `Threads` linkage.
3. `0003` installs every header that an application built outside the emulator's tree needs.
4. `0004` computes the RoCEv2 iCRC with ARMv8 CRC32 instructions when the CPU has them. This cuts sender CPU on Cortex-A53.

Build the emulator standalone (emulator only, no Python):

```bash
git clone --branch 2.7.0 https://github.com/nvidia-holoscan/holoscan-sensor-bridge.git
cd holoscan-sensor-bridge && git am /path/to/meta-hailo15-hsb/recipes-hsb/hsb-emulator/files/*.patch
cmake -S src/hololink/emulation -B build \
    -DHSB_EMULATOR_ENABLE_CUDA=OFF -DHSB_EMULATOR_BUILD_PYTHON=OFF \
    -DCMAKE_CXX_STANDARD=20 -DCMAKE_BUILD_TYPE=Release
cmake --build build
```

The `hsb-emulator` Yocto recipe in `meta-hailo15-hsb` applies them automatically.
