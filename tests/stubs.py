"""Fake Venus OS / pyserial / GLib modules, inserted into sys.modules before
the service is imported. No hardware, no D-Bus, no GLib needed."""

import sys
import types

# Bench-wide registry: the modem that new Serial() instances talk to, and the
# initial values of the settings.
CURRENT = {'modem': None}
SETTINGS_PRESET = {}


class FakeSerialException(Exception):
    pass


class FakeSerial:
    def __init__(self, dev, baud, timeout=3):
        self.modem = CURRENT['modem']
        if self.modem is None or self.modem.died or not self.modem.port_open_ok:
            raise FakeSerialException('cannot open %s' % dev)
        self.dev = dev
        self.timeout = timeout
        self._rx = []
        self.is_open = True
        self.modem.serial = self

    def reset_input_buffer(self):
        self._rx = []

    def write(self, data):
        if self.modem.died:
            raise FakeSerialException('write: device gone')
        cmd = data.decode().strip('\r\n')
        resp = self.modem.handle(cmd, 'serial')
        if resp is not None:
            self._rx.extend((line + '\r\n').encode() for line in resp)

    @property
    def in_waiting(self):
        return sum(len(x) for x in self._rx)

    def readline(self):
        if self.modem.died:
            raise FakeSerialException('read: device gone')
        return self._rx.pop(0) if self._rx else b''

    def close(self):
        self.is_open = False


class FakeGLib:
    callbacks = []

    @staticmethod
    def timeout_add(ms, fn):
        FakeGLib.callbacks.append((ms, fn))
        return 1

    class MainLoop:
        def run(self):
            pass

        def quit(self):
            pass


class FakeVeDbusService:
    register_failures = 0  # how many times register() raises (class-level knob)

    def __init__(self, name, register=True):
        self.name = name
        self.paths = {}
        self.dbusconn = object()
        self.registered = False

    def add_path(self, path, value, **kw):
        self.paths[path] = value

    def register(self):
        if FakeVeDbusService.register_failures > 0:
            FakeVeDbusService.register_failures -= 1
            raise Exception('name already taken')
        self.registered = True

    def __getitem__(self, path):
        return self.paths[path]

    def __setitem__(self, path, value):
        self.paths[path] = value


class FakeSettingsDevice:
    def __init__(self, conn, settings, callback, timeout=0):
        self.values = {k: v[1] for k, v in settings.items()}
        self.values.update(SETTINGS_PRESET)
        self.callback = callback
        self.writes = []

    def __getitem__(self, key):
        return self.values[key]

    def __setitem__(self, key, value):
        self.writes.append((key, value))
        self.values[key] = value

    def change(self, key, value):
        """A change made from the GUI."""
        old = self.values[key]
        self.values[key] = value
        self.callback(key, old, value)


def install():
    if 'serial' in sys.modules and getattr(sys.modules['serial'], 'FAKE', False):
        return
    serial = types.ModuleType('serial')
    serial.FAKE = True
    serial.SerialException = FakeSerialException
    serial.Serial = FakeSerial
    sys.modules['serial'] = serial

    gi = types.ModuleType('gi')
    repo = types.ModuleType('gi.repository')
    repo.GLib = FakeGLib
    gi.repository = repo
    sys.modules['gi'] = gi
    sys.modules['gi.repository'] = repo

    dbus = types.ModuleType('dbus')
    mainloop = types.ModuleType('dbus.mainloop')
    glib = types.ModuleType('dbus.mainloop.glib')
    glib.threads_init = lambda: None
    glib.DBusGMainLoop = lambda set_as_default=False: None
    mainloop.glib = glib
    dbus.mainloop = mainloop
    sys.modules['dbus'] = dbus
    sys.modules['dbus.mainloop'] = mainloop
    sys.modules['dbus.mainloop.glib'] = glib

    vedbus = types.ModuleType('vedbus')
    vedbus.VeDbusService = FakeVeDbusService
    sys.modules['vedbus'] = vedbus

    settingsdevice = types.ModuleType('settingsdevice')
    settingsdevice.SettingsDevice = FakeSettingsDevice
    sys.modules['settingsdevice'] = settingsdevice
