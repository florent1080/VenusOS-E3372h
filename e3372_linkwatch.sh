#!/bin/sh
# /data/e3372_linkwatch.sh - last-resort link watchdog for the E3372h
# Part of VenusOS-E3372h package
#
# WHY THIS EXISTS
# The recovery ladder of dbus-modem-e3372.py talks to the modem through its
# tty (and /dev/cdc-wdm0). Both vanish when the modem leaves the USB bus, and
# serial-starter then kills the service - so the very component that knows how
# to reset the USB port is gone exactly when that reset is needed. A modem
# that drops off the bus (or that a command such as AT+CFUN=0 switched off
# before the follow-up could be sent) would stay dead until someone unplugs it.
#
# This script is that missing piece: it is started detached by the modem
# service (new session, no inherited descriptor), so it survives the service
# being killed, and it only ever does one thing - power-cycle the modem's USB
# port when the link has been dead for a long time.
#
# It is deliberately dumb and slow: one check every few minutes, one action per
# half hour at most, and nothing at all while the data session is alive.

CONF=/data/e3372-config.conf
LOG=/data/log/e3372/linkwatch.log
PIDFILE=/var/run/e3372-linkwatch.pid
USB_RESET=/data/e3372_usb_reset.sh
IFACE=wwan0

CHECK_INTERVAL=60          # seconds between checks
DOWN_BEFORE_ACT=900        # link must be dead this long before acting
MIN_GAP=1800               # never act more often than this
HCI_RESET=0                # rebind the USB controller when the modem is gone

[ -f "$CONF" ] && . "$CONF" 2>/dev/null
[ -n "$LINKWATCH_DOWN_SECONDS" ] && DOWN_BEFORE_ACT=$LINKWATCH_DOWN_SECONDS
[ -n "$LINKWATCH_HCI_RESET" ] && HCI_RESET=$LINKWATCH_HCI_RESET

mkdir -p /data/log/e3372
say() { echo "$(date) - $*" >> "$LOG"; }

# Single instance, even across restarts of the modem service.
if [ -f "$PIDFILE" ]; then
    old=$(cat "$PIDFILE" 2>/dev/null)
    if [ -n "$old" ] && [ -d "/proc/$old" ]; then
        exit 0
    fi
fi
echo $$ > "$PIDFILE"
trap 'rm -f "$PIDFILE"' EXIT INT TERM

say "linkwatch started (pid $$, act after ${DOWN_BEFORE_ACT}s, hci_reset=$HCI_RESET)"

modem_sysfs() {
    for f in /sys/bus/usb/devices/*/idProduct; do
        [ -f "$f" ] || continue
        d=$(dirname "$f")
        if grep -q '^1506$' "$f" 2>/dev/null && grep -q '^12d1$' "$d/idVendor" 2>/dev/null; then
            echo "$d"
            return 0
        fi
    done
    return 1
}

connect_wanted() {
    # Respect the UI switch. When the D-Bus answer is unavailable (the service
    # may be dead, which is precisely the case we are here for), assume yes.
    v=$(dbus -y com.victronenergy.settings /Settings/Modem/Connect GetValue 2>/dev/null)
    [ "$v" = "0" ] && return 1
    return 0
}

link_alive() {
    # An address plus a default route through wwan0 is the cheapest proof that
    # the data session exists. The address alone is not: it survives a drop.
    ip -4 addr show "$IFACE" 2>/dev/null | grep -q 'inet ' || return 1
    ip route 2>/dev/null | grep -q "^default.*dev $IFACE" || return 1
    return 0
}

hci_reset() {
    # The modem is not on the bus any more: rebind its USB controller, which is
    # the closest thing to re-plugging the cable. This also re-enumerates the
    # other devices of that controller, so it stays opt-in.
    drv=/sys/bus/platform/drivers/xhci-hcd
    [ -d "$drv" ] || { say "no xhci-hcd driver, cannot rebind"; return 1; }
    for hci in $(ls "$drv" 2>/dev/null | grep -E '^xhci-hcd'); do
        say "rebinding USB controller $hci"
        echo "$hci" > "$drv/unbind" 2>/dev/null
        sleep 5
        echo "$hci" > "$drv/bind" 2>/dev/null
        sleep 20
        if modem_sysfs > /dev/null; then
            say "modem is back on the bus after rebinding $hci"
            return 0
        fi
    done
    return 1
}

down_since=0
last_action=0
now=0

while true; do
    sleep "$CHECK_INTERVAL"
    now=$((now + CHECK_INTERVAL))

    if ! connect_wanted; then
        down_since=0
        continue
    fi

    if link_alive; then
        if [ "$down_since" != 0 ]; then
            say "link is back after $((now - down_since))s"
        fi
        down_since=0
        continue
    fi

    [ "$down_since" = 0 ] && { down_since=$now; say "link down, watching"; }
    [ $((now - down_since)) -lt "$DOWN_BEFORE_ACT" ] && continue
    [ "$last_action" != 0 ] && [ $((now - last_action)) -lt "$MIN_GAP" ] && continue

    last_action=$now
    dev=$(modem_sysfs)
    if [ -n "$dev" ]; then
        say "link dead for $((now - down_since))s, resetting the USB port ($dev)"
        [ -x "$USB_RESET" ] && "$USB_RESET" "$dev"
    else
        say "link dead for $((now - down_since))s and the modem is not on the USB bus"
        if [ "$HCI_RESET" = "1" ]; then
            hci_reset
        else
            say "set LINKWATCH_HCI_RESET=1 in $CONF to rebind the USB controller"
        fi
    fi
    # Give the modem time to enumerate and the service time to dial before the
    # next judgement.
    down_since=$now
done
