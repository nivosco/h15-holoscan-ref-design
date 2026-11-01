# BusyBox ifup assigns a static address without a broadcast address unless one is
# given, and HSB enumeration (BOOTP) is sent to the subnet broadcast address.
HSB_ETH0_BROADCAST ?= "10.0.0.255"

do_configure:append() {
    interfaces="${WORKDIR}/interfaces"
    [ -f "$interfaces" ] || return 0
    grep -q "^\s*broadcast" "$interfaces" && return 0
    sed -i "/^iface eth0 inet static/,/^iface /s/^\(\s*\)netmask\(.*\)$/&\n\1broadcast ${HSB_ETH0_BROADCAST}/" \
        "$interfaces"
}
