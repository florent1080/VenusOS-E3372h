#!/bin/sh
# Offline bench for e3372_linkwatch.sh and e3372_usb_reset.sh.
#
# The link watchdog guards the only remote link this installation has, and it
# is the one piece the Python bench cannot reach. This runs the REAL scripts
# under /bin/sh against a fake sysfs tree, with stubbed ip/dbus/AT tools.
#
# Usage: sh tests/linkwatch_bench.sh

set -u
HERE=$(cd "$(dirname "$0")" && pwd)
PKG=$(dirname "$HERE")
TMP=${TMPDIR:-/tmp}/e3372-bench.$$
PASS=0
FAIL=0

ok()   { PASS=$((PASS + 1)); echo "  ok   - $1"; }
bad()  { FAIL=$((FAIL + 1)); echo "  FAIL - $1"; }
check() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (got '$2', want '$3')"; fi; }

setup() {
    rm -rf "$TMP"
    mkdir -p "$TMP/run" "$TMP/data/log/e3372" "$TMP/bin" \
             "$TMP/sys/bus/usb/devices/3-1" \
             "$TMP/sys/bus/usb/devices/usb3/3-0:1.0/usb3-port1"
    printf '12d1\n' > "$TMP/sys/bus/usb/devices/3-1/idVendor"
    printf '1506\n' > "$TMP/sys/bus/usb/devices/3-1/idProduct"
    printf '1\n'    > "$TMP/sys/bus/usb/devices/3-1/authorized"
    printf '5\n'    > "$TMP/sys/bus/usb/devices/3-1/devnum"
    printf '0\n'    > "$TMP/sys/bus/usb/devices/usb3/3-0:1.0/usb3-port1/disable"
    printf 'configured\n' > "$TMP/sys/bus/usb/devices/usb3/3-0:1.0/usb3-port1/state"
    printf '123456\n' > "$TMP/uptime"
}

