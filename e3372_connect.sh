#!/bin/sh
# /data/e3372_connect.sh - E3372h link bring-up
# Part of VenusOS-E3372h package
# Called by udev when the NCM net interface appears.
# Logs to /var/log/e3372.log
#
# This script MUST NOT open a serial port.
#
# The AT dialogue (APN, AT^NDISDUP) is owned exclusively by
# dbus-modem-e3372.py, which serial-starter binds to the modem's real tty.
# Earlier versions hardcoded /dev/ttyUSB0 and sent AT commands to it. On a
# Venus OS install ttyUSB0 is almost never the modem - it is typically a
# VE.Direct adapter (MPPT, BMV), enumerated long before the modem. Writing to
# it at 115200 baud both missed the modem entirely and disturbed the VE.Direct
# service, which expects 19200 baud on that port.
#
# This script's only job now: make sure the NCM netdev is up and has a
# resident DHCP client. Dialling and recovery are the D-Bus service's job.

LOG=/var/log/e3372.log
LOCKDIR=/var/run/e3372_connect.lock
IFACE=wwan0
PIDFILE=/var/run/udhcpc.wwan0.pid

exec >> "$LOG" 2>&1

# Single instance: udev can fire several times for one hotplug, and the
# previous version raced three concurrent udhcpc against each other.
if ! mkdir "$LOCKDIR" 2>/dev/null; then
    echo "$(date) - already running, skipping (trigger: ${1:-manual})"
    exit 0
fi
trap 'rmdir "$LOCKDIR" 2>/dev/null' EXIT INT TERM

echo "$(date) - e3372_connect.sh started (trigger: ${1:-manual})"

# Wait for the net interface to show up (driver probe can lag the udev event)
i=0
while [ $i -lt 15 ]; do
    ip link show "$IFACE" > /dev/null 2>&1 && break
    sleep 1
    i=$((i + 1))
done

if ! ip link show "$IFACE" > /dev/null 2>&1; then
    echo "$(date) - $IFACE not present, nothing to do"
    exit 0
fi

ip link set "$IFACE" up
echo "$(date) - $IFACE up"

# Start a resident DHCP client. No -q: the client must stay alive to renew the
# lease and to restore the address and default route after a session drop.
# Same pidfile as dbus-modem-e3372.py, so the two never start a duplicate.
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null; then
    echo "$(date) - udhcpc already running (pid $(cat "$PIDFILE"))"
else
    rm -f "$PIDFILE"
    udhcpc -i "$IFACE" -b -p "$PIDFILE" -t 8 -T 3 -A 15 > /dev/null 2>&1 &
    echo "$(date) - udhcpc started on $IFACE"
    sleep 5
fi

ip -4 addr show "$IFACE" | grep inet
echo "$(date) - e3372_connect.sh finished"
