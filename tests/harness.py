"""Bench: loads dbus-modem-e3372.py against the fakes and drives its poll loop
on a virtual clock. Also plays serial-starter: when the modem's tty vanishes
the service instance is dropped, when the modem is back a new one starts."""

import importlib.util
import logging
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import stubs
import fake_clock
import fake_modem
import fake_system

SERVICE = os.path.join(os.path.dirname(HERE), 'dbus-modem-e3372.py')
POLL = 10

LOG = []


class _ListHandler(logging.Handler):
    def emit(self, record):
        LOG.append((record.levelname, record.getMessage()))


_root = logging.getLogger()
_root.setLevel(logging.DEBUG)
if not any(isinstance(h, _ListHandler) for h in _root.handlers):
    _root.addHandler(_ListHandler())


def load_service():
    stubs.install()
    spec = importlib.util.spec_from_file_location('dbus_modem_e3372', SERVICE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Bench:
    def __init__(self, level=4, timescale=1.0, probe_hosts='8.8.8.8,1.1.1.1',
                 settings=None, tty='/dev/ttyUSB1', start_time=1000.0, linkwatch='1'):
        self.clock = fake_clock.FakeClock(start_time)
        self.modem = fake_modem.FakeModem(self.clock)
        stubs.CURRENT['modem'] = self.modem
        stubs.SETTINGS_PRESET.clear()
        stubs.SETTINGS_PRESET.update(settings or {})
        stubs.FakeVeDbusService.register_failures = 0
        self.mod = load_service()
        self.mod.time = self.clock
        self.system = fake_system.FakeSystem(self.clock, self.modem, tty)
        self.tty = tty
        self.cfg = self.mod.Config(raw={
            'APN': 'test.apn', 'PROBE_HOST': probe_hosts,
            'RECOVERY_LEVEL': str(level), 'RECOVERY_TIMESCALE': str(timescale),
            'LINKWATCH': linkwatch})
        self.svc = None
        self.next_poll = None
        self.restarts = 0
        self.poll_durations = []
        self.log_start = len(LOG)

    # ---- lifecycle ----

    def start(self):
        self.svc = self.mod.ModemService(self.tty, self.cfg, self.system,
                                         fake_modem.FakeWdm(self.modem))
        ok = self.svc.start()
        if self.next_poll is None:
            self.next_poll = self.clock.monotonic() + POLL
        return ok

    @property
    def now(self):
        return self.clock.monotonic()

    def poll(self):
        """Advance to the next poll boundary and run one _update()."""
        self.clock.advance(max(0.0, self.next_poll - self.now))
        self.next_poll += POLL
        if self.svc is not None:
            t0 = self.now
            self.svc._update()
            self._join_probe()
            self.poll_durations.append(self.now - t0)
            if self.modem.died:
                # serial-starter kills the service when its tty disappears
                self.svc.stop()
                self.svc = None
                self.restarts += 1
        elif not self.modem.died:
            # ... and starts a new instance once the tty is back
            self.start()
            self._join_probe()

    def run(self, seconds):
        end = self.now + seconds
        while self.next_poll <= end:
            self.poll()

    def _join_probe(self):
        if self.svc is not None and self.svc.probe_thread is not None:
            self.svc.probe_thread.join(timeout=2)

    # ---- inspection ----

    def logs(self, needle=None, level=None):
        out = []
        for lvl, msg in LOG[self.log_start:]:
            if needle is not None and needle not in msg:
                continue
            if level is not None and lvl != level:
                continue
            out.append(msg)
        return out

    def heavy(self):
        """Recovery commands received by the modem: (t, channel, cmd)."""
        return [x for x in self.modem.sent
                if x[2].startswith(('AT+COPS=', 'AT+CFUN=', 'AT^RESET'))
                and x[2] != 'AT+CFUN?']

    def spawned(self, action=None):
        """Detached helper invocations, optionally filtered by action."""
        out = [a for a in self.system.spawned if 'usb_reset' in str(a[0])]
        if action:
            out = [a for a in out if len(a) > 1 and a[1] == action]
        return out

    def heavy_cmds(self):
        return [x[2] for x in self.heavy()]

    def dbus(self, path):
        return self.svc.dbus[path]

    def state_file(self):
        import json
        text = self.system.files.get('/run/e3372-recovery.json')
        return json.loads(text) if text else None
