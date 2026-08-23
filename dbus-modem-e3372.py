#!/usr/bin/python3 -u
"""
dbus-modem-e3372.py - Venus OS D-Bus modem service for Huawei E3372h (NCM mode)
Publishes modem info on com.victronenergy.modem for the Venus OS UI.
Manages the NCM data connection via AT^NDISDUP instead of PPP, and keeps it
alive with a watchdog (re-dial on session loss, persistent DHCP client).
"""

import os
import sys
import signal
import time
import threading
import subprocess
import serial
import logging

# Venus OS python libraries
sys.path.insert(1, '/opt/victronenergy/dbus-modem')
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python'))

from gi.repository import GLib
import dbus
import dbus.mainloop.glib
from vedbus import VeDbusService
from settingsdevice import SettingsDevice

VERSION = '1.1-e3372'

CONFIG_FILE = '/data/e3372-config.conf'
IFACE = 'wwan0'
DHCP_PIDFILE = '/var/run/udhcpc.wwan0.pid'

POLL_INTERVAL = 10          # seconds between status polls
REDIAL_MIN_BACKOFF = 30     # seconds before the first re-dial retry
REDIAL_MAX_BACKOFF = 600    # ceiling for the exponential backoff
PROBE_INTERVAL = 120        # seconds between connectivity probes
PROBE_FAILURES_MAX = 3      # consecutive probe failures before forcing a re-dial
PROBE_REDIALS_MAX = 3       # give up on the probe after this many futile re-dials

# CREG stat values that mean "attached to a network"
REG_HOME = 1
REG_ROAMING = 5


def load_config(path):
    """Parse a trivial KEY=value config file. Never raises."""
    cfg = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, value = line.split('=', 1)
                cfg[key.strip()] = value.strip().strip('"').strip("'")
    except Exception:
        pass
    return cfg


_cfg = load_config(CONFIG_FILE)
APN = _cfg.get('APN', 'mmsbouygtel.com')
# Host pinged to verify traffic actually flows. Empty disables the probe, which
# is the right setting on an APN that filters ICMP.
PROBE_HOST = _cfg.get('PROBE_HOST', '8.8.8.8')

log = logging.getLogger()
logging.basicConfig(format='%(levelname)-8s %(message)s', level=logging.INFO)

modem_settings = {
    'connect': ['/Settings/Modem/Connect', 1, 0, 1],
    'roaming': ['/Settings/Modem/RoamingPermitted', 0, 0, 1],
    'apn':     ['/Settings/Modem/APN', APN, 0, 0],
}


class E3372Modem:
    def __init__(self, dev):
        self.dev = dev
        self.ser = None
        self.lock = threading.Lock()

    def open(self):
        try:
            self.ser = serial.Serial(self.dev, 115200, timeout=3)
            time.sleep(0.5)
            self.ser.reset_input_buffer()
            return True
        except Exception as e:
            log.error('Cannot open %s: %s', self.dev, e)
            self.ser = None
            return False

    def close(self):
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None

    def at(self, cmd, timeout=3):
        """Send AT command and return response lines (excluding echo and OK/ERROR).

        Returns None when the command failed or the modem did not answer - the
        caller must treat None as "unknown", never as "not connected".
        """
        with self.lock:
            # The port can die under us (USB glitch, modem reset); reopen it
            # rather than staying broken until the service is restarted.
            if self.ser is None and not self.open():
                return None
            try:
                self.ser.reset_input_buffer()
                self.ser.write((cmd + '\r').encode())
                time.sleep(0.3)

                lines = []
                end_time = time.time() + timeout
                while time.time() < end_time:
                    if self.ser.in_waiting:
                        raw = self.ser.readline()
                        line = raw.decode(errors='replace').strip()
                        if not line:
                            continue
                        if line == cmd:  # echo
                            continue
                        if line == 'OK':
                            return lines
                        if line == 'ERROR' or line.startswith('+CME ERROR'):
                            log.warning('%s -> %s', cmd, line)
                            return None
                        if line == 'COMMAND NOT SUPPORT':
                            log.warning('%s -> not supported', cmd)
                            return None
                        # Skip unsolicited Huawei notifications
                        if line.startswith('^RSSI:') or line.startswith('^HCSQ:'):
                            continue
                        if line.startswith('^NDISSTAT:'):
                            continue
                        lines.append(line)
                    else:
                        time.sleep(0.1)

                log.warning('%s -> timeout', cmd)
                return lines if lines else None
            except Exception as e:
                log.error('Serial error on %s: %s', cmd, e)
                self.close()
                return None


