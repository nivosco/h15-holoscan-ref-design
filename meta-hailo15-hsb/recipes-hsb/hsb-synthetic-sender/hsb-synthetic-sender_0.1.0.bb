SUMMARY = "Synthetic NV12 frame sender over Holoscan Sensor Bridge (Phase 0 bring-up tool)"
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

FILESEXTRAPATHS:prepend := "${THISDIR}/../../../h15/tools:"
SRC_URI = "file://hsb_synthetic_sender"
S = "${WORKDIR}/hsb_synthetic_sender"

DEPENDS = "hsb-emulator dlpack zlib"

inherit meson pkgconfig

# Emulator headers include each other by bare name.
CXXFLAGS += "-I${STAGING_INCDIR}/hololink/emulation"
