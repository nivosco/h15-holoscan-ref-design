SUMMARY = "Hailo-15 object detection + tracking sender over Holoscan Sensor Bridge"
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

FILESEXTRAPATHS:prepend := "${THISDIR}/../../../h15:"
SRC_URI = "file://app/hsb_od_sender file://common \
           file://h15-hsb-sender.init \
           file://h15-hsb-sender.default \
          "
S = "${WORKDIR}/app/hsb_od_sender"

DEPENDS = "hsb-emulator dlpack zlib libmedialib hailo-analytics-api hailo-postprocess-tools cxxopts protobuf"

inherit meson pkgconfig update-rc.d

INITSCRIPT_NAME = "h15-hsb-sender"
# After the ISP and media servers (S20) and the Hailo init script (S50).
INITSCRIPT_PARAMS = "start 95 5 . stop 05 0 1 6 ."

# Emulator headers include each other by bare name.
CXXFLAGS += "-I${STAGING_INCDIR}/hololink/emulation"

do_install:append() {
    install -m 0755 ${S}/h15_hsb_make_config.py ${D}${bindir}/h15-hsb-make-config
    install -d ${D}${sysconfdir}/init.d ${D}${sysconfdir}/default
    install -m 0755 ${WORKDIR}/h15-hsb-sender.init ${D}${sysconfdir}/init.d/h15-hsb-sender
    install -m 0644 ${WORKDIR}/h15-hsb-sender.default ${D}${sysconfdir}/default/h15-hsb-sender
}

CONFFILES:${PN} = "${sysconfdir}/default/h15-hsb-sender"

RDEPENDS:${PN} += "python3-core python3-json initscripts-functions util-linux-logger"
