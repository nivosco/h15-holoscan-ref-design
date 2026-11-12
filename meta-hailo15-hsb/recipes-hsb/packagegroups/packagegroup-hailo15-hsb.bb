SUMMARY = "Hailo-15 Holoscan Sensor Bridge reference design"
LICENSE = "MIT"

inherit packagegroup

RDEPENDS:${PN} = " \
    h15-hsb-sender \
    hsb-emulator-examples \
    hsb-synthetic-sender \
"
