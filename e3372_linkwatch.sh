#!/bin/sh
# /data/e3372_linkwatch.sh - last-resort link watchdog for the E3372h
# Part of VenusOS-E3372h package
#
# WHY THIS EXISTS
# The recovery ladder of dbus-modem-e3372.py talks to the modem through its
# tty. That tty disappears whenever the modem leaves the USB bus, and
# serial-starter then kills the service - so the component that knows how to
# recover is gone exactly when recovery is needed. This script is started
# detached by the service (new session, no inherited descriptors), so it
# outlives that, and it is the layer that repairs a half-finished USB action.
#
# ORDER MATTERS: talk to the modem first, touch USB second. On 2026-09-19 the
# ladder performed two USB re-enumerations while AT^RESET - the command that
# actually restarts this firmware - was available on /dev/cdc-wdm0 the whole
# time and was never tried. It never re-authorised its way out, because no
# host-side action changes anything inside the modem.
#
# It never reboots the GX.

CONF=/data/e3372-config.conf
LOG=/data/log/e3372/linkwatch.log
PIDFILE=/var/run/e3372-linkwatch.pid
INTENT=/run/e3372-intent.json
LAST_DESTRUCTIVE=/run/e3372-last-destructive
RECOVERY_STATE=/run/e3372-recovery.json
BOOT_ID_FILE=/proc/sys/kernel/random/boot_id
UPTIME_FILE=/proc/uptime
USB_RESET=/data/e3372_usb_reset.sh
AT=/data/e3372_at.py
IFACE=wwan0

CHECK_INTERVAL=60          # seconds between checks
STEP1_AFTER=600            # link dead this long before the first action
STEP_GAP=600               # between escalation steps
DESTRUCTIVE_GAP=900        # shared with the service's own ladder
HCI_GAP=21600              # at most one controller rebind per 6 h
HCI_RESET=1                # the controller carrying the modem only

mkdir -p /data/log/e3372 2>/dev/null
say() { echo "$(date) [$(cut -d. -f1 "$UPTIME_FILE" 2>/dev/null)] - $*" >> "$LOG"; }

# The config is edited by hand, on Windows, and a stray CR or a syntax error
# would either disarm this watchdog or execute arbitrary shell if we sourced
# it. Read it key by key instead, and fall back to the default on anything
# that is not a plain number.
conf_get() {
    v=$(sed -n "s/^[ 	]*$1[ 	]*=[ 	]*//p" "$CONF" 2>/dev/null \
        | tail -n 1 | tr -d '\r' | sed 's/^"//; s/"$//; s/^'\''//; s/'\''$//')
    case "$v" in
        ''|*[!0-9]*) echo "$2" ;;
        *)           echo "$v" ;;
    esac
}

[ -f "$CONF" ] && {
    STEP1_AFTER=$(conf_get LINKWATCH_DOWN_SECONDS "$STEP1_AFTER")
    HCI_RESET=$(conf_get LINKWATCH_HCI_RESET "$HCI_RESET")
}

now_s() { cut -d. -f1 "$UPTIME_FILE"; }   # monotonic: this GX has no NTP

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
    v=$(dbus -y com.victronenergy.settings /Settings/Modem/Connect GetValue 2>/dev/null)
    [ "$v" = "0" ] && return 1
    return 0
}

link_alive() {
    ip -4 addr show "$IFACE" 2>/dev/null | grep -q 'inet ' || return 1
    ip route 2>/dev/null | grep -q "^default.*dev $IFACE" || return 1
    return 0
}

service_is_busy() {
    # The service persists its ladder state; while it is mid-rung, stay out of
    # the way so the two can never act at the same time.
    [ -f "$RECOVERY_STATE" ] || return 1
    grep -q '"state": *"rung"' "$RECOVERY_STATE" 2>/dev/null
}

destructive_allowed() {
    [ -f "$LAST_DESTRUCTIVE" ] || return 0
    last=$(cat "$LAST_DESTRUCTIVE" 2>/dev/null)
    case "$last" in ''|*[!0-9]*) return 0 ;; esac
    [ $(( $(now_s) - last )) -ge "$DESTRUCTIVE_GAP" ]
}

mark_destructive() { now_s > "$LAST_DESTRUCTIVE"; }

