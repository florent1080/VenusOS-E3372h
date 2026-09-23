#!/bin/sh
# Offline bench for the collateral guard of e3372_usb_reset.sh.
#
# Rebinding the modem's USB controller re-enumerates every device on it. On
# the reference install that includes the VE.Direct cable of the BMV-712, the
# active battery service with DVCC on - so the helper must refuse to rebind a
# shared controller unless the owner explicitly accepted it. This sources the
# REAL helper and exercises its discovery against a fake controller subtree
# made of plain directories (no symlinks, so it also runs under Git Bash).
#
# It also checks the DHCP client matcher and lints every shell file for the
# editing artefacts that slipped through more than once.
#
# Usage: sh tests/usb_reset_bench.sh

HERE=$(cd "$(dirname "$0")" && pwd)
PKG=$(dirname "$HERE")
TMP=${TMPDIR:-/tmp}/e3372-usbbench.$$
PASS=0
FAIL=0

ok()   { PASS=$((PASS + 1)); echo "  ok   - $1"; }
bad()  { FAIL=$((FAIL + 1)); echo "  FAIL - $1"; }
check() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (got '$2', want '$3')"; fi; }

mkdev() {   # mkdev <dir> <vid> <product>
    mkdir -p "$1"
    printf '%s\n' "$2" > "$1/idVendor"
    printf '%s\n' "$3" > "$1/product"
}

setup() {
    rm -rf "$TMP"
    HD="$TMP/sys/devices/platform/axi/x.usb/xhci-hcd.1"
    mkdev "$HD/usb3" 1d6b "xHCI Host Controller"
    mkdev "$HD/usb4" 1d6b "xHCI Host Controller"
    mkdev "$HD/usb3/3-1" 12d1 "HUAWEI_MOBILE"
    MODEM="$HD/usb3/3-1"
}

# Load the helper's functions without running it.
USB_RESET_SOURCE_ONLY=1
export USB_RESET_SOURCE_ONLY
. "$PKG/e3372_usb_reset.sh"

echo "== e3372_usb_reset.sh: controller rebind guard =="

setup
check "the modem alone: nothing else would be touched" \
      "$(hci_collateral "$HD" "$MODEM" | tr '\n' ' ')" ""

setup
mkdev "$HD/usb3/3-2" 0403 "TTL232R-3V3"
check "the BMV cable next to the modem is found" \
      "$(hci_collateral "$HD" "$MODEM" | tr '\n' ' ')" "3-2 (TTL232R-3V3) "

setup
mkdev "$HD/usb3/3-2" 1a40 "USB 2.0 Hub"
mkdev "$HD/usb3/3-2/3-2.1" 1a86 "USB Serial"
got=$(hci_collateral "$HD" "$MODEM" | sort | tr '\n' ' ')
check "a device behind a hub is found too" "$got" "3-2 (USB 2.0 Hub) 3-2.1 (USB Serial) "

setup
check "root hubs are never counted" \
      "$(hci_collateral "$HD" "$MODEM" | grep -c 1d6b)" "0"

# the modem already gone from the bus: everything else still counts
setup
mkdev "$HD/usb3/3-2" 0403 "TTL232R-3V3"
rm -rf "$MODEM"
check "with the modem absent, the neighbour is still found" \
      "$(hci_collateral "$HD" "" | tr '\n' ' ')" "3-2 (TTL232R-3V3) "

# the acceptance switch is read safely
setup
CONF="$TMP/e3372-config.conf"
printf 'HCI_REBIND_SHARED=1\r\n' > "$CONF"
check "the acceptance survives a Windows line ending" "$(conf_get HCI_REBIND_SHARED 0)" "1"
printf 'HCI_REBIND_SHARED=yes please\n' > "$CONF"
check "anything but a number means no" "$(conf_get HCI_REBIND_SHARED 0)" "0"
rm -f "$CONF"
check "absent means no" "$(conf_get HCI_REBIND_SHARED 0)" "0"

echo
echo "== source-level guarantees =="

# the guard runs before the controller is ever unbound
guard=$(grep -n 'hci_collateral "\$HCIDIR"' "$PKG/e3372_usb_reset.sh" | head -n 1 | cut -d: -f1)
unbind=$(grep -n '> "\$DRV/unbind"' "$PKG/e3372_usb_reset.sh" | head -n 1 | cut -d: -f1)
if [ -n "$guard" ] && [ -n "$unbind" ] && [ "$guard" -lt "$unbind" ]; then
    ok "the collateral check comes before the unbind (lines $guard < $unbind)"
else
    bad "the collateral check must come before the unbind (guard=$guard unbind=$unbind)"
fi
if grep -q 'exit 3' "$PKG/e3372_usb_reset.sh"; then ok "a refusal exits with its own status"
else bad "no distinct exit status for a refusal"; fi
if grep -q '"\$rc" = 3' "$PKG/e3372_linkwatch.sh"; then ok "the watchdog recognises a refusal"
else bad "the watchdog does not recognise a refusal"; fi

echo
echo "== DHCP clients: every one on wwan0, and only those =="

# An orphan client, whose pid had been overwritten in the pidfile, was found
# alive after 21 hours fighting the current one over the lease. Stopping and
# detecting clients now goes by command line, not by pidfile.
m() { printf '%s' "$1" | is_wwan_dhcp && echo match || echo no; }
check "the real client is recognised" \
      "$(m 'udhcpc -i wwan0 -b -p /var/run/udhcpc.wwan0.pid -t 8 -T 3 -A 15 ')" "match"
check "a client started by full path is recognised" "$(m '/sbin/udhcpc -i wwan0 ')" "match"
check "a client on another interface is left alone" "$(m 'udhcpc -i wlan0 -b ')" "no"
check "a similarly named interface is left alone" "$(m 'udhcpc -i wwan00 ')" "no"
check "a grep that mentions it is left alone" "$(m 'grep udhcpc -i wwan0 ')" "no"
check "a shell that mentions it is left alone" "$(m 'sh -c udhcpc -i wwan0 ; sleep 1 ')" "no"

if grep -q 'udhcpc .\*-i \$IFACE ' "$PKG/e3372_connect.sh"; then
    ok "the link bring-up looks for any client on the interface, not just the pidfile's"
else
    bad "the link bring-up only trusts the pidfile"
fi

echo
echo "== editing artefacts =="

# Three times in one day an edit turned a backslash-newline continuation into
# a literal '\n' followed by indentation. The script still parses, the command
# silently misbehaves (an extra argument, a grep that never matches). Catch
# that shape in every shell file of the package.
for f in "$PKG"/*.sh "$PKG/setup" "$PKG"/tests/*.sh; do
    if grep -nE ' \\n {4,}' "$f" > /dev/null; then
        bad "a broken line continuation in $(basename "$f"): $(grep -nE ' \\n {4,}' "$f" | head -n 1 | cut -c1-60)"
    else
        ok "no broken line continuation in $(basename "$f")"
    fi
done

# Raw control characters (a CR, a NUL, a ^A where a \1 was meant) in any
# source file of the package.
for f in "$PKG"/*.sh "$PKG"/*.py "$PKG/setup" "$PKG"/tests/*.sh "$PKG"/tests/*.py; do
    n=$(tr -d '\n\t' < "$f" | tr -d '\040-\176' | tr -d '\200-\377' | wc -c)
    check "no raw control character in $(basename "$f")" "$n" "0"
done

rm -rf "$TMP"
echo
echo "passed: $PASS   failed: $FAIL"
[ "$FAIL" = 0 ]
