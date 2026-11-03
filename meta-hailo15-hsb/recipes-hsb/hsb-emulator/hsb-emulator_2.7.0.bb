SUMMARY = "NVIDIA Holoscan Sensor Bridge emulator (CUDA-free build for Hailo-15)"
HOMEPAGE = "https://github.com/nvidia-holoscan/holoscan-sensor-bridge"
LICENSE = "Apache-2.0"
LIC_FILES_CHKSUM = "file://${WORKDIR}/git/LICENSE;md5=86d3f3a95c324c9479bd8986968f4327"

SRC_URI = "git://github.com/nvidia-holoscan/holoscan-sensor-bridge.git;protocol=https;nobranch=1 \
           file://0001-emulation-make-CUDA-optional-for-the-linux-target.patch;patchdir=${WORKDIR}/git \
           file://0002-emulation-toolchain-portability-fixes.patch;patchdir=${WORKDIR}/git \
           file://0003-emulation-install-all-headers-needed-by-out-of-tree-.patch;patchdir=${WORKDIR}/git \
           file://0004-emulation-use-ARMv8-CRC32-instructions-for-the-RoCEv.patch;patchdir=${WORKDIR}/git \
           file://0005-emulation-let-callers-set-flags-crc-and-the-frame-ti.patch;patchdir=${WORKDIR}/git \
          "
# tag 2.7.0
SRCREV = "1df133d4c78a6921249be4b147d65554aa8efa68"

S = "${WORKDIR}/git/src/hololink/emulation"

DEPENDS = "dlpack zlib"

inherit cmake

EXTRA_OECMAKE = " \
    -DHSB_EMULATOR_TARGET=linux \
    -DHSB_EMULATOR_ENABLE_CUDA=OFF \
    -DHSB_EMULATOR_BUILD_PYTHON=OFF \
    -DCMAKE_CXX_STANDARD=20 \
"

HSB_EMULATOR_EXAMPLES = "hsb_control serve_roce_msg serve_coe_msg"

do_install:append() {
    install -d ${D}${bindir}
    for example in ${HSB_EMULATOR_EXAMPLES}; do
        installed_name=$(echo ${example} | sed -e 's/^hsb_//' -e 's/_/-/g')
        install -m 0755 ${B}/examples/${example} ${D}${bindir}/hsb-${installed_name}
    done
}

PACKAGES =+ "${PN}-examples"
FILES:${PN}-examples = "${bindir}/hsb-*"
ALLOW_EMPTY:${PN} = "1"
