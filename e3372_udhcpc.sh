#!/bin/sh
# /data/e3372_udhcpc.sh - udhcpc event script for the modem's interface
# Part of VenusOS-E3372h package
#
# WHY THIS EXISTS
# udhcpc only negotiates the lease; applying it (address, default route,
# DNS) is the job of an event script. BusyBox looks for it at a fixed path,
# /usr/share/udhcpc/default.script, which up to Venus OS v3.75 came from the
# busybox-udhcpc package. Venus OS v3.85 no longer ships it: the modem dialled,
# udhcpc got its lease within a second and then had nobody to apply it, so
# wwan0 never got an address (2026-11-07 to 2026-11-09, GX offline). Every
# udhcpc this package starts now names this script with -s, so the link no
# longer depends on what the image happens to contain.
#
# Same behaviour as Yocto's simple.script, which v3.75 ran, except for DNS:
#   - address and broadcast from the lease
#   - default route through the lease's router at metric 10, so a wired or
#     WiFi connection managed by connman (metric 0) keeps priority
#   - DNS servers go to /run/ppp/resolv.conf, the file Venus OS reserves for
#     its cellular uplink: resolv-watch points dnsmasq at connman's servers
#     when connman has some and at this file otherwise. simple.script wrote
#     /etc/resolv.conf instead, which replaced the local dnsmasq proxy.

PATH=/sbin:/usr/sbin:/bin:/usr/bin
DNS_FILE=/run/ppp/resolv.conf

[ -n "$1" ] && [ -n "$interface" ] || { echo "$0: must be run by udhcpc" >&2; exit 1; }

case "$1" in
    deconfig)
        ip -4 addr flush dev "$interface"
        ip link set dev "$interface" up
        [ -d "${DNS_FILE%/*}" ] && : > "$DNS_FILE"
        ;;

    bound|renew)
        # A renew usually keeps the address; only replace it when it changed.
        if ! ip -4 addr show dev "$interface" | grep -q "inet $ip/$mask "; then
            ip -4 addr flush dev "$interface"
            ip addr add "$ip/$mask" broadcast "${broadcast:-+}" dev "$interface"
        fi

        if [ -n "$router" ]; then
            while ip route del default dev "$interface" 2>/dev/null; do :; done
            metric=10
            for r in $router; do
                ip route add default via "$r" dev "$interface" metric $metric
                metric=$((metric + 1))
            done
        fi

        # Written in place (not renamed): resolv-watch waits for close_write.
        if [ -n "$dns" ]; then
            mkdir -p "${DNS_FILE%/*}"
            {
                [ -n "$domain" ] && echo "domain $domain"
                for s in $dns; do echo "nameserver $s"; done
            } > "$DNS_FILE"
        fi
        ;;
esac

exit 0
