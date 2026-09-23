"""Fake SystemShim: an in-memory file system, a scripted ping, a simulated
udhcpc, and a USB reset helper that resets the fake modem."""

import types

PIDFILE = '/var/run/udhcpc.wwan0.pid'
SYSFS_DEV = '/sys/bus/usb/devices/3-1'
SYSFS_PORT = '/sys/bus/usb/devices/usb3/3-0:1.0/usb3-port1'
SYSFS_PEER = '/sys/bus/usb/devices/usb4/4-0:1.0/usb4-port1'
HCI = 'xhci-hcd.1'


class FakeSystem:
    def __init__(self, clock, modem, tty='/dev/ttyUSB1'):
        self.clock = clock
        self.modem = modem
        self.tty = tty
        self.files = {
            SYSFS_DEV + '/idVendor': '12d1\n',
            SYSFS_DEV + '/idProduct': '1506\n',
            SYSFS_DEV + '/authorized': '1\n',
            SYSFS_PORT + '/disable': '0\n',
            SYSFS_PORT + '/state': 'configured\n',
            SYSFS_PEER + '/disable': '0\n',
            '/sys/bus/platform/drivers/xhci-hcd/' + HCI: '',
            '/sys/bus/platform/drivers/xhci-hcd/xhci-hcd.0': '',
        }
        self.calls = []        # (t, kind, detail)
        self.writes = []       # (t, path, text)
        self.spawned = []
        self.ping_results = True   # bool or callable(host) -> bool
        self.boot = 'boot-1'
        self.ip = ''
        self.udhcpc_pid = None
        self.next_pid = 1000
        self.rx_bytes = 1000
        self.port_write_fails = False
        self.live_pids = set()
        self.realpaths = {}

    def _t(self):
        return self.clock.monotonic()

    def sh(self, cmd):
        self.calls.append((self._t(), 'sh', cmd))
        if cmd.startswith('udhcpc'):
            self.next_pid += 1
            self.udhcpc_pid = self.next_pid
            self.files[PIDFILE] = str(self.udhcpc_pid)
            pid = self.udhcpc_pid

            def lease():
                if self.udhcpc_pid == pid and self.modem.ndis:
                    self.ip = '10.0.0.2'
            self.clock.at(5, lease)
        elif 'addr flush' in cmd:
            self.ip = ''
        return 0

    def run(self, argv, timeout=5):
        self.calls.append((self._t(), 'run', list(argv)))
        if list(argv[:3]) == ['ip', '-4', 'addr']:
            out = '    inet %s/30 scope global wwan0\n' % self.ip if self.ip else ''
            return types.SimpleNamespace(stdout=out, returncode=0)
        return types.SimpleNamespace(stdout='', returncode=0)

    def read(self, path):
        if path == SYSFS_DEV + '/devnum':
            return None if self.modem.died else '%d\n' % self.modem.devnum
        if path.endswith('/statistics/rx_bytes'):
            return '%d\n' % self.rx_bytes
        return self.files.get(path)

    def write(self, path, text):
        """Plain sysfs write (write_atomic cannot touch sysfs)."""
        self.calls.append((self._t(), 'write', (path, text.strip())))
        if self.port_write_fails and path.endswith('/disable'):
            return False
        self.files[path] = text
        if path.endswith('/disable'):
            self.files[SYSFS_PORT + '/state'] = (
                'not attached\n' if text.strip() == '1' else 'configured\n')
        return True

    def write_atomic(self, path, text):
        self.files[path] = text
        self.writes.append((self._t(), path, text))
        return True

    def exists(self, path):
        if path.startswith('/proc/'):
            try:
                return int(path.split('/')[2]) in self.live_pids
            except (IndexError, ValueError):
                return False
        if path == self.tty:
            return not self.modem.died
        if path == '/dev/cdc-wdm0':
            return self.modem.wdm_available and not self.modem.died
        if path.startswith('/sys/class/tty/') or path.startswith('/sys/class/net/'):
            return not self.modem.died
        if path in self.files:
            return True
        prefix = path.rstrip('/') + '/'
        return any(k.startswith(prefix) for k in self.files)

    def listdir(self, path):
        prefix = path.rstrip('/') + '/'
        names = set()
        for k in self.files:
            if k.startswith(prefix):
                names.add(k[len(prefix):].split('/')[0])
        return sorted(names)

    DEVPATH = ('/sys/devices/platform/axi/1000120000.pcie/1f00300000.usb/'
               'xhci-hcd.1/usb3/3-1')

    def add_usb_device(self, name, vid, pid, product, hci='xhci-hcd.1'):
        """Put another device on the bus, under the given controller."""
        base = '/sys/bus/usb/devices/' + name
        self.files[base + '/idVendor'] = vid
        self.files[base + '/idProduct'] = pid
        self.files[base + '/product'] = product
        root = 'usb3' if hci == 'xhci-hcd.1' else 'usb1'
        self.realpaths[base] = ('/sys/devices/platform/axi/1000120000.pcie/x.usb/'
                                '%s/%s/%s' % (hci, root, name))

    def realpath(self, path):
        if path in self.realpaths:
            return self.realpaths[path]
        if path == SYSFS_DEV:
            return self.DEVPATH
        if path.startswith('/sys/class/tty/'):
            return self.DEVPATH + '/3-1:1.0/' + path.split('/')[4]
        if path.startswith('/sys/class/net/'):
            return self.DEVPATH + '/3-1:1.1'
        return path

    def remove(self, path):
        self.files.pop(path, None)
        if path == PIDFILE:
            self.udhcpc_pid = None

    def kill(self, pid, sig=None):
        self.calls.append((self._t(), 'kill', pid))
        if pid == self.udhcpc_pid:
            self.udhcpc_pid = None
        return True

    def pid_cmdline(self, pid):
        if pid == self.udhcpc_pid:
            return b'udhcpc\x00-i\x00wwan0\x00'
        return b''

    def spawn_detached(self, argv):
        self.calls.append((self._t(), 'spawn', list(argv)))
        self.spawned.append(list(argv))
        if argv and str(argv[0]).endswith('e3372_usb_reset.sh'):
            self.remove(PIDFILE)
            # MEASURED: a host-side reset re-enumerates the device but
            # leaves the firmware state (CFUN, the write lock) untouched.
            self.modem.bus_reset()
        return True

    def ping(self, host, iface):
        self.calls.append((self._t(), 'ping', host))
        r = self.ping_results
        return bool(r(host)) if callable(r) else bool(r)

    def boot_id(self):
        return self.boot

    # bench helpers
    def pings(self):
        return [c for c in self.calls if c[1] == 'ping']
