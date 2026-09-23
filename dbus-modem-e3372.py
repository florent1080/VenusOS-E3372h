#!/usr/bin/python3 -u
"""
dbus-modem-e3372.py - Venus OS D-Bus modem service for Huawei E3372h (NCM mode)

Publishes modem info on com.victronenergy.modem for the Venus OS UI, manages
the NCM data connection via AT^NDISDUP instead of PPP, and keeps the modem
usable with two watchdogs:

  - the session watchdog re-dials a dropped data session (exponential
    backoff) and checks that traffic really flows (ICMP probe);
  - the recovery ladder handles every state in which the modem cannot be
    dialled at all: mute AT port, missing or failed SIM, registration lost,
    denied or stuck, dial refused, session up without traffic. It escalates
    from a network re-selection to a radio cycle, a modem reset and finally a
    USB port reset, with an exponential backoff between full ladders.

The GX itself is never rebooted: every recovery action is confined to the
modem.
"""

import os
import sys
import json
import signal
import select
import posixpath
import time
import threading
import subprocess
import logging
import logging.handlers

import serial

# Venus OS python libraries
sys.path.insert(1, '/opt/victronenergy/dbus-modem')
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python'))

from gi.repository import GLib
import dbus
import dbus.mainloop.glib
from vedbus import VeDbusService
from settingsdevice import SettingsDevice

VERSION = '1.5-e3372'

CONFIG_FILE = '/data/e3372-config.conf'
IFACE = 'wwan0'
DHCP_PIDFILE = '/var/run/udhcpc.wwan0.pid'
WDM_DEV = '/dev/cdc-wdm0'
STATE_FILE = '/run/e3372-recovery.json'
USB_RESET_HELPER = '/data/e3372_usb_reset.sh'
LINKWATCH_HELPER = '/data/e3372_linkwatch.sh'
LINKWATCH_PIDFILE = '/var/run/e3372-linkwatch.pid'
INTENT_FILE = '/run/e3372-intent.json'
RECOVERY_LOG_DIR = '/data/log/e3372'
RECOVERY_LOG = RECOVERY_LOG_DIR + '/recovery.log'
BOOT_ID_FILE = '/proc/sys/kernel/random/boot_id'

POLL_INTERVAL = 10          # seconds between status polls
REDIAL_MIN_BACKOFF = 30     # seconds before the first re-dial retry
REDIAL_MAX_BACKOFF = 600    # ceiling for the exponential backoff
PROBE_INTERVAL = 120        # seconds between connectivity probes
PROBE_FAILURES_MAX = 3      # consecutive probe failures before forcing a re-dial
PROBE_REDIALS_MAX = 3       # futile re-dials before the recovery ladder takes over
PROBE_SUSPEND = 6 * 3600    # probe suspended this long when nothing restores traffic
DIAL_TIMEOUT = 30           # seconds for a dial to bring the NCM session up
DIAL_FAILURES_MAX = 5       # consecutive dial failures before the recovery ladder
SESSION_STABLE = 60         # seconds of stable session before the backoff is reset
DHCP_STALE = 60             # seconds without address on a live session before DHCP restart
DHCP_RESTART_MIN_GAP = 120  # never restart DHCP more often than this
DHCP_START_GRACE = 15       # let a freshly started udhcpc write its pidfile
AT_MUTE_POLLS = 6           # consecutive polls without any AT answer = mute port
SIGNAL_LOG_INTERVAL = 60    # seconds between periodic signal quality lines
NAG_INTERVAL = 600          # seconds between repeats of a standing warning
HEALTHY_RESET_AFTER = 3600  # seconds of health before the ladder counter is cleared
LADDER_BACKOFF_MIN = 900    # seconds between two full ladders (first)
LADDER_BACKOFF_MAX = 14400  # ceiling (4 h)
DESTRUCTIVE_WINDOW = 1800   # no more than DESTRUCTIVE_MAX modem/USB resets per window
# A full ladder is exactly three destructive rungs (modem reset, USB
# re-enumeration, controller rebind), so the budget must allow three or the
# last rung is unreachable. Two full ladders inside the window stay impossible.
DESTRUCTIVE_MAX = 3
STEP_GAP = 10               # seconds between the two commands of a cycle rung
VERIFY_WINDOW = 45          # seconds given to a verification dial
TICK_ERRORS_MAX = 6         # recovery tick exceptions in a row before a self-reset
REGISTER_RETRY_FOR = 30     # seconds to retry the D-Bus name registration

# CREG stat values (3GPP TS 27.007)
REG_NREG, REG_HOME, REG_SEARCHING, REG_DENIED, REG_UNKNOWN, REG_ROAMING = 0, 1, 2, 3, 4, 5
REGISTERED = (REG_HOME, REG_ROAMING)
REG_NAMES = {
    REG_NREG: 'not registered', REG_HOME: 'home', REG_SEARCHING: 'searching',
    REG_DENIED: 'denied', REG_UNKNOWN: 'unknown', REG_ROAMING: 'roaming',
}

# /SimStatus codes: CME error codes (3GPP TS 27.007 9.2) plus the two Victron
# values. These are the codes the Venus GUI knows how to display.
SIM_READY = 1000
SIM_ERROR = 1001
SIM_NO_SIM = 10
SIM_PIN = 11
SIM_PUK = 12
SIM_FAIL = 13
SIM_BUSY = 14
SIM_WRONG = 15
SIM_BAD_PASSWD = 16
CPIN_TEXT = {
    'READY': SIM_READY, 'SIM PIN': SIM_PIN, 'SIM PUK': SIM_PUK,
    'PH-SIM PIN': 5, 'PH-FSIM PIN': 6, 'PH-FSIM PUK': 7,
    'SIM PIN2': 17, 'SIM PUK2': 18,
    'PH-NET PIN': 40, 'PH-NET PUK': 41, 'PH-NETSUB PIN': 42, 'PH-NETSUB PUK': 43,
    'PH-SP PIN': 44, 'PH-SP PUK': 45, 'PH-CORP PIN': 46, 'PH-CORP PUK': 47,
}
CME_SIM_CODES = (SIM_NO_SIM, SIM_PIN, SIM_PUK, SIM_FAIL, SIM_BUSY, SIM_WRONG, SIM_BAD_PASSWD)
SIM_NAMES = {
    SIM_READY: 'ready', SIM_ERROR: 'error', SIM_NO_SIM: 'no SIM', SIM_PIN: 'PIN required',
    SIM_PUK: 'PUK required', SIM_FAIL: 'SIM failure', SIM_BUSY: 'SIM busy',
    SIM_WRONG: 'wrong SIM', SIM_BAD_PASSWD: 'wrong PIN',
}
SIM_LADDER = (SIM_NO_SIM, SIM_FAIL, SIM_BUSY, SIM_ERROR)  # a modem reset may fix these
SIM_HUMAN = (SIM_PUK, SIM_WRONG, SIM_BAD_PASSWD)          # nothing automatic can

# Victron PPP_STATUS enum: the GUI renders 1 as "connecting".
PPP_DOWN = 0
PPP_INIT = 1
PPP_UP = 2

# +COPS? access technology
ACT_NAMES = {0: 'GSM', 1: 'GSM', 2: 'UMTS', 3: 'EDGE', 4: 'HSDPA', 5: 'HSUPA',
             6: 'HSPA', 7: 'LTE', 8: 'LTE', 9: 'LTE'}

# ---- recovery ladder ---------------------------------------------------------

RUNG_RADIO_ON = 'RADIO_ON'    # AT+CFUN=1, only when the radio is off
RUNG_COPS = 'COPS_AUTO'       # AT+COPS=0: back to automatic network selection
RUNG_RESET = 'MODEM_RESET'    # AT^RESET: the only command measured to restart
                              # this firmware, and the only one measured to
                              # escape the state AT+CFUN=4 leaves it in
RUNG_USB = 'USB_REENUM'       # host-side bus reset: re-enumerates the device
                              # but provably does NOT change its internal state
RUNG_HCI = 'HCI_REBIND'       # unbind/bind the modem's own USB controller

# Commands this package must never send. AT+CFUN=4 was measured, in situ, to
# put the modem in a state where every configuration write is refused with
# +CME ERROR: 100 - including the command that would undo it. AT+CFUN=0 is the
# same class, AT+COPS=2 is refused by this firmware anyway, and AT+CGATT=0
# detaches without a guaranteed way back. A watchdog must never create a state
# it may not be able to leave.
FORBIDDEN_PREFIXES = ('AT+CFUN=0', 'AT+CFUN=4', 'AT+CFUN=6', 'AT+CFUN=7',
                      'AT+COPS=2', 'AT+CGATT=0', 'AT^RADIOOFF', 'AT^SYSCFG')

RUNG_MIN_LEVEL = {RUNG_RADIO_ON: 1, RUNG_COPS: 1, RUNG_RESET: 2,
                  RUNG_USB: 3, RUNG_HCI: 4}
# Rungs that can take the modem (and this service with it) off the bus.
RUNG_DESTRUCTIVE = (RUNG_RESET, RUNG_USB, RUNG_HCI)
# Every rung is a single step: there is no window in which the service can be
# killed between two halves of a sequence and leave the modem worse off.
SETTLE = {RUNG_RADIO_ON: 60, RUNG_COPS: 90, RUNG_RESET: 120,
          RUNG_USB: 150, RUNG_HCI: 180}
LADDERS = {
    'at_mute': [RUNG_RESET, RUNG_USB, RUNG_HCI],
    'sim':     [RUNG_RESET, RUNG_USB],
    'reg':     [RUNG_RADIO_ON, RUNG_COPS, RUNG_RESET, RUNG_USB, RUNG_HCI],
    'dial':    [RUNG_COPS, RUNG_RESET, RUNG_USB],
    'traffic': [RUNG_RESET],
}
RESET_MIN_GAP = 600         # never two modem resets within 10 minutes