# Source the watchdog with every path pointed at the fake tree.
load_watchdog() {
    CONF="$TMP/data/e3372-config.conf"
    LOG="$TMP/data/log/e3372/linkwatch.log"
    PIDFILE="$TMP/run/linkwatch.pid"
    INTENT="$TMP/run/e3372-intent.json"
    LAST_DESTRUCTIVE="$TMP/run/last-destructive"
    RECOVERY_STATE="$TMP/run/e3372-recovery.json"
    USB_RESET="$PKG/e3372_usb_reset.sh"
    AT="$TMP/bin/e3372_at.py"
    LINKWATCH_SOURCE_ONLY=1
    export LINKWATCH_SOURCE_ONLY
    . "$PKG/e3372_linkwatch.sh"
    # Re-point what the script hardcoded at load time.
    CONF="$TMP/data/e3372-config.conf"
    LOG="$TMP/data/log/e3372/linkwatch.log"
    INTENT="$TMP/run/e3372-intent.json"
    LAST_DESTRUCTIVE="$TMP/run/last-destructive"
    RECOVERY_STATE="$TMP/run/e3372-recovery.json"
    BOOT_ID_FILE="$TMP/boot_id"
    UPTIME_FILE="$TMP/uptime"
    modem_sysfs() {
        for f in "$TMP"/sys/bus/usb/devices/*/idProduct; do
            [ -f "$f" ] || continue
            d=$(dirname "$f")
            if grep -q '^1506$' "$f" 2>/dev/null && grep -q '^12d1$' "$d/idVendor" 2>/dev/null; then
                echo "$d"; return 0
            fi
        done
        return 1
    }
}

echo "== e3372_linkwatch.sh =="

# --- 1. the config is read safely, whatever is in it ----------------------
setup
cat > "$TMP/data/e3372-config.conf" <<'EOF'
APN=mmsbouygtel.com
LINKWATCH_DOWN_SECONDS=300
LINKWATCH_HCI_RESET=1
EOF
load_watchdog
check "plain value is read" "$(conf_get LINKWATCH_DOWN_SECONDS 999)" "300"
check "missing key falls back" "$(conf_get NOT_A_KEY 777)" "777"

# a CR at the end of the line (the config is edited on Windows) must not make
# the value unreadable and silently disarm the watchdog
printf 'LINKWATCH_HCI_RESET=1\r\n' > "$TMP/data/e3372-config.conf"
check "CRLF value is still read" "$(conf_get LINKWATCH_HCI_RESET 0)" "1"

# a line that would be a fatal syntax error if the file were sourced
printf 'LINKWATCH_DOWN_SECONDS=600 (seconds)\nAPN=$(touch %s/pwned)\n' "$TMP" \
    > "$TMP/data/e3372-config.conf"
check "garbage value falls back" "$(conf_get LINKWATCH_DOWN_SECONDS 900)" "900"
if [ -f "$TMP/pwned" ]; then bad "the config was executed"; else ok "the config is never executed"; fi

# --- 2. an unfinished USB action is repaired ------------------------------
setup
load_watchdog
BOOT=$(cat "$TMP/boot_id")
PORTDIR="$TMP/sys/bus/usb/devices/usb3/3-0:1.0/usb3-port1"
printf '1\n' > "$PORTDIR/disable"          # a helper died between its writes
cat > "$TMP/run/e3372-intent.json" <<EOF
{"boot_id":"$BOOT","action":"portcycle","target":"$PORTDIR","peer":"",
 "undo_file":"disable","undo_value":"0","undo_at":100}
EOF
repair_intent
check "the port was re-enabled" "$(cat "$PORTDIR/disable")" "0"
if [ -f "$TMP/run/e3372-intent.json" ]; then bad "the intent was not cleared"
else ok "the intent was cleared"; fi
grep -q CRITICAL "$LOG" && ok "the repair is logged as critical" \
                        || bad "the repair was not logged"

# an intent from a previous boot must be discarded, not replayed
setup
load_watchdog
printf '0\n' > "$PORTDIR/disable"
cat > "$TMP/run/e3372-intent.json" <<EOF
{"boot_id":"some-other-boot","action":"portcycle","target":"$PORTDIR","peer":"",
 "undo_file":"disable","undo_value":"1","undo_at":1}
EOF
repair_intent
check "a stale-boot intent is dropped" "$(cat "$PORTDIR/disable")" "0"

# not yet due: leave it alone, the helper may still be running
setup
load_watchdog
printf '1\n' > "$PORTDIR/disable"
cat > "$TMP/run/e3372-intent.json" <<EOF
{"boot_id":"$BOOT","action":"portcycle","target":"$PORTDIR","peer":"",
 "undo_file":"disable","undo_value":"0","undo_at":999999}
EOF
repair_intent
check "a fresh intent is not repaired early" "$(cat "$PORTDIR/disable")" "1"

# --- 3. the destructive budget is shared with the service -----------------
setup
load_watchdog
if destructive_allowed; then ok "allowed when nothing happened yet"
else bad "should be allowed"; fi
printf '123400\n' > "$TMP/run/last-destructive"     # 56 s ago
if destructive_allowed; then bad "should be held"; else ok "held right after a reset"; fi
printf '100000\n' > "$TMP/run/last-destructive"     # 23400 s ago
if destructive_allowed; then ok "allowed again after the gap"; else bad "should be allowed"; fi

# --- 4. it stands aside while the service is mid-rung ---------------------
setup
load_watchdog
printf '{"state": "rung", "reason": "reg"}\n' > "$TMP/run/e3372-recovery.json"
if service_is_busy; then ok "detects the service mid-rung"; else bad "missed the service"; fi
printf '{"state": "idle"}\n' > "$TMP/run/e3372-recovery.json"
if service_is_busy; then bad "false positive"; else ok "idle service is not busy"; fi

# --- 5. the modem is found by id, never by name ---------------------------
setup
load_watchdog
check "modem found in sysfs" "$(modem_sysfs)" "$TMP/sys/bus/usb/devices/3-1"
rm -rf "$TMP/sys/bus/usb/devices/3-1"
if modem_sysfs > /dev/null; then bad "found a modem that is not there"
else ok "reports the modem as absent"; fi

# --- 6. exactly one watchdog, and a signal really stops it ----------------
# Found in the field: the TERM trap removed the lock and let the script carry
# on, so every 'kill' left an unlocked watchdog running and the next start
# added another one. Three were found running side by side.

# Sets TOOK. Must NOT be called as $(...): a subshell cannot wait for its
# parent's children, so 'wait' would return at once and every check below
# would pass whatever the script did.
elapsed_wait() {
    t0=$(date +%s); wait "$1" 2>/dev/null; t1=$(date +%s); TOOK=$((t1 - t0))
}

setup
load_watchdog
PIDFILE="$TMP/run/linkwatch.pid"
(
    acquire_instance || exit 9
    : > "$TMP/child_ready"
    n=0; while [ $n -lt 20 ]; do sleep 1; n=$((n + 1)); done
) &
child=$!
n=0; while [ ! -f "$TMP/child_ready" ] && [ $n -lt 10 ]; do sleep 1; n=$((n + 1)); done
[ -f "$PIDFILE" ] && ok "the lock is taken" || bad "the lock was not taken"
kill -TERM "$child" 2>/dev/null
elapsed_wait "$child"; took=$TOOK
if [ "$took" -lt 5 ]; then ok "SIGTERM stops it (${took}s)"; else bad "SIGTERM did not stop it (${took}s)"; fi
if [ -f "$PIDFILE" ]; then bad "its lock was left behind"; else ok "its lock is released on the way out"; fi

setup
load_watchdog
PIDFILE="$TMP/run/linkwatch.pid"
printf '99999999\n' > "$PIDFILE"
cleanup
check "a lock owned by someone else is left alone" "$(cat "$PIDFILE")" "99999999"

setup
load_watchdog
PIDFILE="$TMP/run/linkwatch.pid"
sleep 30 &
live=$!
printf '%s\n' "$live" > "$PIDFILE"
if acquire_instance; then bad "started although the lock owner is alive"
else ok "does not start while the lock owner is alive"; fi
kill "$live" 2>/dev/null; wait "$live" 2>/dev/null

setup
load_watchdog
PIDFILE="$TMP/run/linkwatch.pid"
printf '99999999\n' > "$PIDFILE"
if ( acquire_instance ); then ok "a stale lock does not block the start"
else bad "a stale lock blocked the start"; fi

setup
load_watchdog
sh -c 'sleep 30; :' e3372_linkwatch.sh &
fake=$!
sleep 1
if other_instance; then ok "a running copy is found even without its lock"
else bad "a running copy without a lock went unnoticed"; fi
kill "$fake" 2>/dev/null; wait "$fake" 2>/dev/null

# --- 7. setup stops every copy, whatever the pidfile says ------------------
eval "$(sed -n '/^stop_linkwatch() {/,/^}/p' "$PKG/setup")"
sh -c 'sleep 30; :' /data/e3372_linkwatch.sh &
a=$!
sh -c 'sleep 30; :' e3372_linkwatch.sh &
b=$!
sleep 1
stop_linkwatch
elapsed_wait "$a"; took_a=$TOOK
elapsed_wait "$b"; took_b=$TOOK
if [ "$took_a" -lt 5 ] && [ "$took_b" -lt 5 ]; then ok "setup stops every running copy"
else bad "setup left a copy running (${took_a}s, ${took_b}s)"; fi

echo
echo "== source-level guarantees =="

# --- 8. no command that can strand the modem ------------------------------
for f in "$PKG/e3372_linkwatch.sh" "$PKG/e3372_usb_reset.sh" "$PKG/e3372_connect.sh"; do
    n=$(grep -c -E 'CFUN=0|CFUN=4|CFUN=6|CFUN=7|COPS=2|CGATT=0' "$f" || true)
    check "no radio-off command in $(basename "$f")" "$n" "0"
done

# --- 9. the controller rebind is never done blindly -----------------------
if grep -q 'for hci in' "$PKG/e3372_usb_reset.sh"; then
    bad "the rebind loops over every controller"
else
    ok "the rebind targets one named controller"
fi

# --- 10. every helper parses under a POSIX shell ---------------------------
for f in "$PKG"/*.sh; do
    if sh -n "$f" 2>/dev/null; then ok "sh -n $(basename "$f")"
    else bad "sh -n $(basename "$f")"; fi
done

rm -rf "$TMP"
echo
echo "passed: $PASS   failed: $FAIL"
[ "$FAIL" = 0 ]
