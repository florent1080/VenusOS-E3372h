#!/bin/sh
# /data/e3372_usb_reset.sh - host-side re-enumeration of the E3372h
# Part of VenusOS-E3372h package
#
# WHAT THIS IS, AND WHAT IT IS NOT
# This is a BUS reset, not a power cycle, and on this board it never will be.
# Measured on 2026-09-22 (Raspberry Pi 5, xhci-hcd.1): the kernel really does
# clear port power (PORTSC reads "Powered-off Not-connected Disabled") and the
# device really leaves the bus - and the modem still came back 0.5 s later with
# its firmware state intact (AT+CSCS still showed the value set before the
# cycle). RP1's port-power output is not wired to a load switch. So
# de-authorising, disabling the port and rebinding the controller are three
# strengths of the same host-side act, and NONE of them changes anything
# inside the modem. Use this when the driver or the enumeration is wedged; use
# AT^RESET when the modem itself is.
#
# Usage:
#   e3372_usb_reset.sh portcycle [<device sysfs path>]
#   e3372_usb_reset.sh rebind    [<device sysfs path>] [<xhci-hcd.N>]
#   e3372_usb_reset.sh                 (defaults to portcycle)
#
# The modem service launches this detached, because the first write makes the
# tty disappear and serial-starter then kills the service: nothing else would
# be left to write the port back.

CONF=/data/e3372-config.conf
LOG=/data/log/e3372/usbreset.log
LINKLOG=/var/log/e3372.log
PIDFILE=/var/run/udhcpc.wwan0.pid
INTENT=/run/e3372-intent.json
BOOT_ID_FILE=/proc/sys/kernel/random/boot_id
UPTIME_FILE=/proc/uptime
GUARD_AFTER=60          # a forked child re-enables the port after this
SETTLE=5                # the cut itself; longer is pointless, it is not power
WAIT_BACK=30            # how long we wait for the device to come back

ACTION=${1:-portcycle}
DEV=$2
HCI=$3

mkdir -p /data/log/e3372 2>/dev/null
say() {
    echo "$(date) - usb_reset[$ACTION]: $*" >> "$LOG"
    echo "$(date) - usb_reset[$ACTION]: $*" >> "$LINKLOG"
}

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

# The port directory is found by resolving each candidate's 'device' symlink
# and comparing it with the modem's own resolved path - never by name, which
# differs behind an external hub.
find_port() {
    rp=$(readlink -f "$1")
    hub=$(dirname "$rp")
    for p in "$hub"/*/*-port*; do
        [ -f "$p/disable" ] || continue
        [ "$(readlink -f "$p/device" 2>/dev/null)" = "$rp" ] || continue
        echo "$p"
        return 0
    done
    return 1
}

# Read one numeric key from the user-editable config without sourcing it.
conf_get() {
    v=$(sed -n "s/^[ 	]*$1[ 	]*=[ 	]*//p" "$CONF" 2>/dev/null \
        | tail -n 1 | tr -d '\r' | sed 's/^"//; s/"$//')
    case "$v" in
        ''|*[!0-9]*) echo "$2" ;;
        *)           echo "$v" ;;
    esac
}

# Every USB device under a controller, other than the modem itself and the
# root hubs. Rebinding the controller re-enumerates all of them: on the
# reference install that includes the VE.Direct cable of the BMV-712, the
# active battery service. Walks the controller's own subtree, so it needs no
# symlink to be followed.
hci_collateral() {
    hdir=$1
    self=$2
    [ -d "$hdir" ] || return 0
    find "$hdir" -name idVendor 2>/dev/null | while read -r f; do
        d=$(dirname "$f")
        [ "$d" = "$self" ] && continue
        [ "$(cat "$f" 2>/dev/null)" = "1d6b" ] && continue     # root hub
        echo "$(basename "$d") ($(cat "$d/product" 2>/dev/null))"
    done
}

find_hci() {
    readlink -f "$1" | sed -n 's#.*/\(xhci-hcd\.[0-9]*\)/.*#\1#p'
}

write_intent() {
    # Written BEFORE anything is taken down, so that whoever finds it can undo
    # it: the linkwatch every minute, and the service at start-up. /run is a
    # tmpfs, so a reboot clears it, which is also correct.
    boot=$(cat "$BOOT_ID_FILE" 2>/dev/null)
    up=$(cut -d. -f1 "$UPTIME_FILE")
    cat > "$INTENT" <<EOF
{"boot_id":"$boot","action":"$1","target":"$2","peer":"$3",
 "undo_file":"$4","undo_value":"$5","undo_at":$((up + GUARD_AFTER))}
EOF
}

clear_intent() { rm -f "$INTENT"; }

stop_dhcp() {
    # Otherwise e3372_connect.sh sees a live pidfile when the interface comes
    # back and never restarts DHCP.
    if [ -f "$PIDFILE" ]; then
        kill "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null
        rm -f "$PIDFILE"
    fi
}

wait_back() {
    i=0
    while [ $i -lt "$WAIT_BACK" ]; do
        [ -n "$(find_dev)" ] && [ -c /dev/cdc-wdm0 ] && return 0
        sleep 1
        i=$((i + 1))
    done
    [ -n "$(find_dev)" ]
}

# The test bench sources this file to exercise the helpers above; it must stop
# before acting. In normal use the variable is unset and this is a no-op.
if [ -n "$USB_RESET_SOURCE_ONLY" ]; then
    return 0 2>/dev/null || exit 0
fi

# ---------------------------------------------------------------------------

[ -z "$DEV" ] || [ ! -f "$DEV/idVendor" ] && DEV=$(find_dev)
if [ -z "$DEV" ]; then
    say "the modem is not on the USB bus at all"
    # Nothing to cycle: the only remaining host-side lever is the controller.
    [ "$ACTION" = "portcycle" ] && ACTION=rebind