# Role 0: repair a half-finished USB action. This is the layer that survives
# the helper being killed between its two writes.
repair_intent() {
    [ -f "$INTENT" ] || return 0
    boot=$(cat "$BOOT_ID_FILE" 2>/dev/null)
    grep -q "\"boot_id\":\"$boot\"" "$INTENT" 2>/dev/null || {
        rm -f "$INTENT"
        return 0
    }
    undo_at=$(sed -n 's/.*"undo_at":\([0-9]*\).*/\1/p' "$INTENT" | tail -n 1)
    case "$undo_at" in ''|*[!0-9]*) rm -f "$INTENT"; return 0 ;; esac
    [ "$(now_s)" -gt $((undo_at + 10)) ] || return 0

    target=$(sed -n 's/.*"target":"\([^"]*\)".*/\1/p' "$INTENT" | tail -n 1)
    peer=$(sed -n 's/.*"peer":"\([^"]*\)".*/\1/p' "$INTENT" | tail -n 1)
    ufile=$(sed -n 's/.*"undo_file":"\([^"]*\)".*/\1/p' "$INTENT" | tail -n 1)
    uval=$(sed -n 's/.*"undo_value":"\([^"]*\)".*/\1/p' "$INTENT" | tail -n 1)
    say "CRITICAL: a USB action was left unfinished, undoing it ($target/$ufile=$uval)"
    [ -n "$target" ] && [ -n "$ufile" ] && echo "$uval" > "$target/$ufile" 2>/dev/null
    [ -n "$peer" ] && echo "$uval" > "$peer/$ufile" 2>/dev/null
    dev=$(modem_sysfs)
    [ -n "$dev" ] && echo 1 > "$dev/authorized" 2>/dev/null
    rm -f "$INTENT"
}

at_reset() {
    [ -x "$AT" ] || { say "no $AT, cannot talk to the modem"; return 1; }
    [ -c /dev/cdc-wdm0 ] || { say "no /dev/cdc-wdm0, cannot talk to the modem"; return 1; }
    cfun=$("$AT" 'AT+CFUN?' 2>/dev/null | sed -n 's/.*+CFUN: *\([0-9]*\).*/\1/p' | tail -n 1)
    say "modem says CFUN=${cfun:-?}"
    if [ -n "$cfun" ] && [ "$cfun" != "1" ]; then
        say "radio is off, switching it on"
        "$AT" 'AT+CFUN=1' >> "$LOG" 2>&1
        sleep 20
        link_alive && return 0
    fi
    say "sending AT^RESET (the only command measured to restart this firmware)"
    "$AT" 'AT^RESET' >> "$LOG" 2>&1
    mark_destructive
    sleep 45
    link_alive
}

# --- single instance -------------------------------------------------------
# The pidfile is the lock. Until v1.5 the TERM trap removed it and then let
# the script carry on, so every 'kill' left a running watchdog without a lock
# and the next start added another one: three were found running side by
# side. Now a signal really stops the script, only the owner may remove the
# lock, and a process scan backs the lock up.
cleanup() {
    [ "$(cat "$PIDFILE" 2>/dev/null)" = "$$" ] && rm -f "$PIDFILE"
}

# A watchdog runs as exactly "/bin/sh /data/e3372_linkwatch.sh": an interpreter
# and the script, nothing else. Matching that shape and not just the name
# matters - an SSH command or an editor that merely mentions the file must
# never be taken for a running copy (a looser match killed the very shell
# that was running it).
other_instance() {
    for d in /proc/[0-9]*; do
        [ "${d#/proc/}" = "$$" ] && continue
        { tr '\0' ' ' < "$d/cmdline"; } 2>/dev/null | grep -q '^[^ ]*sh [^ ]*e3372_linkwatch\.sh $' && return 0
    done
    return 1
}

acquire_instance() {
    if [ -f "$PIDFILE" ]; then
        old=$(cat "$PIDFILE" 2>/dev/null)
        [ -n "$old" ] && [ "$old" != "$$" ] && [ -d "/proc/$old" ] && return 1
    fi
    other_instance && return 1
    echo $$ > "$PIDFILE"
    trap 'cleanup' EXIT
    trap 'cleanup; exit 0' INT TERM HUP
    return 0
}

