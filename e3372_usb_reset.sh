#!/bin/sh
# /data/e3372_usb_reset.sh - USB-level reset of the Huawei E3372h
# Part of VenusOS-E3372h package
#
# De-authorizes then re-authorizes the modem's USB device, which is what a
# physical unplug/replug does. Last rung of the recovery ladder of
# dbus-modem-e3372.py, which launches this script detached: as soon as the
# device is de-authorized its tty disappears and serial-starter kills the
# modem service, so the service itself could never write the "1" back.
#
# Usage: e3372_usb_reset.sh [/sys/bus/usb/devices/X-Y]
# The sysfs path is rediscovered when missing or stale. Logs to /var/log/e3372.log.

LOG=/var/log/e3372.log
PIDFILE=/var/run/udhcpc.wwan0.pid
DEV="$1"

exec >> "$LOG" 2>&1

find_dev() {
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

if [ -z "$DEV" ] || [ ! -f "$DEV/authorized" ]; then
    DEV=$(find_dev)
    if [ -z "$DEV" ]; then
        echo "$(date) - usb_reset: E3372h not found in sysfs, nothing to do"
        exit 1
    fi
fi

echo "$(date) - usb_reset: de-authorizing $DEV"
echo 0 > "$DEV/authorized"
sleep 5

# The DHCP client of the vanished interface would otherwise keep the pidfile
# and block the relaunch by e3372_connect.sh.
if [ -f "$PIDFILE" ]; then
    kill "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null
    rm -f "$PIDFILE"
fi

echo "$(date) - usb_reset: re-authorizing $DEV"
echo 1 > "$DEV/authorized"
sleep 15

echo "$(date) - usb_reset: done, ttys: $(ls /dev/ttyUSB* 2>/dev/null | tr '\n' ' ')wwan0 addresses: $(ip -4 addr show wwan0 2>/dev/null | grep -c inet)"