fi

OLDNUM=$(cat "$DEV/devnum" 2>/dev/null)
say "starting (dev=${DEV:-none} devnum=${OLDNUM:-none})"

case "$ACTION" in
portcycle)
    PORT=$(find_port "$DEV")
    if [ -z "$PORT" ]; then
        say "no port directory for $DEV, falling back to authorized"
        stop_dhcp
        echo 0 > "$DEV/authorized" && sleep "$SETTLE" && echo 1 > "$DEV/authorized"
        wait_back && say "device back after the authorize fallback" \
                  || say "device did NOT come back"
        exit 0
    fi
    PEER=$(readlink -f "$PORT/peer" 2>/dev/null)
    say "port=$PORT peer=${PEER:-none}"

    write_intent portcycle "$PORT" "$PEER" disable 0
    # Four independent layers make sure the port comes back up: this trap, the
    # guard child below, the intent journal, and the fact that a reboot clears
    # sysfs anyway.
    undo_port() {
        echo 0 > "$PORT/disable" 2>/dev/null
        [ -n "$PEER" ] && echo 0 > "$PEER/disable" 2>/dev/null
        clear_intent
    }
    # A signal must undo AND stop: a trap that only undoes lets the script
    # carry on as if nothing had happened.
    trap 'undo_port' EXIT
    trap 'undo_port; exit 1' INT TERM HUP
    ( sleep "$GUARD_AFTER"
      if [ "$(cat "$PORT/disable" 2>/dev/null)" = "1" ]; then
          echo 0 > "$PORT/disable" 2>/dev/null
          [ -n "$PEER" ] && echo 0 > "$PEER/disable" 2>/dev/null
          clear_intent
          echo "$(date) - usb_reset: GUARD re-enabled $PORT" >> "$LOG"
      fi ) &
    GUARD=$!

    stop_dhcp
    # Both PORTSCs of the connector must go down together.
    [ -n "$PEER" ] && echo 1 > "$PEER/disable" 2>/dev/null
    if ! echo 1 > "$PORT/disable" 2>/dev/null; then
        say "cannot write $PORT/disable, falling back to authorized"
        kill "$GUARD" 2>/dev/null
        trap - EXIT INT TERM HUP
        clear_intent
        echo 0 > "$DEV/authorized" 2>/dev/null
        sleep "$SETTLE"
        echo 1 > "$DEV/authorized" 2>/dev/null
        wait_back && say "device back after the authorize fallback"
        exit 0
    fi
    state=$(cat "$PORT/state" 2>/dev/null)
    say "port state is now '$state'"
    sleep "$SETTLE"
    echo 0 > "$PORT/disable" 2>/dev/null
    [ -n "$PEER" ] && echo 0 > "$PEER/disable" 2>/dev/null
    clear_intent
    kill "$GUARD" 2>/dev/null
    trap - EXIT INT TERM HUP
    ;;

rebind)
    [ -z "$HCI" ] && HCI=$(find_hci "$DEV")
    DRV=/sys/bus/platform/drivers/xhci-hcd
    if [ -z "$HCI" ] || [ ! -d "$DRV" ]; then
        say "cannot work out the controller (hci='${HCI:-}'), nothing to do"
        exit 1
    fi
    # Only ever the controller carrying the modem, and only if the modem is
    # alone on it - unless the owner explicitly accepted the collateral.
    HCIDIR=$(readlink -f "$DRV/$HCI" 2>/dev/null)
    SELF=$( [ -n "$DEV" ] && readlink -f "$DEV" )
    OTHERS=$(hci_collateral "$HCIDIR" "$SELF" | tr '\n' ' ')
    if [ -n "$OTHERS" ] && [ "$(conf_get HCI_REBIND_SHARED 0)" != "1" ]; then
        say "REFUSED: ${OTHERS}share $HCI with the modem and would be re-enumerated too (set HCI_REBIND_SHARED=1 in $CONF to accept that)"
        exit 3
    fi
    say "rebinding controller $HCI${OTHERS:+ (also re-enumerates: $OTHERS)}"
    write_intent rebind "$DRV" "" bind "$HCI"
    undo_bind() {
        echo "$HCI" > "$DRV/bind" 2>/dev/null
        clear_intent
    }
    trap 'undo_bind' EXIT
    trap 'undo_bind; exit 1' INT TERM HUP
    ( sleep "$GUARD_AFTER"
      if [ ! -e "$DRV/$HCI" ]; then
          echo "$HCI" > "$DRV/bind" 2>/dev/null
          clear_intent
          echo "$(date) - usb_reset: GUARD re-bound $HCI" >> "$LOG"
      fi ) &
    GUARD=$!
    stop_dhcp
    echo "$HCI" > "$DRV/unbind" 2>/dev/null
    sleep "$SETTLE"
    echo "$HCI" > "$DRV/bind" 2>/dev/null
    clear_intent
    kill "$GUARD" 2>/dev/null
    trap - EXIT INT TERM HUP
    ;;

*)
    say "unknown action"
    exit 1
    ;;
esac

if wait_back; then
    NEWDEV=$(find_dev)
    NEWNUM=$(cat "$NEWDEV/devnum" 2>/dev/null)
    if [ -n "$OLDNUM" ] && [ "$NEWNUM" = "$OLDNUM" ]; then
        say "device is back but devnum is unchanged ($NEWNUM): nothing was reset"
    else
        say "device re-enumerated (devnum ${OLDNUM:-none} -> ${NEWNUM:-none})"
    fi
else
    say "device did NOT come back within ${WAIT_BACK}s"
fi
say "done, wwan0 addresses: $(ip -4 addr show wwan0 2>/dev/null | grep -c inet)"