# The test bench sources this file to exercise the helpers above against a
# fake sysfs tree; it must stop before taking the lock and entering the loop.
# In normal use the variable is unset and this is a no-op.
if [ -n "$LINKWATCH_SOURCE_ONLY" ]; then
    return 0 2>/dev/null || exit 0
fi

# LINKWATCH=0 disables the watchdog, whoever starts it (service or setup).
[ "$(conf_get LINKWATCH 1)" = "0" ] && exit 0
acquire_instance || exit 0

say "linkwatch started (pid $$, first action after ${STEP1_AFTER}s, hci_reset=$HCI_RESET)"

down_since=0
step=0
modem_hci=""
last_step_at=0
last_hci=0
cycle=0
backoff=0

while true; do
    sleep "$CHECK_INTERVAL"
    now=$(now_s)

    repair_intent

    if ! connect_wanted; then
        down_since=0; step=0
        continue
    fi

    if link_alive; then
        if [ "$down_since" != 0 ]; then
            say "link is back after $((now - down_since))s (at step $step)"
        fi
        down_since=0
        # A full hour of health resets the escalation and the backoff.
        if [ "$step" != 0 ] && [ $((now - last_step_at)) -ge 3600 ]; then
            step=0; backoff=0
        fi
        continue
    fi

    [ "$down_since" = 0 ] && { down_since=$now; say "link down, watching"; }
    [ $((now - down_since)) -lt "$STEP1_AFTER" ] && continue
    [ "$last_step_at" != 0 ] && [ $((now - last_step_at)) -lt "$STEP_GAP" ] && continue
    service_is_busy && { say "the service is mid-rung, standing by"; continue; }

    step=$((step + 1))
    last_step_at=$now
    dev=$(modem_sysfs)
    if [ -n "$dev" ]; then
        modem_hci=$(readlink -f "$dev" | sed -n 's#.*/\(xhci-hcd\.[0-9]*\)/.*#\1#p')
    fi

    case "$step" in
    1)
        say "step 1: talking to the modem (down for $((now - down_since))s)"
        at_reset && { say "recovered by AT^RESET"; step=0; continue; }
        ;;
    2)
        if ! destructive_allowed; then
            say "step 2 held: the service reset something less than ${DESTRUCTIVE_GAP}s ago"
            step=1
            continue
        fi
        say "step 2: USB re-enumeration"
        mark_destructive
        [ -x "$USB_RESET" ] && "$USB_RESET" portcycle "$dev" >> "$LOG" 2>&1
        sleep 20
        ;;
    3)
        say "step 3: AT^RESET again, the modem may answer after the re-enumeration"
        at_reset && { say "recovered by AT^RESET"; step=0; continue; }
        ;;
    4)
        if [ "$HCI_RESET" != "1" ]; then
            say "step 4 skipped: LINKWATCH_HCI_RESET is not 1"
        elif [ "$last_hci" != 0 ] && [ $((now - last_hci)) -lt "$HCI_GAP" ]; then
            say "step 4 held: the controller was rebound $((now - last_hci))s ago"
        elif [ -z "$modem_hci" ]; then
            say "step 4 skipped: the modem's controller was never seen"
        else
            say "step 4: rebinding the modem's USB controller ($modem_hci)"
            rc=0
            [ -x "$USB_RESET" ] && { "$USB_RESET" rebind "$dev" "$modem_hci" >> "$LOG" 2>&1; rc=$?; }
            if [ "$rc" = 3 ]; then
                # The helper refused before touching anything: other devices
                # share the controller. Nothing happened, so nothing is spent.
                say "step 4 refused: other devices share $modem_hci (see usbreset.log)"
            else
                last_hci=$now
                mark_destructive
                sleep 30
            fi
        fi
        ;;
    *)
        # Round finished without recovery: wait longer and start again. Never
        # give up, never reboot.
        cycle=$((cycle + 1))
        backoff=$((1800 * cycle))
        [ "$backoff" -gt 14400 ] && backoff=14400
        say "a full round did not recover the link; next round in ${backoff}s"
        i=0
        while [ $i -lt "$backoff" ]; do
            sleep "$CHECK_INTERVAL"
            i=$((i + CHECK_INTERVAL))
            repair_intent
            if link_alive; then
                say "link came back on its own during the backoff"
                break
            fi
        done
        step=0
        ;;
    esac
done