# Returned by the detector when the modem told us nothing this poll: neither
# "a fault" nor "no fault".
UNKNOWN = object()
# 'traffic' is the only reason that can fire on a link which is otherwise
# perfectly healthy (an operator filtering ICMP looks exactly like a dead
# session), so it gets a long grace period and the mildest possible ladder.
GRACE = {'at_mute': 0, 'sim': 300, 'reg': 120, 'reg_searching': 300,
         'dial': 0, 'traffic': 1800}

log = logging.getLogger()
rlog = logging.getLogger('recovery')

modem_settings = {
    'connect': ['/Settings/Modem/Connect', 1, 0, 1],
    'roaming': ['/Settings/Modem/RoamingPermitted', 0, 0, 1],
    'apn':     ['/Settings/Modem/APN', '', 0, 0],
    'pin':     ['/Settings/Modem/PIN', '', 0, 0],
}


# ---- helpers -----------------------------------------------------------------

def fmt_duration(seconds):
    seconds = int(max(0, seconds))
    if seconds < 60:
        return '%ds' % seconds
    if seconds < 3600:
        return '%dm%02ds' % (seconds // 60, seconds % 60)
    return '%dh%02dm' % (seconds // 3600, (seconds % 3600) // 60)


def parse_cme(line):
    """'+CME ERROR: 10' -> 10, textual errors -> None."""
    try:
        return int(line.split(':', 1)[1].strip())
    except (IndexError, ValueError):
        return None


def _hcsq_scale(v, floor, step, count):
    """Huawei ^HCSQ encoding: 0 = below floor, 1..count linear, count+1 = above
    ceiling, 255 = unknown."""
    if v is None or v == 255:
        return None
    if v <= 0:
        return round(floor, 1)
    if v > count:
        return round(floor + step * (count - 1), 1)
    return round(floor + step * (v - 1), 1)


def hcsq_to_dbm(line):
    """Parse a ^HCSQ response into dBm/dB values. Returns {} when unusable."""
    try:
        body = line.split(':', 1)[1].strip()
        parts = [p.strip().strip('"') for p in body.split(',')]
        mode = parts[0].upper()
        vals = []
        for p in parts[1:]:
            vals.append(int(p) if p != '' else None)
    except (IndexError, ValueError):
        return {}
    vals += [None] * 4
    out = {'mode': mode}
    if mode == 'LTE':
        out['rssi'] = _hcsq_scale(vals[0], -120, 1, 95)
        out['rsrp'] = _hcsq_scale(vals[1], -140, 1, 96)
        out['sinr'] = _hcsq_scale(vals[2], -20, 0.2, 250)
        out['rsrq'] = _hcsq_scale(vals[3], -19.5, 0.5, 34)
    elif mode == 'WCDMA':
        out['rssi'] = _hcsq_scale(vals[0], -120, 1, 95)
        out['rscp'] = _hcsq_scale(vals[1], -120, 1, 95)
        out['ecio'] = _hcsq_scale(vals[2], -32, 0.5, 65)
    elif mode == 'GSM':
        out['rssi'] = _hcsq_scale(vals[0], -120, 1, 95)
    return out


def fmt_signal(sig, csq=None):
    if not sig:
        return 'signal: unknown'
    mode = sig.get('mode', '?')
    if mode == 'NOSERVICE':
        s = 'signal: no service'
    else:
        parts = ['signal: %s' % mode]
        for key, unit in (('rssi', 'dBm'), ('rsrp', 'dBm'), ('rscp', 'dBm'),
                          ('sinr', 'dB'), ('rsrq', 'dB'), ('ecio', 'dB')):
            if key in sig:
                v = sig[key]
                parts.append('%s=%s' % (key, ('%g%s' % (v, unit)) if v is not None else '?'))
        s = ' '.join(parts)
    if csq is not None:
        s += ' csq=%s' % csq
    return s


class Config:
    """Trivial KEY=value file. Never raises; every field has a default."""

    def __init__(self, path=CONFIG_FILE, raw=None):
        cfg = raw if raw is not None else self._read(path)
        self.apn = cfg.get('APN', '') or 'mmsbouygtel.com'
        hosts = cfg.get('PROBE_HOST', '8.8.8.8,1.1.1.1')
        self.probe_hosts = [h.strip() for h in hosts.split(',') if h.strip()]
        self.recovery_level = self._num(cfg.get('RECOVERY_LEVEL'), 4, 0, 4, int)
        self.linkwatch = str(cfg.get('LINKWATCH', '1')).strip() not in ('0', 'no', 'false')
        # Rebinding the USB controller re-enumerates EVERY device on it. By
        # default the rung refuses when the modem is not alone there.
        self.hci_shared_ok = str(cfg.get('HCI_REBIND_SHARED', '0')).strip().lower() \
            in ('1', 'yes', 'true')
        self.timescale = self._num(cfg.get('RECOVERY_TIMESCALE'), 1.0, 0.1, 1.0, float)

    @staticmethod
    def _num(value, default, lo, hi, conv):
        try:
            return max(lo, min(hi, conv(value)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _read(path):
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


class SystemShim:
    """Single point of contact with the OS, so that the test bench can replace
    it. Every method is tolerant: no exception escapes."""

    def sh(self, cmd):
        try:
            return os.system(cmd)
        except Exception:
            return -1

    def run(self, argv, timeout=5):
        try:
            return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except Exception:
            return None

    def read(self, path):
        try:
            with open(path) as f:
                return f.read()
        except Exception:
            return None

    def write(self, path, text):
        """Plain write, for sysfs attributes. write_atomic() uses os.replace()
        and therefore cannot write them at all."""
        try:
            with open(path, 'w') as f:
                f.write(text)
            return True
        except Exception as e:
            log.warning('cannot write %s: %s', path, e)
            return False

    def write_atomic(self, path, text):
        tmp = path + '.tmp'
        try:
            with open(tmp, 'w') as f:
                f.write(text)
            os.replace(tmp, path)
            return True
        except Exception as e:
            log.warning('cannot write %s: %s', path, e)
            return False

    def exists(self, path):
        return os.path.exists(path)

    def listdir(self, path):
        try:
            return os.listdir(path)
        except Exception:
            return []

    def realpath(self, path):
        try:
            return os.path.realpath(path)
        except Exception:
            return path

    def remove(self, path):
        try:
            os.remove(path)
        except Exception:
            pass

    def kill(self, pid, sig=signal.SIGTERM):
        try:
            os.kill(pid, sig)
            return True
        except Exception:
            return False

    def pid_cmdline(self, pid):
        try:
            with open('/proc/%d/cmdline' % pid, 'rb') as f:
                return f.read()
        except Exception:
            return b''

    def spawn_detached(self, argv):
        """Start a helper that must survive this process (new session, no
        inherited descriptors)."""
        try:
            subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, close_fds=True,
                             start_new_session=True)
            return True
        except Exception as e:
            log.error('cannot start %s: %s', argv, e)
            return False

    def ping(self, host, iface):
        try:
            r = subprocess.run(['ping', '-c', '1', '-W', '3', '-I', iface, host],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=8)
            return r.returncode == 0
        except Exception:
            return False

    def boot_id(self):
        return (self.read(BOOT_ID_FILE) or '').strip()


class WdmChannel:
    """Second AT channel of the E3372h (/dev/cdc-wdm0). Used for the heavy
    commands of the recovery ladder so that a slow answer never blocks the
    service and never pollutes the serial port it polls."""

    def __init__(self, dev=WDM_DEV, sysx=None):
        self.dev = dev
        self.sysx = sysx or SystemShim()

    def available(self):
        return self.sysx.exists(self.dev)

    def send(self, cmd, timeout=3.0):
        """Returns ('ok'|'error'|'timeout'|'nodev', text)."""
        if not self.available():
            return 'nodev', ''
        fd = None
        try:
            fd = os.open(self.dev, os.O_RDWR | os.O_NONBLOCK)
            os.write(fd, (cmd + '\r').encode())
            buf = b''
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                r, _, _ = select.select([fd], [], [], 0.5)
                if not r:
                    continue
                try:
                    buf += os.read(fd, 4096)
                except BlockingIOError:
                    continue
                if b'\r\nOK\r\n' in buf or b'\nOK\r' in buf or buf.strip().endswith(b'OK'):
                    return 'ok', buf.decode(errors='replace')
                if b'ERROR' in buf:
                    return 'error', buf.decode(errors='replace')
            return 'timeout', buf.decode(errors='replace')
        except Exception as e:
            log.warning('wdm %s: %s', cmd, e)
            return 'nodev', ''
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except Exception:
                    pass


class E3372Modem:
    def __init__(self, dev, sysx=None, stop_event=None):
        self.dev = dev
        self.ser = None
        self.lock = threading.Lock()
        self.sysx = sysx or SystemShim()
        self.stop_event = stop_event or threading.Event()
        self.last_error = None

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

    def responded(self):
        """True when the last command got any answer from the modem (OK, ERROR
        or CME), False when it timed out or the port is gone."""
        return self.last_error not in ('timeout', 'io', 'nodev')

    def at(self, cmd, timeout=3):
        """Send AT command and return response lines (excluding echo and OK/ERROR).

        Returns None when the command failed or the modem did not answer;
        self.last_error then says why: a CME code (int), 'error',
        'unsupported', 'timeout', 'io' or 'nodev'. The caller must treat None
        as "unknown", never as "not connected".
        """
        self.last_error = None
        with self.lock:
            if self.stop_event.is_set():
                self.last_error = 'io'
                return None
            # A vanished device node must fail fast, not cost a timeout per call.
            if not self.sysx.exists(self.dev):
                self.close()
                self.last_error = 'nodev'
                return None
            # The port can die under us (USB glitch, modem reset); reopen it
            # rather than staying broken until the service is restarted.
            if self.ser is None and not self.open():
                self.last_error = 'io'
                return None
            # For Huawei commands only the matching ^XXX: line is a response;
            # any other ^XXX: line is an unsolicited report.
            want = None
            if cmd.startswith('AT^'):
                want = cmd[2:].split('=')[0].split('?')[0] + ':'
            try:
                self.ser.reset_input_buffer()
                self.ser.write((cmd + '\r').encode())
                time.sleep(0.3)

                lines = []
                # Monotonic: this GX has no NTP and its wall clock is hours
                # off, so a step of the wall clock must never turn a 3 s
                # timeout into an infinite wait (or an instant one).
                end_time = time.monotonic() + timeout
                while time.monotonic() < end_time:
                    if self.stop_event.is_set():
                        self.last_error = 'io'
                        return None
                    if self.ser.in_waiting:
                        raw = self.ser.readline()
                        line = raw.decode(errors='replace').strip()
                        if not line:
                            continue
                        if line == cmd:  # echo
                            continue
                        if line == 'OK':
                            return lines
                        if line == 'ERROR':
                            self.last_error = 'error'
                            log.warning('%s -> %s', cmd, line)
                            return None
                        if line.startswith('+CME ERROR') or line.startswith('+CMS ERROR'):
                            code = parse_cme(line)
                            self.last_error = code if code is not None else 'error'
                            log.warning('%s -> %s', cmd, line)
                            return None
                        if line == 'COMMAND NOT SUPPORT':
                            self.last_error = 'unsupported'
                            log.warning('%s -> not supported', cmd)
                            return None
                        if line.startswith('^'):
                            if want and line.startswith(want):
                                lines.append(line)
                            else:
                                log.debug('unsolicited: %s', line)
                            continue
                        lines.append(line)
                    else:
                        time.sleep(0.1)

                log.warning('%s -> timeout', cmd)
                if lines:
                    return lines
                self.last_error = 'timeout'
                return None
            except Exception as e:
                log.error('Serial error on %s: %s', cmd, e)
                self.last_error = 'io'
                self.close()
                return None


class Recovery:
    """State machine of the recovery ladder. Owns no I/O: modem actions go
    through `actions` (the ModemService), files through `sysx`.

    States: idle -> grace -> rung -> idle | wait -> rung ...
    """

    def __init__(self, actions, sysx, level=4, timescale=1.0, now=0.0):
        self.actions = actions
        self.sysx = sysx
        self.level = level
        self.ts = timescale
        self.state = 'idle'
        self.reason = None
        self.stuck_since = None
        self.rung_idx = -1
        self.step = 0
        self.phase = 'step'          # 'step' | 'settle' | 'next'
        self.verify_started = False
        self.busy_until = 0.0
        self.next_ladder_at = 0.0
        self.ladder_count = 0
        self.ladder_started_at = None
        self.healthy_since = now
        self.history = []
        self.seen = {}
        self.mute_polls = 0
        self.last_snap = None
        self.last_nag = 0.0
        self.tick_errors = 0

    # ---- persistence ----

    def save(self, now):
        data = {
            'boot_id': self.sysx.boot_id(), 'saved_at': now,
            'state': self.state, 'reason': self.reason, 'stuck_since': self.stuck_since,
            'rung_idx': self.rung_idx, 'step': self.step, 'phase': self.phase,
            'busy_until': self.busy_until, 'next_ladder_at': self.next_ladder_at,
            'ladder_count': self.ladder_count, 'ladder_started_at': self.ladder_started_at,
            'history': self.history[-20:],
            # The devnum recorded before the current destructive rung: the
            # rung usually kills this service, so the next instance needs it
            # to judge whether the modem really re-enumerated.
            'reset_devnum': getattr(self.actions, 'reset_devnum', None),
            'last_reset_at': getattr(self.actions, 'last_reset_at', None),
        }
        self.sysx.write_atomic(STATE_FILE, json.dumps(data))

    def load(self, now):
        text = self.sysx.read(STATE_FILE)
        if not text:
            rlog.info('recovery: state idle (no saved state)')
            return
        try:
            data = json.loads(text)
        except ValueError:
            rlog.warning('recovery: saved state unreadable, ignored')
            return
        if data.get('boot_id') != self.sysx.boot_id():
            rlog.info('recovery: saved state from another boot, ignored')
            return
        self.state = data.get('state', 'idle')
        self.reason = data.get('reason')
        self.stuck_since = data.get('stuck_since')
        self.rung_idx = data.get('rung_idx', -1)
        self.step = data.get('step', 0)
        self.phase = data.get('phase', 'settle')
        self.busy_until = data.get('busy_until', 0.0)
        self.next_ladder_at = data.get('next_ladder_at', 0.0)
        self.ladder_count = data.get('ladder_count', 0)
        self.ladder_started_at = data.get('ladder_started_at')
        self.history = data.get('history', [])
        if data.get('reset_devnum') is not None:
            self.actions.reset_devnum = data['reset_devnum']
        if data.get('last_reset_at') is not None:
            self.actions.last_reset_at = data['last_reset_at']
        if self.state == 'rung' and (self.reason not in LADDERS
                                     or self.rung_idx >= len(LADDERS[self.reason])):
            self.state = 'idle'
            self.reason = None
        if self.state == 'idle':
            self.healthy_since = now
        rlog.info('recovery: state %s (loaded, ladder_count %d, reason %s, busy for %s)',
                  self.state, self.ladder_count, self.reason,
                  fmt_duration(self.busy_until - now))

    # ---- observation ----

    def _reason_of(self, snap):
        if not snap.get('connect_wanted', True):
            return None
        if self.mute_polls >= AT_MUTE_POLLS:
            return 'at_mute'
        if not snap.get('at_alive'):
            # The modem said nothing this poll: that is not evidence the fault
            # has cleared. Returning None here used to wipe the reason and
            # restart the grace timer, so an intermittently silent modem could
            # never reach the ladder at all.
            return UNKNOWN
        sim = snap.get('sim')
        if sim in SIM_LADDER:
            return 'sim'
        if sim != SIM_READY:
            return None
        reg = snap.get('reg')
        if reg is None:
            return None
        if reg not in REGISTERED:
            return 'reg'
        if reg == REG_ROAMING and not snap.get('roaming_allowed', True):
            return None
        if snap.get('dial_failures', 0) >= DIAL_FAILURES_MAX:
            return 'dial'
        if snap.get('probe_exhausted'):
            return 'traffic'
        return None

    def _grace(self, snap):
        if self.reason == 'reg' and snap.get('reg') == REG_SEARCHING:
            return GRACE['reg_searching'] * self.ts
        return GRACE.get(self.reason, 0) * self.ts

    def _cleared(self, snap):
        r = self.reason
        if snap is None:
            return False
        if r == 'at_mute':
            return bool(snap.get('at_alive'))
        if r == 'sim':
            return snap.get('sim') in (SIM_READY, SIM_PIN)
        if r == 'reg':
            return snap.get('reg') in REGISTERED
        if r == 'dial':
            return bool(snap.get('ncm'))
        if r == 'traffic':
            return self.actions.traffic_ok()
        return True

    def observe(self, snap, now):
        """Feed the poll snapshot. Detectors stand still during a settle window."""
        self.last_snap = snap
        if now < self.busy_until:
            return
        self.mute_polls = 0 if snap.get('at_alive') else self.mute_polls + 1
        r = self._reason_of(snap)
        if r is UNKNOWN:
            # Keep stuck_since, seen and the current reason untouched; only
            # mute_polls keeps counting, towards the at_mute reason.
            return
        if r is None:
            self.seen.clear()
        else:
            self.seen.setdefault(r, now)
            for k in list(self.seen):
                if k != r:
                    del self.seen[k]
        if self.state == 'rung':
            return
        if r == self.reason:
            return
        if r is None:
            rlog.info('recovery: [%s] cleared after %s', self.reason,
                      fmt_duration(now - (self.stuck_since or now)))
            self._to_idle(now)
            return
        old = self.reason
        self.reason = r
        self.stuck_since = self.seen[r]
        self.state = 'grace'
        rlog.warning('recovery: [%s] stuck (%s)%s, grace %s', r, self._detail(snap),
                     '' if old is None else ' (was %s)' % old,
                     fmt_duration(self._grace(snap)))
        self.actions.on_stuck(r, snap)

    def _detail(self, snap):
        if snap is None:
            return '?'
        r = self.reason
        if r == 'at_mute':
            return 'no AT answer for %d polls' % self.mute_polls
        if r == 'sim':
            return 'SIM %s' % SIM_NAMES.get(snap.get('sim'), snap.get('sim'))
        if r == 'reg':
            return 'CREG %s %s' % (snap.get('reg'), REG_NAMES.get(snap.get('reg'), ''))
        if r == 'dial':
            return '%d consecutive dial failures' % snap.get('dial_failures', 0)
        if r == 'traffic':
            return 'session up but no traffic'
        return r

    # ---- tick ----

    def tick(self, now):
        try:
            self._tick(now)
            self.tick_errors = 0
        except Exception:
            self.tick_errors += 1
            rlog.exception('recovery: tick failed (%d/%d)', self.tick_errors, TICK_ERRORS_MAX)
            if self.tick_errors >= TICK_ERRORS_MAX:
                rlog.critical('recovery: too many failures, resetting to idle')
                self.tick_errors = 0
                self.busy_until = 0.0
                self._to_idle(now)

    def _tick(self, now):
        if self.last_snap is None:
            return
        if self.state == 'idle':
            if self.ladder_count and self.healthy_since is not None \
                    and now - self.healthy_since >= HEALTHY_RESET_AFTER * self.ts:
                rlog.info('recovery: healthy for %s, ladder counter cleared',
                          fmt_duration(now - self.healthy_since))
                self.ladder_count = 0
                self.next_ladder_at = 0.0
                self.save(now)
            return
        if self.state == 'grace':
            if now - self.stuck_since < self._grace(self.last_snap):
                return
            if now >= self.next_ladder_at:
                self._start_ladder(now)
            else:
                self.state = 'wait'
                rlog.warning('recovery: [%s] grace over, next ladder allowed in %s (ladder #%d)',
                             self.reason, fmt_duration(self.next_ladder_at - now),
                             self.ladder_count + 1)
            return
        if self.state == 'wait':
            if now >= self.next_ladder_at:
                self._start_ladder(now)
            elif now - self.last_nag >= NAG_INTERVAL * self.ts:
                self.last_nag = now
                rlog.warning('recovery: [%s] still stuck (%s), next ladder in %s (ladder #%d)',
                             self.reason, self._detail(self.last_snap),
                             fmt_duration(self.next_ladder_at - now), self.ladder_count + 1)
            return
        if self.state == 'rung':
            if now < self.busy_until:
                return
            if self.phase == 'step':
                self._run_step(now)
            elif self.phase == 'next':
                self._advance_rung(now)
            else:  # settle over: verify, then judge
                ladder = self._ladder()
                rung = ladder[self.rung_idx] if 0 <= self.rung_idx < len(ladder) else None
                # A rung that answered OK but provably changed nothing (the
                # modem never left the bus) must not burn the rest of the
                # settle: escalate straight away.
                if rung and not self.verify_started \
                        and not self.actions.rung_was_effective(rung):
                    if self.history:
                        self.history[-1]['result'] = 'ineffective'
                    self._advance_rung(now)
                    return
                # Both 'dial' and 'traffic' are judged on a session that the
                # rung has just torn down, so they need a dial before the
                # verdict - otherwise they can never be scored a success.
                if self.reason in ('dial', 'traffic') and not self.verify_started:
                    if self.actions.verify_dial(now):
                        self.verify_started = True
                        self.busy_until = now + VERIFY_WINDOW
                        return
                if self._cleared(self.last_snap):
                    self._finish_ladder(now, True)
                else:
                    self._advance_rung(now)

    # ---- ladder mechanics ----

    def _ladder(self):
        return LADDERS.get(self.reason, [])

    def _destructive_recent(self, now):
        return sum(1 for h in self.history
                   if h.get('rung') in RUNG_DESTRUCTIVE and h.get('result') == 'sent'
                   and now - h.get('t', 0) < DESTRUCTIVE_WINDOW * self.ts)

    def _start_ladder(self, now):
        self.state = 'rung'
        self.rung_idx = -1
        self.ladder_started_at = now
        rlog.warning('recovery: [%s] ladder #%d starting (%s, stuck for %s, level %d)',
                     self.reason, self.ladder_count + 1, self._detail(self.last_snap),
                     fmt_duration(now - (self.stuck_since or now)), self.level)
        self._advance_rung(now)

    def _destructive_free_at(self, now):
        """When the destructive budget frees up, or None if it is free now."""
        stamps = sorted(h.get('t', 0) for h in self.history
                        if h.get('rung') in RUNG_DESTRUCTIVE and h.get('result') == 'sent'
                        and now - h.get('t', 0) < DESTRUCTIVE_WINDOW * self.ts)
        if len(stamps) < DESTRUCTIVE_MAX:
            return None
        return stamps[-DESTRUCTIVE_MAX] + DESTRUCTIVE_WINDOW * self.ts

    def _defer_ladder(self, now, until):
        """Postpone a ladder whose every rung is currently blocked by the
        destructive budget, WITHOUT counting it: counting it would double the
        backoff for a ladder that never ran a single command."""
        self.next_ladder_at = until
        self.state = 'wait'
        self.last_nag = now
        self.rung_idx = -1
        rlog.warning('recovery: [%s] ladder #%d deferred, every rung is held by '
                     'the reset budget; retrying in %s', self.reason,
                     self.ladder_count + 1, fmt_duration(until - now))
        self.save(now)

    def _advance_rung(self, now):
        ladder = self._ladder()
        idx = self.rung_idx + 1
        held_until = None
        while idx < len(ladder):
            rung = ladder[idx]
            if RUNG_MIN_LEVEL[rung] > self.level:
                rlog.info('recovery: [%s] rung %d/%d %s skipped (RECOVERY_LEVEL=%d)',
                          self.reason, idx + 1, len(ladder), rung, self.level)
                idx += 1
                continue
            if rung in RUNG_DESTRUCTIVE and self._destructive_recent(now) >= DESTRUCTIVE_MAX:
                free_at = self._destructive_free_at(now)
                if free_at is not None:
                    held_until = free_at if held_until is None else min(held_until, free_at)
                rlog.error('recovery: [%s] rung %d/%d %s held: %d resets in the last %s',
                           self.reason, idx + 1, len(ladder), rung,
                           DESTRUCTIVE_MAX, fmt_duration(DESTRUCTIVE_WINDOW * self.ts))
                idx += 1
                continue
            break
        if idx >= len(ladder):
            if held_until is not None and self.rung_idx < 0:
                # Nothing ran at all, only the budget stood in the way.
                self._defer_ladder(now, held_until)
                return
            self._finish_ladder(now, False)
            return
        self.rung_idx = idx
        self.step = 0
        self.phase = 'step'
        self.verify_started = False
        self._run_step(now)

    def _run_step(self, now):
        """Execute the current rung. Every rung is a single command, so there
        is no half-finished sequence to recover from."""
        ladder = self._ladder()
        rung = ladder[self.rung_idx]
        destructive = rung in RUNG_DESTRUCTIVE
        if destructive:
            # The service may not survive this command (the modem leaves the
            # bus and serial-starter kills us): take the measurement the next
            # instance will need, then persist, then act.
            self.actions.prepare_rung(rung, now)
            self.phase = 'settle'
            self.busy_until = now + SETTLE[rung] * self.ts
            self.history.append({'t': now, 'reason': self.reason,
                                 'rung': rung, 'result': 'sent'})
            self.save(now)

        result = self.actions.run_rung_step(rung, 0, now)
        rlog.warning('recovery: [%s] ladder #%d rung %d/%d %s -> %s',
                     self.reason, self.ladder_count + 1, self.rung_idx + 1,
                     len(ladder), rung, result)

        if result == 'sent':
            if not destructive:
                self.phase = 'settle'
                self.busy_until = now + SETTLE[rung] * self.ts
                self.history.append({'t': now, 'reason': self.reason,
                                     'rung': rung, 'result': 'sent'})
            return

        # refused / impossible / ineffective: go to the next rung without
        # burning the settle time.
        if destructive:
            self.history[-1]['result'] = result
            self.save(now)
        else:
            self.history.append({'t': now, 'reason': self.reason,
                                 'rung': rung, 'result': result})
        self.phase = 'next'
        self.busy_until = now + STEP_GAP

    def _finish_ladder(self, now, success):
        ladder = self._ladder()
        rung = ladder[self.rung_idx] if 0 <= self.rung_idx < len(ladder) else '-'
        elapsed = fmt_duration(now - (self.stuck_since or now))
        if success:
            rlog.warning('recovery: [%s] recovered at rung %d/%d %s after %s',
                         self.reason, self.rung_idx + 1, len(ladder), rung, elapsed)
            self.history.append({'t': now, 'reason': self.reason, 'rung': rung, 'result': 'recovered'})
            reason = self.reason
            self._to_idle(now)
            self.actions.on_recovered(reason)
            return
        if self.reason == 'traffic':
            rlog.error('recovery: [traffic] the radio cycle did not restore traffic after %s, '
                       'suspending the probe for %s', elapsed, fmt_duration(PROBE_SUSPEND * self.ts))
            self._to_idle(now)
            self.actions.on_traffic_exhausted(now)
            return
        self.ladder_count += 1
        backoff = min(LADDER_BACKOFF_MIN * (2 ** (self.ladder_count - 1)), LADDER_BACKOFF_MAX) * self.ts
        self.next_ladder_at = now + backoff
        self.state = 'wait'
        self.last_nag = now
        rlog.error('recovery: [%s] ladder #%d exhausted after %d rungs (%s), next ladder in %s',
                   self.reason, self.ladder_count, self.rung_idx + 1,
                   fmt_duration(now - (self.ladder_started_at or now)), fmt_duration(backoff))
        self.save(now)

    def _to_idle(self, now):
        self.state = 'idle'
        self.reason = None
        self.stuck_since = None
        self.rung_idx = -1
        self.step = 0
        self.phase = 'step'
        self.verify_started = False
        self.healthy_since = now
        self.seen.clear()
        self.save(now)

    def cancel(self, now, why):
        if self.state != 'idle':
            rlog.warning('recovery: [%s] cancelled (%s)', self.reason, why)
            self._to_idle(now)

    def allows_session_watchdog(self, now):
        return self.state in ('idle', 'grace', 'wait') and now >= self.busy_until

    def current_rung(self):
        ladder = self._ladder()
        if self.state == 'rung' and 0 <= self.rung_idx < len(ladder):
            return ladder[self.rung_idx]
        return ''

    def describe(self, now):
        return {
            '/Recovery/State': self.state,
            '/Recovery/Reason': self.reason or '',
            '/Recovery/Rung': self.current_rung(),
            '/Recovery/LadderCount': self.ladder_count,
            '/Recovery/NextLadderIn': int(max(0, self.next_ladder_at - now)) if self.state == 'wait' else 0,
        }


class ModemService:
    def __init__(self, dev, cfg=None, sysx=None, wdm=None):
        self.cfg = cfg or Config()
        self.sysx = sysx or SystemShim()
        self.stop_event = threading.Event()
        self.modem = E3372Modem(dev, self.sysx, self.stop_event)
        self.wdm = wdm or WdmChannel(WDM_DEV, self.sysx)
        self.dbus = None
        self.settings = None
        self.ncm_connected = False
        self.sim_status = None
        self.reg_status = None
        self.identified = False
        self.at_alive = False
        self.resync_needed = False
        # session watchdog state
        self.redial_count = 0
        self.next_redial = 0.0
        self.dial_deadline = None
        self.dial_started_at = None
        self.dial_failures = 0
        self.session_up_at = None
        self.no_ip_since = None
        self.last_dhcp_restart = 0.0
        self.dhcp_started_at = -1e9
        # probe state
        self.probe_failures = 0
        self.probe_redials = 0
        self.probe_exhausted = False
        self.probe_runs = 0
        self.probe_runs_at_redial = 0
        self.last_rx = None
        self.probe_suspended_until = 0.0
        self.last_probe = 0.0
        self.probe_thread = None
        # SIM PIN
        self.pin_attempted = False
        # recovery bookkeeping
        self.reset_devnum = None
        self.last_reset_at = -1e9
        self.cfun = None
        # logging
        self.signal = {}
        self.last_signal_log = 0.0
        self.nags = {}
        # recovery ladder
        self.recovery = Recovery(self, self.sysx, self.cfg.recovery_level,
                                 self.cfg.timescale, time.monotonic())

    # ---- lifecycle -----------------------------------------------------------

    def start(self):
        dbus.mainloop.glib.threads_init()
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

        self.dbus = VeDbusService('com.victronenergy.modem', register=False)
        for path, value in (('/Model', None), ('/IMEI', None), ('/NetworkName', None),
                            ('/NetworkType', None), ('/SignalStrength', None), ('/Roaming', None),
                            ('/Connected', 0), ('/IP', None), ('/SimStatus', None),
                            ('/RegStatus', None), ('/PPPStatus', 0),
                            ('/Recovery/State', 'idle'), ('/Recovery/Reason', ''),
                            ('/Recovery/Rung', ''), ('/Recovery/LadderCount', 0),
                            ('/Recovery/NextLadderIn', 0), ('/Modem/Cfun', None),
                            ('/Signal/Rsrp', None), ('/Signal/Sinr', None), ('/Signal/Rsrq', None)):
            self.dbus.add_path(path, value)

        # The previous instance may still hold the name for a moment after a
        # restart: retry instead of dying and leaving serial-starter to loop.
        deadline = time.monotonic() + REGISTER_RETRY_FOR
        while True:
            try:
                self.dbus.register()
                break
            except Exception as e:
                if time.monotonic() >= deadline or self.stop_event.is_set():
                    log.error('D-Bus registration failed: %s', e)
                    return False
                log.warning('D-Bus name busy (%s), retrying', e)
                time.sleep(2)
        log.info('Registered on D-Bus as com.victronenergy.modem')

        self.settings = SettingsDevice(self.dbus.dbusconn, modem_settings,
                                       self.setting_changed, timeout=10)

        self.recovery.load(time.monotonic())
        self._repair_usb_intent()
        self._ensure_linkwatch()

        if not self.modem.open():
            # Keep running: the port is reopened at every poll and the recovery
            # ladder can act on a dead modem.
            log.error('Failed to open modem, will keep trying')

        self._init_modem()

        GLib.timeout_add(POLL_INTERVAL * 1000, self._update)
        return True

    def stop(self):
        self.stop_event.set()

    def setting_changed(self, setting, old, new):
        log.info('Setting %s changed: %s -> %s', setting, old, new)
        now = time.monotonic()
        if setting == 'apn':
            self._hangup()
            if self._connect_wanted():
                self._reset_backoff()
                self._dial_start(now)
        elif setting == 'connect':
            if new:
                self._reset_backoff()
                self._dial_start(now)
            else:
                self._hangup()
                self.recovery.cancel(now, '/Settings/Modem/Connect switched off')
        elif setting == 'pin':
            self.pin_attempted = False

    # ---- identification ------------------------------------------------------

    def _identify(self):
        """Modem identification (/Model, /IMEI). Retried from _update until it
        succeeds: a single AT timeout at startup must not leave both empty
        forever."""
        if self.modem.at('AT') is None:
            return False
        self.modem.at('AT+CMEE=1')     # numeric error codes
        self.modem.at('AT^CURC=0')     # no unsolicited ^RSSI/^HCSQ/^MODE reports

        r = self.modem.at('AT+CGMM')
        if r:
            for line in r:
                if not line.startswith('+') and not line.startswith('^'):
                    self.dbus['/Model'] = line
                    log.info('Model: %s', line)
                    break

        r = self.modem.at('AT+CGSN')
        if r:
            for line in r:
                if not line.startswith('+') and not line.startswith('^'):
                    self.dbus['/IMEI'] = line
                    log.info('IMEI: %s', line)
                    break

        self.identified = (self.dbus['/Model'] is not None
                           and self.dbus['/IMEI'] is not None)
        return self.identified

    def _init_modem(self):
        """Initial modem identification and NCM setup."""
        now = time.monotonic()
        if not self._identify():
            log.error('Modem not responding, will retry')
            return

        self._update_status()
        if self.ncm_connected:
            # A restart of the service must not cost the data session (and the
            # remote access that rides on it).
            rlog.info('session: already up, keeping it')
            self.session_up_at = now
            self._ensure_dhcp()
        elif self._connect_wanted() and self.sim_status == SIM_READY \
                and (self.reg_status == REG_HOME
                     or (self.reg_status == REG_ROAMING and self._roaming_allowed())):
            self._dial_start(now)
        # Otherwise the session watchdog dials as soon as the modem is
        # registered, and the recovery ladder handles the rest.

    # ---- NCM session ---------------------------------------------------------

    def _apn(self):
        apn = self.settings['apn'] if self.settings else ''
        return apn or self.cfg.apn

    def _connect_wanted(self):
        """True unless the user turned the connection off in the UI."""
        if self.settings is None:
            return True
        return bool(self.settings['connect'])

    def _roaming_allowed(self):
        if self.settings is None:
            return True
        return bool(self.settings['roaming'])

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

    def _dial_start(self, now):
        """First phase of a dial (a few seconds): hang up, make sure the old
        session is really down, request a new one. The outcome is observed by
        _poll_dial at the next polls."""
        if self.dial_deadline is not None:
            return False
        apn = self._apn()
        rlog.info('session: dial APN=%s', apn)
        self.dbus['/PPPStatus'] = PPP_INIT

        self.modem.at('AT+CGDCONT=1,"IP","%s"' % apn)
        # Always hang up first: NDISDUP on an already-half-open context is
        # silently ignored by the E3372h firmware, and a new session requested
        # while the old one is still being torn down dies with it.
        self.modem.at('AT^NDISDUP=1,0', timeout=5)
        self.ncm_connected = True
        for _ in range(6):
            if not self._query_ncm() or not self.modem.responded():
                break
            time.sleep(0.5)
        self.ncm_connected = False
        self.session_up_at = None
        self.modem.at('AT^NDISDUP=1,1,"%s"' % apn, timeout=10)
        self.dial_started_at = now
        self.dial_deadline = now + DIAL_TIMEOUT
        return True

    def _poll_dial(self, now):
        """Second phase of a dial: judge it on the session state refreshed by
        _update_status."""
        if self.dial_deadline is None:
            return
        if self.ncm_connected:
            rlog.info('session: up %s after dial, (re)starting DHCP on %s',
                      fmt_duration(now - (self.dial_started_at or now)), IFACE)
            self.dial_deadline = None
            self.session_up_at = now
            self.dial_failures = 0
            self.no_ip_since = None
            self._restart_dhcp()
            self.dbus['/PPPStatus'] = PPP_UP
        elif now >= self.dial_deadline:
            self.dial_deadline = None
            self.dial_failures += 1
            rlog.warning('session: dial failed, session still down (%d consecutive)',
                         self.dial_failures)
            self.dbus['/PPPStatus'] = PPP_DOWN

    def _cancel_dial(self):
        self.dial_deadline = None

    def _hangup(self):
        rlog.info('session: hangup')
        self._cancel_dial()
        self.modem.at('AT^NDISDUP=1,0', timeout=5)
        self.ncm_connected = False
        self.session_up_at = None
        self.dbus['/PPPStatus'] = PPP_DOWN
        self._stop_dhcp()

    # ---- DHCP client ---------------------------------------------------------

    def _dhcp_pid(self):
        """PID of the udhcpc instance owning IFACE, or None."""
        try:
            pid = int((self.sysx.read(DHCP_PIDFILE) or '').strip())
        except ValueError:
            return None
        if b'udhcpc' in self.sysx.pid_cmdline(pid):
            return pid
        return None

    def _ensure_dhcp(self, now=None):
        if self._dhcp_pid():
            return
        # udhcpc is started in the background and writes its pidfile a moment
        # later, so _dhcp_pid() still says "nothing running" for a second or
        # two. Without this guard the very next poll starts a second client on
        # the same interface, and the two fight over the lease.
        if now is None:
            now = time.monotonic()
        if now - self.dhcp_started_at < DHCP_START_GRACE:
            return
        self.dhcp_started_at = now
        log.info('starting udhcpc on %s', IFACE)
        # No -q: udhcpc stays resident and renews the lease.
        self.sysx.sh('udhcpc -i %s -b -p %s -t 8 -T 3 -A 15 >/dev/null 2>&1 &'
                     % (IFACE, DHCP_PIDFILE))

    def _stop_dhcp(self):
        pid = self._dhcp_pid()
        if pid:
            self.sysx.kill(pid)
        self.sysx.remove(DHCP_PIDFILE)

    def _restart_dhcp(self):
        self.dhcp_started_at = -1e9      # a deliberate restart is never held
        self._stop_dhcp()
        time.sleep(0.5)
        self.sysx.sh('ip link set %s up 2>/dev/null' % IFACE)
        # The old address and its default route survive a dropped session and
        # would otherwise black-hole every packet.
        self.sysx.sh('ip -4 addr flush dev %s 2>/dev/null' % IFACE)
        self._ensure_dhcp()

    def _iface_ip(self):
        result = self.sysx.run(['ip', '-4', 'addr', 'show', IFACE], timeout=5)
        if result is None:
            return ''
        for line in result.stdout.split('\n'):
            if 'inet ' in line:
                return line.strip().split()[1].split('/')[0]
        return ''

    # ---- status --------------------------------------------------------------

    def _nag(self, key, level, msg, now):
        if now - self.nags.get(key, -NAG_INTERVAL) < NAG_INTERVAL:
            return
        self.nags[key] = now
        rlog.log(level, msg)

    def _set_sim(self, sim, now):
        if sim == self.sim_status:
            return False
        rlog.warning('sim: %s -> %s', self._sim_name(self.sim_status), self._sim_name(sim))
        self.sim_status = sim
        self.dbus['/SimStatus'] = sim
        if sim == SIM_READY:
            self.pin_attempted = False
        return True

    @staticmethod
    def _sim_name(sim):
        if sim is None:
            return 'unknown'
        return '%s (%s)' % (sim, SIM_NAMES.get(sim, 'code %s' % sim))

    @staticmethod
    def _reg_name(reg):
        if reg is None:
            return 'unknown'
        return '%s (%s)' % (reg, REG_NAMES.get(reg, '?'))

    def _set_reg(self, reg):
        if reg == self.reg_status:
            return False
        rlog.warning('reg: %s -> %s', self._reg_name(self.reg_status), self._reg_name(reg))
        self.reg_status = reg
        self.dbus['/RegStatus'] = reg
        self.dbus['/Roaming'] = (reg == REG_ROAMING)
        return True

    def _publish_no_network(self):
        self.ncm_connected = False
        self.dbus['/SignalStrength'] = 0
        self.dbus['/NetworkName'] = ''
        self.dbus['/NetworkType'] = ''
        self.dbus['/Connected'] = 0
        self.dbus['/PPPStatus'] = PPP_DOWN
        self.dbus['/IP'] = ''

    def _handle_pin(self, now):
        pin = (self.settings['pin'] if self.settings else '') or ''
        if not pin:
            self._nag('pin', logging.ERROR,
                      'sim: PIN required but /Settings/Modem/PIN is empty', now)
            return
        if self.pin_attempted:
            return
        self.pin_attempted = True
        rlog.info('sim: PIN required, sending it')
        if self.modem.at('AT+CPIN="%s"' % pin, timeout=10) is not None:
            rlog.info('sim: PIN accepted')
            return
        err = self.modem.last_error
        if err == SIM_BAD_PASSWD or err == 'error':
            # One wrong attempt is one too many: three lock the SIM (PUK).
            rlog.error('sim: wrong PIN, clearing /Settings/Modem/PIN')
            self.settings['pin'] = ''
            self._set_sim(SIM_BAD_PASSWD, now)
        else:
            rlog.error('sim: PIN entry failed (%s)', err)

    def _update_status(self):
        """Query modem status and update D-Bus values. Stops at the first
        timeout: a mute modem must not cost one timeout per command."""
        now = time.monotonic()
        changed = False

        if self.resync_needed:
            self.modem.at('AT', timeout=1)
            self.resync_needed = False

        # SIM status
        r = self.modem.at('AT+CPIN?')
        if r is None:
            if not self.modem.responded():
                self.at_alive = False
                return
            err = self.modem.last_error
            sim = err if err in CME_SIM_CODES else SIM_ERROR
        else:
            text = ''
            for line in r:
                if '+CPIN:' in line:
                    text = line.split(':', 1)[1].strip()
            sim = CPIN_TEXT.get(text, SIM_ERROR)
        self.at_alive = True
        if sim == SIM_PIN and self.sim_status == SIM_BAD_PASSWD \
                and not ((self.settings['pin'] if self.settings else '') or ''):
            # Keep "wrong PIN" on display until a new PIN is entered: the
            # modem itself only ever says "PIN required".
            sim = SIM_BAD_PASSWD
        changed |= self._set_sim(sim, now)

        if sim == SIM_PIN:
            self._handle_pin(now)
        elif sim in SIM_HUMAN:
            self._nag('sim-human', logging.ERROR,
                      'sim: %s, human intervention needed' % self._sim_name(sim), now)

        if sim != SIM_READY:
            changed |= self._set_reg(None)
            self._publish_no_network()
            self.signal = {}
            return

        # Radio state. The package never switches the radio off, but something
        # else might have (a manual command, a firmware quirk), and until v1.4
        # the service had no way to see it at all.
        cfun = self._query_cfun()
        if cfun is not None and cfun != self.cfun:
            rlog.warning('radio: CFUN %s -> %s', self.cfun, cfun)
            self.cfun = cfun
        self.dbus['/Modem/Cfun'] = self.cfun
        if not self.modem.responded():
            return

        # Signal strength
        r = self.modem.at('AT+CSQ')
        if not self.modem.responded():
            return
        csq = None
        if r:
            for line in r:
                if '+CSQ:' in line:
                    try:
                        csq = int(line.split(':')[1].strip().split(',')[0])
                        self.dbus['/SignalStrength'] = csq
                    except ValueError:
                        pass

        # Registration status
        r = self.modem.at('AT+CREG?')
        if not self.modem.responded():
            return
        if r:
            for line in r:
                if '+CREG:' in line:
                    parts = line.split(':')[1].strip().split(',')
                    try:
                        stat = int(parts[1]) if len(parts) > 1 else int(parts[0])
                        changed |= self._set_reg(stat)
                    except ValueError:
                        pass

        # Operator
        r = self.modem.at('AT+COPS?')
        if not self.modem.responded():
            return
        if r:
            for line in r:
                if '+COPS:' in line:
                    parts = line.split(',')
                    if len(parts) >= 3:
                        self.dbus['/NetworkName'] = parts[2].strip('" ')
                        if len(parts) >= 4:
                            try:
                                self.dbus['/NetworkType'] = ACT_NAMES.get(int(parts[3]), 'Unknown')
                            except ValueError:
                                self.dbus['/NetworkType'] = 'Unknown'
                    else:
                        self.dbus['/NetworkName'] = ''
                        self.dbus['/NetworkType'] = ''

        # NCM connection status - the only source of truth for the data
        # session. An address left over on wwan0 proves nothing.
        was = self.ncm_connected
        self._query_ncm()
        if not self.modem.responded():
            return
        changed |= (was != self.ncm_connected)

        self.dbus['/IP'] = self._iface_ip()
        self.dbus['/Connected'] = 1 if self.ncm_connected else 0
        if self.ncm_connected:
            self.dbus['/PPPStatus'] = PPP_UP
        else:
            self.dbus['/PPPStatus'] = PPP_INIT if self.dial_deadline is not None else PPP_DOWN

        if changed or now - self.last_signal_log >= SIGNAL_LOG_INTERVAL:
            self._log_signal(now, csq)

    def _log_signal(self, now, csq=None):
        self.last_signal_log = now
        r = self.modem.at('AT^HCSQ?')
        if r:
            for line in r:
                if line.startswith('^HCSQ:'):
                    self.signal = hcsq_to_dbm(line)
                    break
        self.dbus['/Signal/Rsrp'] = self.signal.get('rsrp')
        self.dbus['/Signal/Sinr'] = self.signal.get('sinr')
        self.dbus['/Signal/Rsrq'] = self.signal.get('rsrq')
        if csq is None:
            csq = self.dbus['/SignalStrength']
        log.info(fmt_signal(self.signal, csq))

    def _log_diag(self):
        """Best-effort diagnostic when a stuck condition is detected."""
        for cmd in ('AT^SYSINFOEX', 'AT+CEER'):
            r = self.modem.at(cmd)
            if r:
                rlog.info('diag: %s -> %s', cmd, ' | '.join(r))

    def _snapshot(self):
        return {
            'at_alive': self.at_alive,
            'sim': self.sim_status,
            'reg': self.reg_status,
            'ncm': self.ncm_connected,
            'ip': self.dbus['/IP'] or '',
            'dial_failures': self.dial_failures,
            'probe_exhausted': self.probe_exhausted,
            'connect_wanted': self._connect_wanted(),
            'roaming_allowed': self._roaming_allowed(),
            'cfun': self.cfun,
        }

    # ---- session watchdog ----------------------------------------------------

    def _reset_backoff(self):
        self.redial_count = 0
        self.next_redial = 0.0
        self.probe_failures = 0

    def _rx_bytes(self):
        v = (self.sysx.read('/sys/class/net/%s/statistics/rx_bytes' % IFACE) or '').strip()
        try:
            return int(v)
        except ValueError:
            return None

    def _probe(self):
        """Verify traffic actually flows. Runs in a worker thread.

        A ping that goes unanswered is NOT proof of a dead session: plenty of
        APNs filter ICMP. Received bytes moving on the interface is proof of
        the opposite, and on this install Tailscale keepalives guarantee they
        move on a live link - so that reading overrides a failed ping.
        """
        ok = False
        for host in self.cfg.probe_hosts:
            if self.sysx.ping(host, IFACE):
                ok = True
                break
        rx = self._rx_bytes()
        if not ok and rx is not None and self.last_rx is not None and rx > self.last_rx:
            log.info('connectivity probe: no ping answer but %d bytes came in, '
                     'the link is alive', rx - self.last_rx)
            ok = True
        self.last_rx = rx
        self.probe_runs += 1
        if ok:
            if self.probe_failures:
                log.info('connectivity probe recovered')
            self.probe_failures = 0
            self.probe_redials = 0
            self.probe_exhausted = False
        else:
            self.probe_failures += 1
            log.warning('connectivity probe failed (%d/%d)',
                        self.probe_failures, PROBE_FAILURES_MAX)

    def _maybe_probe(self, now):
        if not self.cfg.probe_hosts or now < self.probe_suspended_until:
            return
        if self.probe_thread is not None and self.probe_thread.is_alive():
            return
        if now - self.last_probe < PROBE_INTERVAL:
            return
        self.last_probe = now
        self.probe_thread = threading.Thread(target=self._probe, daemon=True)
        self.probe_thread.start()

    def _maybe_redial(self, reason, now):
        if self.dial_deadline is not None or now < self.next_redial:
            return
        self.redial_count += 1
        backoff = min(REDIAL_MIN_BACKOFF * (2 ** (self.redial_count - 1)),
                      REDIAL_MAX_BACKOFF)
        self.next_redial = now + backoff
        log.warning('watchdog: %s - re-dialling (attempt %d, next retry in %ds)',
                    reason, self.redial_count, backoff)
        self._dial_start(now)

    def _track_session(self, now):
        """Session up/down bookkeeping, independent of any watchdog."""
        if self.ncm_connected:
            if self.session_up_at is None:
                self.session_up_at = now
                if self.dial_deadline is None:
                    rlog.info('session: up (not dialled by us), ensuring DHCP')
                    self._ensure_dhcp()
        elif self.session_up_at is not None:
            rlog.warning('session: down after %s', fmt_duration(now - self.session_up_at))
            self.session_up_at = None
            self.no_ip_since = None

    def _session_watchdog(self, now):
        """Keep the data session alive while the modem is registered. The
        recovery ladder handles every other state."""
        if self.sim_status != SIM_READY:
            return

        if not self._connect_wanted():
            if self.ncm_connected:
                log.info('watchdog: /Settings/Modem/Connect is off, hanging up')
                self._hangup()
            return

        reg = self.reg_status
        if reg not in REGISTERED:
            # Not attached to a network: dialling would fail anyway. The
            # recovery ladder is in charge of getting the modem back.
            self.probe_failures = 0
            return

        if reg == REG_ROAMING and not self._roaming_allowed():
            self._nag('roaming', logging.INFO,
                      'watchdog: roaming network and roaming not permitted, staying offline', now)
            return

        if not self.ncm_connected:
            self._maybe_redial('NCM data session down', now)
            return

        # Session up. The re-dial backoff is only forgiven once the session
        # has held for a while: a session that dies seconds after each dial
        # must not be re-dialled every 30 s forever.
        if self.redial_count and self.session_up_at is not None \
                and now - self.session_up_at >= SESSION_STABLE:
            rlog.info('session: stable for %s, re-dial backoff reset',
                      fmt_duration(now - self.session_up_at))
            self._reset_backoff()

        if not self.dbus['/IP']:
            if self.no_ip_since is None:
                self.no_ip_since = now
            if now - self.no_ip_since >= DHCP_STALE \
                    and now - self.last_dhcp_restart >= DHCP_RESTART_MIN_GAP:
                rlog.warning('session: no address for %s, restarting DHCP',
                             fmt_duration(now - self.no_ip_since))
                self.last_dhcp_restart = now
                self._restart_dhcp()
            else:
                self._ensure_dhcp(now)
            return
        self.no_ip_since = None

        # Session up with an address: make sure packets really come back.
        self._maybe_probe(now)
        if self.probe_failures < PROBE_FAILURES_MAX:
            return

        if self.probe_redials >= PROBE_REDIALS_MAX:
            # Re-dialling does not help: hand over to the recovery ladder, but
            # only once a probe has actually completed on the session the last
            # re-dial created - otherwise the verdict is about the old one.
            if self.probe_runs > self.probe_runs_at_redial:
                self.probe_exhausted = True
            return

        before = self.next_redial
        self._maybe_redial('no connectivity after %d probes' % self.probe_failures, now)
        if self.next_redial != before:
            self.probe_redials += 1
            self.probe_runs_at_redial = self.probe_runs
            # Measure the new session, not the old one.
            self.last_probe = now - PROBE_INTERVAL + 30

    # ---- recovery actions (called by Recovery) -------------------------------

    def _heavy_at(self, cmd):
        """Send a slow or disruptive command, preferably on the wdm channel.
        Returns 'sent', 'refused' or 'impossible'.

        This is the single choke point for every command the recovery ladder
        issues, and it refuses outright anything that could leave the modem in
        a state it cannot be commanded out of.
        """
        if cmd.startswith(FORBIDDEN_PREFIXES):
            log.critical('refusing to send %s: this command can strand the '
                         'modem (see FORBIDDEN_PREFIXES)', cmd)
            return 'impossible'
        if self.wdm.available():
            status, text = self.wdm.send(cmd, timeout=3)
            if status in ('ok', 'timeout'):
                log.info('%s via wdm -> %s', cmd, status)
                return 'sent'
            if status == 'error':
                log.warning('%s via wdm -> %s', cmd, text.strip().replace('\r\n', ' '))
                return 'refused'
            # nodev: fall back to the serial port
        r = self.modem.at(cmd, timeout=3)
        err = self.modem.last_error
        if r is not None:
            log.info('%s via serial -> ok', cmd)
            return 'sent'
        if err == 'timeout':
            log.info('%s via serial -> sent (no answer yet)', cmd)
            self.resync_needed = True
            return 'sent'
        if err in ('io', 'nodev'):
            return 'impossible'
        return 'refused'

    def _repair_usb_intent(self):
        """Undo a USB action that was interrupted half-way.

        The helper disables the modem's port, then re-enables it five seconds
        later. If it is killed in between (OOM, a reboot of the service, a
        stray kill), the port stays down and nothing on this machine would
        ever bring it back. The helper writes its intent before acting; this
        runs at every start-up, and the linkwatch runs it every minute.
        """
        text = self.sysx.read(INTENT_FILE)
        if not text:
            return
        try:
            intent = json.loads(text)
        except ValueError:
            self.sysx.remove(INTENT_FILE)
            return
        if intent.get('boot_id') != self.sysx.boot_id():
            self.sysx.remove(INTENT_FILE)
            return
        target = intent.get('target')
        undo_file = intent.get('undo_file')
        undo_value = intent.get('undo_value')
        if not target or not undo_file:
            self.sysx.remove(INTENT_FILE)
            return
        log.critical('a USB action was left unfinished (%s), undoing it: %s/%s=%s',
                     intent.get('action'), target, undo_file, undo_value)
        self.sysx.write(posixpath.join(target, undo_file),
                        '%s\n' % undo_value)
        peer = intent.get('peer')
        if peer:
            self.sysx.write(posixpath.join(peer, undo_file),
                            '%s\n' % undo_value)
        self.sysx.remove(INTENT_FILE)

    def _ensure_linkwatch(self):
        """Start the last-resort link watchdog, detached.

        Every rung of the ladder needs the modem's tty or /dev/cdc-wdm0. Both
        disappear when the modem leaves the USB bus, and serial-starter then
        kills this service - so nothing would be left to reset the USB port,
        which is the one action that brings such a modem back. The helper runs
        in its own session and survives that, and it re-attaches to the same
        pidfile, so restarting this service never starts a second one.
        """
        if not self.cfg.linkwatch:
            return
        try:
            pid = int((self.sysx.read(LINKWATCH_PIDFILE) or '').strip())
            if self.sysx.exists('/proc/%d' % pid):
                log.info('linkwatch already running (pid %d)', pid)
                return
        except ValueError:
            pass
        if not self.sysx.exists(LINKWATCH_HELPER):
            log.warning('%s missing, no last-resort link watchdog', LINKWATCH_HELPER)
            return
        if self.sysx.spawn_detached([LINKWATCH_HELPER]):
            log.info('linkwatch started')

    def _usb_sysfs_path(self):
        """sysfs directory of the modem's USB device (the one with idVendor)."""
        candidates = [
            '/sys/class/tty/%s/device' % posixpath.basename(self.modem.dev),
            '/sys/class/net/%s/device' % IFACE,
        ]
        for c in candidates:
            if not self.sysx.exists(c):
                continue
            d = self.sysx.realpath(c)
            for _ in range(6):
                if self.sysx.exists(posixpath.join(d, 'idVendor')):
                    if (self.sysx.read(posixpath.join(d, 'idVendor')) or '').strip() == '12d1':
                        return d
                    break
                d = posixpath.dirname(d)
        base = '/sys/bus/usb/devices'
        for name in self.sysx.listdir(base):
            d = posixpath.join(base, name)
            if (self.sysx.read(posixpath.join(d, 'idVendor')) or '').strip() == '12d1' \
                    and (self.sysx.read(posixpath.join(d, 'idProduct')) or '').strip() == '1506':
                return d
        return None

    def _devnum(self):
        """USB device number of the modem, which changes on every real
        re-enumeration. This is how we tell a reset that happened from a
        command that merely answered OK."""
        path = self._usb_sysfs_path()
        if not path:
            return None
        v = (self.sysx.read(posixpath.join(path, 'devnum')) or '').strip()
        return v or None

    def _usb_controller(self, path):
        """Platform name of the USB controller carrying the modem, e.g.
        'xhci-hcd.1'. Never guess: rebinding the wrong controller would take
        out the VE.Direct adapters and the GPS."""
        if not path:
            return None
        real = self.sysx.realpath(path)
        for part in real.split('/'):
            if part.startswith('xhci-hcd.') or part.startswith('dwc'):
                return part
        return None

    def _hci_collateral(self, path, hci):
        """Every other USB device on the modem's controller.

        Rebinding the controller re-enumerates all of them, so the rung must
        not run blind. On the reference install the second device on the
        modem's controller is the VE.Direct cable of the BMV-712 - the active
        battery service, with DVCC on - which is exactly what an energy system
        cannot lose to a modem recovery.
        """
        modem_real = self.sysx.realpath(path) if path else ''
        base = '/sys/bus/usb/devices'
        marker = '/%s/' % hci
        others = []
        for name in self.sysx.listdir(base):
            if name.startswith('usb') or ':' in name:
                continue                        # root hubs and interfaces
            d = posixpath.join(base, name)
            real = self.sysx.realpath(d)
            if real == modem_real or marker not in real + '/':
                continue
            product = (self.sysx.read(posixpath.join(d, 'product')) or '').strip()
            others.append('%s (%s)' % (name, product or 'unknown device'))
        return sorted(others)

    def _query_cfun(self):
        """Current AT+CFUN? value, or None when unknown."""
        r = self.modem.at('AT+CFUN?')
        if not r:
            status, text = self.wdm.send('AT+CFUN?', timeout=3)
            if status != 'ok':
                return None
            r = text.splitlines()
        for line in r:
            if '+CFUN:' in line:
                try:
                    return int(line.split(':', 1)[1].strip().split(',')[0])
                except ValueError:
                    return None
        return None

    def prepare_rung(self, rung, now):
        """Called just before a destructive rung is persisted and executed.
        Records what the next instance of this service will need to judge the
        rung, since the rung itself usually kills us."""
        self.reset_devnum = self._devnum()

    def run_rung_step(self, rung, step, now):
        if rung == RUNG_RADIO_ON:
            # Only ever switches the radio ON, and only when it is off. The
            # package has no command that switches it off.
            cfun = self._query_cfun()
            if cfun == 1:
                rlog.info('recovery: radio already on (CFUN 1), nothing to do')
                return 'ineffective'
            if cfun is None:
                return 'impossible'
            rlog.warning('recovery: radio is off (CFUN %s), switching it on', cfun)
            return self._heavy_at('AT+CFUN=1')

        if rung == RUNG_COPS:
            return self._heavy_at('AT+COPS=0')

        if rung == RUNG_RESET:
            if now - self.last_reset_at < RESET_MIN_GAP * self.cfg.timescale:
                rlog.warning('recovery: modem reset refused, last one was %s ago',
                             fmt_duration(now - self.last_reset_at))
                return 'refused'
            self.pin_attempted = False
            self._cancel_dial()
            self.last_reset_at = now
            # AT^RESET is the only command measured to actually restart this
            # firmware: AT+CFUN=1,1 answers OK without ever leaving the bus.
            return self._heavy_at('AT^RESET')

        if rung == RUNG_USB:
            path = self._usb_sysfs_path()
            if not path:
                rlog.error('recovery: modem USB device not found in sysfs')
                return 'impossible'
            self.pin_attempted = False
            self._cancel_dial()
            self._stop_dhcp()
            self.modem.close()
            return 'sent' if self.sysx.spawn_detached(
                [USB_RESET_HELPER, 'portcycle', path]) else 'impossible'

        if rung == RUNG_HCI:
            path = self._usb_sysfs_path()
            hci = self._usb_controller(path)
            if not hci:
                rlog.error('recovery: cannot work out the modem USB controller')
                return 'impossible'
            others = self._hci_collateral(path, hci)
            if others and not self.cfg.hci_shared_ok:
                rlog.error('recovery: controller rebind refused: %s also on %s '
                           'and would be re-enumerated with the modem (set '
                           'HCI_REBIND_SHARED=1 in %s to accept that)',
                           ', '.join(others), hci, CONFIG_FILE)
                return 'impossible'
            self.pin_attempted = False
            self._cancel_dial()
            self._stop_dhcp()
            self.modem.close()
            return 'sent' if self.sysx.spawn_detached(
                [USB_RESET_HELPER, 'rebind', path, hci]) else 'impossible'

        return 'impossible'

    def rung_was_effective(self, rung):
        """Called at the end of a destructive rung's settle time: did the modem
        really re-enumerate? AT^RESET and the USB actions must change devnum;
        a command that merely answered OK did nothing, and the ladder must move
        on instead of waiting out the rest of the settle.
        """
        if rung not in (RUNG_RESET, RUNG_USB, RUNG_HCI):
            return True
        if self.reset_devnum is None:
            return True
        now_devnum = self._devnum()
        if now_devnum is None:
            # The device is not on the bus (yet): that is a re-enumeration in
            # progress, not an ineffective rung.
            return True
        if now_devnum == self.reset_devnum:
            rlog.error('recovery: %s left devnum at %s - the modem never left '
                       'the bus, treating the rung as ineffective',
                       rung, now_devnum)
            return False
        rlog.info('recovery: %s re-enumerated the modem (devnum %s -> %s)',
                  rung, self.reset_devnum, now_devnum)
        return True

    def on_stuck(self, reason, snap):
        if reason != 'at_mute':
            self._log_diag()

    def on_recovered(self, reason):
        self._reset_backoff()
        self.dial_failures = 0
        if reason == 'traffic':
            self.probe_redials = 0
            self.probe_exhausted = False

    def verify_dial(self, now):
        if self.reg_status in REGISTERED and self._connect_wanted():
            rlog.info('recovery: [dial] verification dial')
            return self._dial_start(now)
        return False

    def traffic_ok(self):
        """Synchronous probe, used once at the end of the traffic ladder."""
        if not self.cfg.probe_hosts:
            return True
        ok = False
        for host in self.cfg.probe_hosts:
            if self.sysx.ping(host, IFACE):
                ok = True
                break
        rx = self._rx_bytes()
        if not ok and rx is not None and self.last_rx is not None and rx > self.last_rx:
            ok = True
        self.last_rx = rx
        if ok:
            self.probe_failures = 0
            self.probe_redials = 0
            self.probe_exhausted = False
            return True
        return False

    def on_traffic_exhausted(self, now):
        self.probe_suspended_until = now + PROBE_SUSPEND * self.cfg.timescale
        self.probe_failures = 0
        self.probe_redials = 0
        self.probe_exhausted = False
        log.error('connectivity probe to %s never recovers, suspended for %s. If your '
                  'operator filters ICMP, set PROBE_HOST= (empty) in %s',
                  ','.join(self.cfg.probe_hosts),
                  fmt_duration(PROBE_SUSPEND * self.cfg.timescale), CONFIG_FILE)

    # ---- periodic update -----------------------------------------------------

    def _step(self, name, fn):
        try:
            fn()
        except Exception:
            log.exception('update step %s failed', name)

    def _publish_recovery(self, now):
        for path, value in self.recovery.describe(now).items():
            if self.dbus[path] != value:
                self.dbus[path] = value

    def _update(self):
        """Periodic update called by GLib. Every step is isolated: one failure
        never stalls the others."""
        now = time.monotonic()
        if not self.identified:
            self._step('identify', self._identify)
        self._step('status', self._update_status)
        self._step('dial', lambda: self._poll_dial(now))
        self._step('session', lambda: self._track_session(now))
        self._step('recovery', lambda: (self.recovery.observe(self._snapshot(), now),
                                        self.recovery.tick(now)))
        if self.recovery.allows_session_watchdog(now):
            self._step('watchdog', lambda: self._session_watchdog(now))
        self._step('publish', lambda: self._publish_recovery(now))
        return True


def sigterm(s, f):
    global mainloop, service
    log.info('Signal received, stopping')
    if service is not None:
        service.stop()
    mainloop.quit()


def setup_recovery_log():
    """Persistent journal of the recovery ladder, in addition to the multilog
    (which changes directory whenever the tty gets a new number)."""
    try:
        os.makedirs(RECOVERY_LOG_DIR, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(RECOVERY_LOG, maxBytes=262144, backupCount=2)
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)-8s %(message)s'))
        rlog.addHandler(handler)
    except Exception as e:
        log.warning('no persistent recovery log: %s', e)


service = None


def main():
    global mainloop, service

    if len(sys.argv) < 3 or sys.argv[1] != '-s':
        print('Usage: dbus-modem-e3372.py -s /dev/ttyUSBx')
        sys.exit(1)

    dev = sys.argv[2]
    logging.basicConfig(format='%(levelname)-8s %(message)s', level=logging.INFO)
    setup_recovery_log()
    cfg = Config()
    log.info('Starting dbus-modem-e3372 %s on %s (probe %s, recovery level %d, timescale %g)',
             VERSION, dev, ','.join(cfg.probe_hosts) or 'off', cfg.recovery_level, cfg.timescale)

    signal.signal(signal.SIGINT, sigterm)
    signal.signal(signal.SIGTERM, sigterm)

    mainloop = GLib.MainLoop()

    service = ModemService(dev, cfg)
    if not service.start():
        sys.exit(1)

    log.info('Modem service running')
    mainloop.run()

    service.modem.close()
    log.info('Stopped')


if __name__ == '__main__':
    main()