class ModemService:
    def __init__(self, dev):
        self.modem = E3372Modem(dev)
        self.dbus = None
        self.settings = None
        self.ncm_connected = False
        self.sim_present = True
        # watchdog state
        self.redial_count = 0
        self.next_redial = 0.0
        self.probe_failures = 0
        self.probe_redials = 0
        self.probe_disabled = False
        self.last_probe = 0.0
        self.probe_thread = None

    def start(self):
        dbus.mainloop.glib.threads_init()
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

        self.dbus = VeDbusService('com.victronenergy.modem', register=False)
        self.dbus.add_path('/Model', None)
        self.dbus.add_path('/IMEI', None)
        self.dbus.add_path('/NetworkName', None)
        self.dbus.add_path('/NetworkType', None)
        self.dbus.add_path('/SignalStrength', None)
        self.dbus.add_path('/Roaming', None)
        self.dbus.add_path('/Connected', 0)
        self.dbus.add_path('/IP', None)
        self.dbus.add_path('/SimStatus', None)
        self.dbus.add_path('/RegStatus', None)
        self.dbus.add_path('/PPPStatus', 0)
        self.dbus.register()

        log.info('Registered on D-Bus as com.victronenergy.modem')

        self.settings = SettingsDevice(self.dbus.dbusconn, modem_settings,
                                       self.setting_changed, timeout=10)

        if not self.modem.open():
            log.error('Failed to open modem, exiting')
            return False

        # Initial modem setup
        self._init_modem()

        GLib.timeout_add(POLL_INTERVAL * 1000, self._update)

        return True

    def setting_changed(self, setting, old, new):
        log.info('Setting %s changed: %s -> %s', setting, old, new)
        if setting == 'apn':
            self._hangup()
            if self._connect_wanted():
                self._reset_backoff()
                self._dial()
        elif setting == 'connect':
            if new:
                self._reset_backoff()
                self._dial()
            else:
                self._hangup()

    def _init_modem(self):
        """Initial modem identification and NCM setup."""
        r = self.modem.at('AT')
        if r is None:
            log.error('Modem not responding')
            return

        # Model
        r = self.modem.at('AT+CGMM')
        if r:
            self.dbus['/Model'] = r[0]
            log.info('Model: %s', r[0])

        # IMEI
        r = self.modem.at('AT+CGSN')
        if r:
            self.dbus['/IMEI'] = r[0]
            log.info('IMEI: %s', r[0])

        # Enable numeric error codes
        self.modem.at('AT+CMEE=1')

        self._query_ncm()
        if not self.ncm_connected and self._connect_wanted():
            self._dial()

        # First update
        self._update_status()

    # ---- NCM session ------------------------------------------------------

    def _apn(self):
        apn = self.settings['apn'] if self.settings else ''
        return apn or APN

    def _connect_wanted(self):
        """True unless the user turned the connection off in the UI."""
        if self.settings is None:
            return True
        return bool(self.settings['connect'])

    def _query_ncm(self):
        """Refresh self.ncm_connected from the modem. Keeps the previous value
        when the modem does not answer, so a serial hiccup never looks like a
        dropped session."""
        r = self.modem.at('AT^NDISSTATQRY?')
        if r:
            for line in r:
                if '^NDISSTATQRY:' in line:
                    parts = line.split(':', 1)[1].strip().split(',')
                    self.ncm_connected = parts[0].strip() == '1'
                    break
        return self.ncm_connected

    def _dial(self):
        """Bring the NCM data session up. Returns True on success."""
        apn = self._apn()
        log.info('NCM dial: APN=%s', apn)

        self.modem.at('AT+CGDCONT=1,"IP","%s"' % apn)
        time.sleep(0.5)
        # Always hang up first: NDISDUP on an already-half-open context is
        # silently ignored by the E3372h firmware.
        self.modem.at('AT^NDISDUP=1,0', timeout=5)
        self.ncm_connected = False
        time.sleep(1)
        self.modem.at('AT^NDISDUP=1,1,"%s"' % apn, timeout=10)

        for _ in range(10):
            time.sleep(1)
            if self._query_ncm():
                break

        if not self.ncm_connected:
            log.warning('NCM dial failed, session still down')
            return False

        log.info('NCM session up, (re)starting DHCP on %s', IFACE)
        self._restart_dhcp()
        return True

    def _hangup(self):
        log.info('NCM hangup')
        self.modem.at('AT^NDISDUP=1,0', timeout=5)
        self.ncm_connected = False
        self._stop_dhcp()

    # ---- DHCP client ------------------------------------------------------

    def _dhcp_pid(self):
        """PID of the udhcpc instance owning IFACE, or None."""
        try:
            with open(DHCP_PIDFILE) as f:
                pid = int(f.read().strip())
            with open('/proc/%d/cmdline' % pid, 'rb') as f:
                if b'udhcpc' in f.read():
                    return pid
        except Exception:
            pass
        return None

    def _ensure_dhcp(self):
        if self._dhcp_pid():
            return
        log.info('starting udhcpc on %s', IFACE)
        # No -q: udhcpc stays resident and renews the lease. The previous
        # version quit as soon as it had an address, so nothing ever renewed
        # it and nothing ever restored the default route.
        os.system('udhcpc -i %s -b -p %s -t 8 -T 3 -A 15 >/dev/null 2>&1 &'
                  % (IFACE, DHCP_PIDFILE))

    def _stop_dhcp(self):
        pid = self._dhcp_pid()
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass
        try:
            os.remove(DHCP_PIDFILE)
        except Exception:
            pass

    def _restart_dhcp(self):
        self._stop_dhcp()
        time.sleep(0.5)
        os.system('ip link set %s up 2>/dev/null' % IFACE)
        # The old address and its default route survive a dropped session and
        # would otherwise black-hole every packet.
        os.system('ip -4 addr flush dev %s 2>/dev/null' % IFACE)
        self._ensure_dhcp()

    def _iface_ip(self):
        try:
            result = subprocess.run(['ip', '-4', 'addr', 'show', IFACE],
                                    capture_output=True, text=True, timeout=5)
            for line in result.stdout.split('\n'):
                if 'inet ' in line:
                    return line.strip().split()[1].split('/')[0]
        except Exception:
            pass
        return ''

    # ---- status -----------------------------------------------------------

    def _update_status(self):
        """Query modem status and update D-Bus values."""
        # SIM status
        r = self.modem.at('AT+CPIN?')
        if r:
            self.sim_present = True
            for line in r:
                if '+CPIN:' in line:
                    status = line.split(':')[1].strip()
                    self.dbus['/SimStatus'] = 1000 if status == 'READY' else 1001
        else:
            # AT+CPIN? returned error (CME ERROR: 10 = no SIM inserted)
            self.sim_present = False
            self.ncm_connected = False
            self.dbus['/SimStatus'] = 0
            self.dbus['/SignalStrength'] = 0
            self.dbus['/NetworkName'] = ''
            self.dbus['/NetworkType'] = ''
            self.dbus['/RegStatus'] = 0
            self.dbus['/Connected'] = 0
            self.dbus['/PPPStatus'] = 0
            self.dbus['/IP'] = ''
            self.dbus['/Roaming'] = False
            return

        # Signal strength
        r = self.modem.at('AT+CSQ')
        if r:
            for line in r:
                if '+CSQ:' in line:
                    parts = line.split(':')[1].strip().split(',')
                    csq = int(parts[0])
                    self.dbus['/SignalStrength'] = csq

        # Registration status
        r = self.modem.at('AT+CREG?')
        if r:
            for line in r:
                if '+CREG:' in line:
                    parts = line.split(':')[1].strip().split(',')
                    stat = int(parts[1]) if len(parts) > 1 else int(parts[0])
                    self.dbus['/RegStatus'] = stat
                    self.dbus['/Roaming'] = (stat == REG_ROAMING)

        # Operator
        r = self.modem.at('AT+COPS?')
        if r:
            for line in r:
                if '+COPS:' in line:
                    parts = line.split(',')
                    if len(parts) >= 3:
                        name = parts[2].strip('" ')
                        self.dbus['/NetworkName'] = name
                        # Access technology
                        if len(parts) >= 4:
                            act = int(parts[3])
                            tech_map = {0: 'GSM', 2: 'UMTS', 7: 'LTE'}
                            self.dbus['/NetworkType'] = tech_map.get(act, 'Unknown')

        # NCM connection status - this is the only source of truth for the
        # data session. An address left over on wwan0 proves nothing: it
        # survives a dropped session and used to be reported as "connected",
        # which is exactly what hid this failure from the UI.
        self._query_ncm()

        self.dbus['/IP'] = self._iface_ip()
        self.dbus['/Connected'] = 1 if self.ncm_connected else 0
        self.dbus['/PPPStatus'] = 1 if self.ncm_connected else 0

    # ---- watchdog ---------------------------------------------------------

    def _reset_backoff(self):
        self.redial_count = 0
        self.next_redial = 0.0
        self.probe_failures = 0

    def _probe(self):
        """Verify traffic actually flows. Runs in a worker thread."""
        ok = False
        try:
            result = subprocess.run(
                ['ping', '-c', '2', '-W', '3', '-I', IFACE, PROBE_HOST],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
            ok = (result.returncode == 0)
        except Exception:
            ok = False

        if ok:
            if self.probe_failures:
                log.info('connectivity probe recovered')
            self.probe_failures = 0
            self.probe_redials = 0
        else:
            self.probe_failures += 1
            log.warning('connectivity probe failed (%d/%d)',
                        self.probe_failures, PROBE_FAILURES_MAX)

    def _maybe_probe(self, now):
        if not PROBE_HOST or self.probe_disabled:
            return
        if self.probe_thread is not None and self.probe_thread.is_alive():
            return
        if now - self.last_probe < PROBE_INTERVAL:
            return
        self.last_probe = now
        self.probe_thread = threading.Thread(target=self._probe, daemon=True)
        self.probe_thread.start()

    def _maybe_redial(self, reason, now):
        if now < self.next_redial:
            return
        self.redial_count += 1
        backoff = min(REDIAL_MIN_BACKOFF * (2 ** (self.redial_count - 1)),
                      REDIAL_MAX_BACKOFF)
        self.next_redial = now + backoff
        log.warning('watchdog: %s - re-dialling (attempt %d, next retry in %ds)',
                    reason, self.redial_count, backoff)
        if self._dial():
            log.info('watchdog: data session re-established')
            self._reset_backoff()

    def _watchdog(self):
        """Keep the data session alive. Called after every status poll."""
        now = time.monotonic()

        if not self.sim_present:
            return

        if not self._connect_wanted():
            if self.ncm_connected:
                log.info('watchdog: /Settings/Modem/Connect is off, hanging up')
                self._hangup()
            return

        reg = self.dbus['/RegStatus']
        if reg not in (REG_HOME, REG_ROAMING):
            # Not attached to a network: dialling would fail anyway, and the
            # backoff must not run away while we are simply out of coverage.
            self.probe_failures = 0
            return

        if reg == REG_ROAMING and self.settings is not None \
                and not self.settings['roaming']:
            return

        if not self.ncm_connected:
            self._maybe_redial('NCM data session down', now)
            return

        if not self.dbus['/IP']:
            self._ensure_dhcp()
            return

        # Session up with an address: make sure packets really come back.
        self._maybe_probe(now)
        if self.probe_failures < PROBE_FAILURES_MAX:
            return

        # A probe that never recovers is far more likely to be a filtered ping
        # than a broken session. Stop re-dialling rather than cycling the
        # connection forever.
        if self.probe_redials >= PROBE_REDIALS_MAX:
            if not self.probe_disabled:
                log.error('connectivity probe to %s never recovers after %d '
                          're-dials, disabling it. If your operator filters '
                          'ICMP, set PROBE_HOST= (empty) in %s',
                          PROBE_HOST, self.probe_redials, CONFIG_FILE)
                self.probe_disabled = True
            return

        before = self.next_redial
        self._maybe_redial('no connectivity after %d probes'
                           % self.probe_failures, now)
        if self.next_redial != before:
            self.probe_redials += 1

    def _update(self):
        """Periodic update called by GLib."""
        try:
            self._update_status()
            self._watchdog()
        except Exception as e:
            log.error('Update error: %s', e)
        return True


def sigterm(s, f):
    global mainloop
    log.info('Signal received, stopping')
    mainloop.quit()


def main():
    global mainloop

    if len(sys.argv) < 3 or sys.argv[1] != '-s':
        print('Usage: dbus-modem-e3372.py -s /dev/ttyUSBx')
        sys.exit(1)

    dev = sys.argv[2]
    log.info('Starting dbus-modem-e3372 %s on %s', VERSION, dev)

    signal.signal(signal.SIGINT, sigterm)
    signal.signal(signal.SIGTERM, sigterm)

    mainloop = GLib.MainLoop()

    svc = ModemService(dev)
    if not svc.start():
        sys.exit(1)

    log.info('Modem service running')
    mainloop.run()

    svc.modem.close()
    log.info('Stopped')


if __name__ == '__main__':
    main()
