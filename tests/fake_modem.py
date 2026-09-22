"""Scripted Huawei E3372h. Answers every command the service uses, records
everything it receives (per channel) and lets a test override any command."""

NO_ANSWER = object()   # a handler returns this to simulate a mute modem


class FakeModem:
    def __init__(self, clock):
        self.clock = clock
        self.responsive = True          # AT port answers
        self.wdm_responsive = True      # cdc-wdm0 answers
        self.wdm_available = True
        self.port_open_ok = True        # serial.Serial() succeeds
        self.died = False               # device gone (after a reset) until revived
        self.cpin = 'READY'             # text, or ('CME', code)
        self.pin = None                 # the correct PIN when the SIM is locked
        self.creg = 1
        self.operator = 'Bouygues Telecom'
        self.act = 7
        self.ndis = 0
        self.csq = 15
        self.hcsq = '"LTE",39,27,86,18'
        self.cfun = 1
        self.cscs = 'GSM'           # reset to the factory 'IRA' by a real reset
        self.devnum = 5             # changes on every real re-enumeration
        self.cops2_refused = True   # the real firmware answers +CME ERROR: 50
        # Measured in situ: with the radio off this firmware answers reads but
        # refuses EVERY configuration write with +CME ERROR: 100 - including
        # the command that would undo it. AT^RESET is the one way out.
        self.write_locked = False
        self.reset_refused = False  # make AT^RESET fail, to test the fallback
        self.csq_empty = False      # '+CSQ: ' with no value, as seen at boot
        self.model = 'E3372'
        self.imei = '865035037218880'
        self.dial_delay = 3             # NDISDUP=1,1 brings the session up after this
        self.ndisdup_auto_up = True
        self.revive_delay = 20          # a reset modem comes back after this
        self.creg_after_reset = None    # None = unchanged
        self.creg_after_cfun = None     # registration after a CFUN=0/1 cycle
        self.responsive_after_reset = True
        self.reset_count = 0        # every re-enumeration, whatever its cause
        self.firmware_resets = 0    # only the ones that restarted the firmware
        self.sent = []                  # (t, channel, cmd)
        self.handlers = {}
        self.serial = None
        self._creg_before_cfun = self.creg

    # ---- scripting ----

    def radio_off(self):
        return self.cfun in (0, 4, 7)

    def locked(self):
        """True when the firmware refuses configuration writes. Measured in
        situ: switching the radio off sets this, and nothing but a real
        firmware restart clears it. A test can set the two independently to
        model a more forgiving firmware."""
        return self.write_locked

    def power_cycle(self):
        """What only a real power cut does: clears the firmware state. Not
        reachable from any host-side USB action on this board."""
        self.cfun = 1
        self.write_locked = False
        self.cscs = 'IRA'
        self.reset(clears_state=True)

    def on(self, prefix, fn):
        """fn(cmd) -> list of response lines, NO_ANSWER, or None (default)."""
        self.handlers[prefix] = fn

    def schedule(self, delay, **state):
        def apply():
            for k, v in state.items():
                setattr(self, k, v)
        self.clock.at(delay, apply)

    def reset(self, clears_state=True):
        """The device leaves the bus and comes back with a new devnum.

        clears_state=True models AT^RESET, measured in situ to restart the
        firmware (CFUN back to 1, CSCS back to the factory 'IRA').
        clears_state=False models a host-side bus reset (port disable or
        authorize), measured NOT to change anything inside the modem.
        """
        self.reset_count += 1
        if clears_state:
            self.firmware_resets += 1
        self.died = True
        self.ndis = 0
        self.devnum += 1
        if clears_state:
            self.cfun = 1
            self.write_locked = False
            self.cscs = 'IRA'
        if self.pin:
            self.cpin = 'SIM PIN'
        creg_after = self.creg if self.creg_after_reset is None else self.creg_after_reset

        def back():
            self.died = False
            if clears_state or not self.radio_off():
                self.creg = creg_after
            self.port_open_ok = True
            if self.responsive_after_reset:
                self.responsive = True
                self.wdm_responsive = True
        self.clock.at(self.revive_delay, back)

    def bus_reset(self):
        """A host-side re-enumeration: new devnum, same firmware state."""
        self.reset(clears_state=False)

    def sent_cmds(self, prefix=None, channel=None):
        out = []
        for t, ch, cmd in self.sent:
            if prefix and not cmd.startswith(prefix):
                continue
            if channel and ch != channel:
                continue
            out.append((t, ch, cmd))
        return out

    # ---- command handling ----

    def handle(self, cmd, channel):
        self.sent.append((self.clock.monotonic(), channel, cmd))
        if self.died:
            return None
        for prefix, fn in self.handlers.items():
            if cmd.startswith(prefix):
                r = fn(cmd)
                if r is NO_ANSWER:
                    return None
                if r is not None:
                    return r
        if channel == 'serial' and not self.responsive:
            return None
        if channel == 'wdm' and not self.wdm_responsive:
            return None
        return self._default(cmd)

    def _default(self, cmd):
        if cmd == 'AT':
            return ['OK']
        if cmd.startswith(('AT+CMEE', 'AT^CURC', 'AT+CGDCONT=')):
            return ['OK']
        if cmd == 'AT+CGMM':
            return [self.model, 'OK']
        if cmd == 'AT+CGSN':
            return [self.imei, 'OK']
        if cmd == 'AT+CPIN?':
            if self.cfun == 0:      # CFUN=0 powers the SIM down, CFUN=4 does not
                return ['+CME ERROR: 13']
            if isinstance(self.cpin, tuple):
                return ['+CME ERROR: %d' % self.cpin[1]]
            return ['+CPIN: %s' % self.cpin, 'OK']
        if cmd.startswith('AT+CPIN='):
            pin = cmd.split('=', 1)[1].strip('"')
            if self.cpin != 'SIM PIN':
                return ['+CME ERROR: 3']
            if pin == self.pin:
                self.cpin = 'READY'
                return ['OK']
            return ['+CME ERROR: 16']
        if cmd == 'AT+CSQ':
            if self.csq_empty:
                # Seen at boot on the real modem; the v1.2 service died on
                # int('') here and was respawned in a loop by serial-starter.
                return ['+CSQ: ,', 'OK']
            return ['+CSQ: %d,99' % self.csq, 'OK']
        if cmd == 'AT+CSCS?':
            return ['+CSCS: "%s"' % self.cscs, 'OK']
        if cmd.startswith('AT+CSCS='):
            if self.locked():
                return ['+CME ERROR: 100']
            self.cscs = cmd.split('=', 1)[1].strip('"')
            return ['OK']
        if cmd == 'AT^RESET':
            if self.reset_refused:
                return ['+CME ERROR: 100']
            self.reset(clears_state=True)
            return ['OK']
        if cmd == 'AT+CREG?':
            return ['+CREG: 0,%d' % (0 if self.radio_off() else self.creg), 'OK']
        if cmd == 'AT+COPS?':
            if self.creg in (1, 5) and not self.radio_off():
                return ['+COPS: 0,0,"%s",%d' % (self.operator, self.act), 'OK']
            return ['+COPS: 0', 'OK']
        if cmd.startswith('AT+COPS='):
            # No network selection is possible with the radio off, and this
            # firmware refuses a plain deregistration outright.
            if self.locked():
                return ['+CME ERROR: 100']
            if cmd.startswith('AT+COPS=2') and self.cops2_refused:
                return ['+CME ERROR: 50']
            return ['OK']
        if cmd == 'AT^NDISSTATQRY?':
            return ['^NDISSTATQRY:%d,,,"IPV4"' % self.ndis, 'OK']
        if cmd.startswith('AT^NDISDUP=1,0'):
            self.ndis = 0
            return ['OK']
        if cmd.startswith('AT^NDISDUP=1,1'):
            if self.ndisdup_auto_up and self.creg in (1, 5) and self.cfun == 1:
                self.clock.at(self.dial_delay, lambda: setattr(self, 'ndis', 1))
            return ['OK']
        if cmd == 'AT^HCSQ?':
            return ['^HCSQ:%s' % self.hcsq, 'OK']
        if cmd == 'AT+CFUN?':
            return ['+CFUN: %d' % self.cfun, 'OK']
        if cmd.startswith('AT+CFUN=1,1'):
            # MEASURED (armC.log): this firmware answers OK and the device
            # never leaves the bus. It restarts nothing.
            if self.locked():
                return ['+CME ERROR: 100']
            self.ndis = 0
            return ['OK']
        if cmd.startswith('AT+CFUN='):
            new = int(cmd.split('=')[1].split(',')[0])
            if new not in (0, 1, 4, 5, 6, 7, 8, 10, 11):
                return ['+CME ERROR: 50']
            if self.locked():
                return ['+CME ERROR: 100']
            was_off = self.radio_off()
            now_off = new in (0, 4, 7)
            if now_off and not was_off:
                self._creg_before_cfun = self.creg
                self.ndis = 0
                self.write_locked = True
            if was_off and not now_off:
                if self.pin:
                    self.cpin = 'SIM PIN'
                if self.creg_after_cfun is not None:
                    creg = self.creg_after_cfun
                elif self._creg_before_cfun is not None:
                    creg = self._creg_before_cfun
                else:
                    creg = 1
                self.schedule(5, creg=creg)
            self.cfun = new
            return ['OK']
        if cmd == 'AT^SYSINFOEX':
            return ['^SYSINFOEX:2,3,0,1,,6,"LTE",101,"LTE"', 'OK']
        if cmd == 'AT+CEER':
            return ['+CEER: 0', 'OK']
        return ['ERROR']


class FakeWdm:
    """Stand-in for WdmChannel, wired to the same FakeModem."""

    def __init__(self, modem):
        self.modem = modem

    def available(self):
        return self.modem.wdm_available and not self.modem.died

    def send(self, cmd, timeout=3.0):
        r = self.modem.handle(cmd, 'wdm')
        if r is None:
            self.modem.clock.advance(timeout)   # the real channel blocks that long
            return 'timeout', ''
        text = '\r\n'.join(r)
        if 'ERROR' in text:
            return 'error', text
        return 'ok', text
