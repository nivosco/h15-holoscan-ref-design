SUMMARY = "DLPack: open in-memory tensor structure (header only)"
HOMEPAGE = "https://github.com/dmlc/dlpack"
LICENSE = "Apache-2.0"
LIC_FILES_CHKSUM = "file://LICENSE;md5=f62d4e85ba68a1574b74d97ab8dea9ab"

SRC_URI = "git://github.com/dmlc/dlpack.git;protocol=https;branch=main"
SRCREV = "bbd2f4d32427e548797929af08cfe2a9cbb3cf12"

S = "${WORKDIR}/git"

do_configure[noexec] = "1"
do_compile[noexec] = "1"

do_install() {
    install -d ${D}${includedir}/dlpack
    install -m 0644 ${S}/include/dlpack/dlpack.h ${D}${includedir}/dlpack/
}

ALLOW_EMPTY:${PN} = "1"
BBCLASSEXTEND = "native nativesdk"
