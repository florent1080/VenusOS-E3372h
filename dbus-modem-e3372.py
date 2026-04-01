#!/usr/bin/python3 -u
"""
dbus-modem-e3372.py — Venus OS D-Bus modem service for Huawei E3372h (NCM mode)
Publishes modem info on com.victronenergy.modem for the Venus OS UI.
Manages NCM data connection via AT^NDISDUP instead of PPP.
"""

import os
import sys
import signal
import time
import threading
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

VERSION = '1.2-e3372'

# Read APN from config file, fallback to default
APN = 'mmsbouygtel.com'
_config_file = '/data/e3372-config.conf'
if os.path.isfile(_config_file):
    with open(_config_file) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line.startswith('APN=') and not _line.startswith('#'):
                APN = _line.split('=', 1)[1].strip()
                break

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
            return False

    def close(self):
        if self.ser:
            try:
                self.ser.close()
            except:
                pass

    def at(self, cmd, timeout=3):
        """Send AT command and return response lines (excluding echo and OK/ERROR)."""
        with self.lock:
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
            except serial.SerialException as e:
                log.error('Serial error on %s: %s', cmd, e)
                return None


class ModemService:
    def __init__(self, dev):
        self.modem = E3372Modem(dev)
        self.dbus = None
        self.settings = None
        self.ncm_connected = False

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

        # Start periodic update (every 10 seconds)
        GLib.timeout_add(10000, self._update)

        return True

    def setting_changed(self, setting, old, new):
        log.info('Setting %s changed: %s -> %s', setting, old, new)
        if setting == 'apn':
            self._setup_ncm()

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

        # Setup NCM connection
        self._setup_ncm()

        # First update
        self._update_status()

    def _setup_ncm(self):
        """Configure APN and start NCM data connection."""
        apn = ''
        if self.settings:
            apn = self.settings['apn']
        if not apn:
            apn = APN

        log.info('Setting up NCM with APN: %s', apn)
        self.modem.at('AT+CGDCONT=1,"IP","%s"' % apn)
        time.sleep(1)

        # Start NCM connection
        self.modem.at('AT^NDISDUP=1,1,"%s"' % apn, timeout=5)
        time.sleep(3)

        # Check status
        r = self.modem.at('AT^NDISSTATQRY?')
        if r:
            for line in r:
                if '^NDISSTATQRY:' in line:
                    parts = line.split(':')[1].strip().split(',')
                    connected = parts[0].strip() == '1'
                    self.ncm_connected = connected
                    log.info('NCM connected: %s', connected)

        # Bring up wwan0 and DHCP
        if self.ncm_connected:
            os.system('ip link set wwan0 up 2>/dev/null')
            os.system('udhcpc -i wwan0 -b -q 2>/dev/null &')

    def _update_status(self):
        """Query modem status and update D-Bus values."""
        # SIM status
        r = self.modem.at('AT+CPIN?')
        if r:
            for line in r:
                if '+CPIN:' in line:
                    status = line.split(':')[1].strip()
                    self.dbus['/SimStatus'] = 1000 if status == 'READY' else 1001

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
                    self.dbus['/Roaming'] = (stat == 5)

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

        # NCM connection status
        r = self.modem.at('AT^NDISSTATQRY?')
        if r:
            for line in r:
                if '^NDISSTATQRY:' in line:
                    parts = line.split(':')[1].strip().split(',')
                    self.ncm_connected = parts[0].strip() == '1'

        # Check wwan0 IP
        try:
            import subprocess
            result = subprocess.run(['ip', '-4', 'addr', 'show', 'wwan0'],
                                    capture_output=True, text=True, timeout=5)
            for line in result.stdout.split('\n'):
                if 'inet ' in line:
                    ip = line.strip().split()[1].split('/')[0]
                    self.dbus['/IP'] = ip
                    self.ncm_connected = True
        except:
            pass

        self.dbus['/Connected'] = 1 if self.ncm_connected else 0

    def _update(self):
        """Periodic update called by GLib."""
        try:
            self._update_status()
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
