#!/bin/bash
# /data/e3372_connect.sh — E3372h NCM data connection setup
# Part of VenusOS-E3372h package
# Called by udev on wwan0 interface appearing AND at boot
# Logs to /var/log/e3372.log

LOG="/var/log/e3372.log"
exec >> "$LOG" 2>&1
echo "$(date) — e3372_connect.sh started (trigger: ${1:-manual})"

# Load config
APN="mmsbouygtel.com"
if [ -f /data/e3372-config.conf ]; then
  . /data/e3372-config.conf
fi

# Wait for modem to be ready
sleep 5

# Establish NCM data connection via AT commands
if [ -e /dev/ttyUSB0 ]; then
  python3 << PYEOF
import serial, time, sys

apn = "$APN"

try:
    port = serial.Serial('/dev/ttyUSB0', 115200, timeout=3)
except Exception as e:
    print(f"Cannot open port (dbus-modem may have it): {e}")
    print("NCM may already be active, trying DHCP anyway")
    sys.exit(0)

time.sleep(0.5)

def at(cmd, delay=2):
    port.reset_input_buffer()
    port.write((cmd + '\r').encode())
    time.sleep(delay)
    resp = port.read(port.in_waiting or 256).decode(errors='replace').strip()
    print(f'  {cmd} -> {resp}')
    return resp

at('AT')
at(f'AT+CGDCONT=1,"IP","{apn}"')
time.sleep(1)
at(f'AT^NDISDUP=1,1,"{apn}"', delay=3)
time.sleep(2)
r = at('AT^NDISSTATQRY?')
port.close()
print('NCM AT sequence complete')
PYEOF
fi

# Bring up interface and get DHCP
for iface in wwan0 usb0; do
  if ip link show "$iface" > /dev/null 2>&1; then
    echo "$(date) — Bringing up $iface"
    ip link set "$iface" up
    kill $(cat /var/run/udhcpc."$iface".pid 2>/dev/null) 2>/dev/null
    udhcpc -i "$iface" -b -p /var/run/udhcpc."$iface".pid -q
    echo "$(date) — DHCP done on $iface"
    ip addr show "$iface" | grep inet
    break
  fi
done

echo "$(date) — e3372_connect.sh finished"
