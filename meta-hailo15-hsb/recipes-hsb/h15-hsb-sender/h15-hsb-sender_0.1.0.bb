SUMMARY = "Hailo-15 object detection + tracking sender over Holoscan Sensor Bridge"
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

FILESEXTRAPATHS:prepend := "${THISDIR}/../../../h15:"
SRC_URI = "file://app/hsb_od_sender file://common"
S = "${WORKDIR}/app/hsb_od_sender"

DEPENDS = "hsb-emulator dlpack zlib libmedialib hailo-analytics-api hailo-postprocess-tools cxxopts protobuf"

inherit meson pkgconfig

# Emulator headers include each other by bare name.
CXXFLAGS += "-I${STAGING_INCDIR}/hololink/emulation"

do_install:append() {
    install -m 0755 ${S}/h15_hsb_make_config.py ${D}${bindir}/h15-hsb-make-config
}

RDEPENDS:${PN} += "python3-core python3-json"
