"""Recovery ladder: escalation, verification, budget and backoff.

The rungs here are the ones measured on the real hardware on 2026-09-22:
AT^RESET restarts the firmware, AT+CFUN=1,1 does not, and no host-side USB
action changes anything inside the modem.
"""

import re
import unittest

import harness

HELPER = '/data/e3372_usb_reset.sh'
SYSFS = '/sys/bus/usb/devices/3-1'


class RegLadderTests(unittest.TestCase):

    def test_denied_escalates_to_the_modem_reset_and_recovers(self):
        b = harness.Bench()
        b.modem.creg = 3                 # registration denied by the network
        b.modem.creg_after_reset = 1     # only a real modem restart clears it
        b.start()
        b.run(30 * 60)

        cmds = b.heavy()
        names = [c[2] for c in cmds]
        # radio is on, so RADIO_ON is skipped without sending anything
        self.assertEqual(names, ['AT+COPS=0', 'AT^RESET'], names)
        self.assertTrue(all(c[1] == 'wdm' for c in cmds), cmds)
        self.assertGreaterEqual(cmds[0][0] - 1000, 120)      # 2 min grace
        self.assertTrue(b.logs('radio already on (CFUN 1), nothing to do'))
        self.assertEqual(b.modem.firmware_resets, 1)
        self.assertEqual(b.restarts, 1)                      # the modem left the bus
        self.assertTrue(b.logs('recovered at rung 3/5 MODEM_RESET'), b.logs('recovery:'))
        self.assertTrue(b.logs('re-enumerated the modem (devnum'))
        self.assertEqual(b.dbus('/RegStatus'), 1)
        self.assertEqual(b.dbus('/Connected'), 1)

    def test_absorbing_state_is_escaped_by_at_reset(self):
        """The 2026-09-19 incident, as measured: radio off, every write
        refused with CME 100, and AT^RESET the only way out."""
        b = harness.Bench()
        b.modem.cfun = 4
        b.modem.write_locked = True
        b.start()
        b.run(30 * 60)

        names = [c[2] for c in b.heavy()]
        self.assertEqual(names[:3], ['AT+CFUN=1', 'AT+COPS=0', 'AT^RESET'], names)
        self.assertTrue(b.logs('radio is off (CFUN 4), switching it on'))
        self.assertTrue(b.logs('recovered at rung 3/5 MODEM_RESET'), b.logs('recovery:'))
        self.assertEqual(b.modem.cfun, 1)
        self.assertFalse(b.modem.write_locked)
        self.assertEqual(b.dbus('/Connected'), 1)
        self.assertEqual(b.system.spawned, [])   # no USB action was needed

    def test_radio_switched_off_externally_is_repaired_without_a_reset(self):
        b = harness.Bench()
        b.modem.cfun = 4
        b.modem.write_locked = False      # a modem that still accepts writes
        b.start()
        b.run(20 * 60)
        self.assertEqual([c[2] for c in b.heavy()], ['AT+CFUN=1'])
        self.assertTrue(b.logs('recovered at rung 1/5 RADIO_ON'))
        self.assertEqual(b.modem.firmware_resets, 0)
        self.assertEqual(b.dbus('/Connected'), 1)

    def test_recovery_mid_ladder_stops_escalation(self):
        b = harness.Bench()
        b.modem.creg = 3
        b.modem.on('AT+COPS=0', lambda c: (b.modem.schedule(30, creg=1), ['OK'])[1])
        b.start()
        b.run(20 * 60)
        self.assertEqual([c[2] for c in b.heavy()], ['AT+COPS=0'])
        self.assertTrue(b.logs('recovered at rung 2/5 COPS_AUTO'))
        self.assertEqual(b.modem.firmware_resets, 0)
        self.assertEqual(b.svc.recovery.ladder_count, 0)

    def test_exhausted_ladder_backs_off_and_never_gives_up(self):
        b = harness.Bench()
        b.modem.creg = 3
        b.modem.creg_after_reset = 3      # nothing helps
        b.start()
        b.run(13 * 3600)

        self.assertTrue(b.spawned('portcycle'), b.system.spawned)
        self.assertTrue(b.spawned('rebind'), b.system.spawned)
        waits = [re.search(r'next ladder in (\S+)', m).group(1)
                 for m in b.logs('exhausted after')]
        self.assertEqual(waits[:5], ['15m00s', '30m00s', '1h00m', '2h00m', '4h00m'])
        self.assertGreaterEqual(b.svc.recovery.ladder_count, 5)
        self.assertEqual(b.state_file()['ladder_count'], b.svc.recovery.ladder_count)

    def test_reason_change_and_clear_are_logged(self):
        b = harness.Bench()
        b.modem.creg = 3
        b.start()
        b.run(60)
        self.assertTrue(b.logs('recovery: [reg] stuck (CREG 3 denied)'))
        self.assertTrue(b.logs('diag: AT^SYSINFOEX'))
        b.modem.creg = 1
        b.run(30)
        self.assertTrue(b.logs('recovery: [reg] cleared after'))
        self.assertEqual(b.heavy(), [])
        self.assertTrue(b.logs('reg: 3 (denied) -> 1 (home)'))

    def test_connect_off_cancels_the_ladder(self):
        b = harness.Bench()
        b.modem.creg = 3
        b.start()
        b.run(140)
        self.assertEqual([c[2] for c in b.heavy()], ['AT+COPS=0'])
        b.svc.settings.change('connect', 0)
        self.assertEqual(b.svc.recovery.state, 'idle')
        n = len(b.heavy())
        b.run(30 * 60)
        self.assertEqual(len(b.heavy()), n)
        self.assertTrue(b.logs('cancelled'))


class SafetyTests(unittest.TestCase):
    """Nothing this package sends may be able to strand the modem."""

    def test_no_radio_off_command_is_ever_sent(self):
        """The cardinal test: every reason, every level, 13 hours."""
        banned = ('AT+CFUN=0', 'AT+CFUN=4', 'AT+CFUN=6', 'AT+CFUN=7',
                  'AT+COPS=2', 'AT+CGATT=0', 'AT^RADIOOFF', 'AT^SYSCFG')
        cases = [
            ('denied', dict(creg=3, creg_after_reset=3)),
            ('absorbing', dict(cfun=4, write_locked=True, creg_after_reset=0)),
            ('mute', dict(responsive=False, wdm_responsive=False)),
            ('nosim', dict(cpin=('CME', 10))),
            ('nodial', dict(ndisdup_auto_up=False)),
        ]
        for level in (0, 1, 2, 3, 4):
            for name, state in cases:
                b = harness.Bench(level=level)
                for k, v in state.items():
                    setattr(b.modem, k, v)
                b.start()
                b.run(13 * 3600)
                sent = [c[2] for c in b.modem.sent]
                for cmd in sent:
                    self.assertFalse(cmd.startswith(banned),
                                     '%s at level %d sent %s' % (name, level, cmd))

    def test_heavy_at_refuses_a_forbidden_command(self):
        b = harness.Bench()
        b.start()
        for cmd in ('AT+CFUN=0', 'AT+CFUN=4', 'AT+COPS=2', 'AT+CGATT=0'):
            self.assertEqual(b.svc._heavy_at(cmd), 'impossible', cmd)
        self.assertEqual(b.modem.sent_cmds('AT+CFUN=0'), [])
        self.assertTrue(b.logs('refusing to send AT+CFUN=0'))

    def test_cfun_1_1_is_never_used_anywhere(self):
        b = harness.Bench()
        b.modem.creg = 3
        b.modem.creg_after_reset = 3
        b.start()
        b.run(6 * 3600)
        self.assertEqual(b.modem.sent_cmds('AT+CFUN=1,1'), [])

    def test_no_two_modem_resets_within_ten_minutes(self):
        b = harness.Bench()
        b.modem.creg = 3
        b.modem.creg_after_reset = 3
        b.start()
        b.run(3 * 3600)
        stamps = [c[0] for c in b.heavy() if c[2] == 'AT^RESET']
        self.assertGreaterEqual(len(stamps), 2)
        for a, c in zip(stamps, stamps[1:]):
            self.assertGreaterEqual(c - a, 600, stamps)

    def test_destructive_budget_never_exceeded(self):
        b = harness.Bench()
        b.modem.creg = 3
        b.modem.creg_after_reset = 3
        b.start()
        b.run(6 * 3600)
        acts = sorted([c[0] for c in b.heavy() if c[2] == 'AT^RESET']
                      + [c[0] for c in b.system.calls if c[1] == 'spawn'])
        # One full ladder is three destructive rungs; two full ladders inside
        # the same 30 min window must stay impossible.
        for t in acts:
            self.assertLessEqual(len([x for x in acts if t <= x < t + 1800]), 3, acts)

    def test_a_ladder_held_by_the_budget_is_deferred_not_counted(self):
        """A ladder that could not run a single command must not count: it
        would double the backoff for nothing."""
        b = harness.Bench()
        b.mod.DESTRUCTIVE_MAX = 1         # one destructive rung per window
        b.modem.responsive = False        # at_mute: an all-destructive ladder
        b.modem.responsive_after_reset = False   # the reset does not help
        b.start()
        b.run(90 * 60)
        self.assertTrue(b.logs('deferred, every rung is held'), b.logs('recovery:'))
        self.assertEqual(b.logs('exhausted after 0 rungs'), [])
        # The deferred ladder keeps its number: it never ran, so it must not
        # consume one of the backoff steps.
        after_defer = [m for m in b.logs('ladder #') if 'starting' in m]
        numbers = [int(m.split('ladder #')[1].split(' ')[0]) for m in after_defer]
        self.assertEqual(numbers.count(2), 2,
                         'ladder #2 should be retried under the same number: %s' % numbers)


class EffectivenessTests(unittest.TestCase):

    def test_a_reset_that_never_left_the_bus_is_marked_ineffective(self):
        """AT^RESET answering OK is not proof: only devnum changing is."""
        b = harness.Bench()
        b.modem.creg = 3
        b.modem.on('AT^RESET', lambda c: ['OK'])      # answers, does nothing
        b.start()
        b.run(30 * 60)
        self.assertTrue(b.logs('left devnum at'), b.logs('recovery:'))
        self.assertTrue(b.logs('treating the rung as ineffective'))
        self.assertTrue(b.spawned('portcycle'))       # escalated straight away
        # Read the persisted history: the USB rung that followed killed the
        # service, so the live object may be gone.
        hist = [h for h in (b.state_file() or {}).get('history', [])
                if h['rung'] == 'MODEM_RESET']
        self.assertTrue(any(h['result'] == 'ineffective' for h in hist), hist)

    def test_a_refused_reset_falls_through_to_the_usb_rung(self):
        b = harness.Bench()
        b.modem.creg = 3
        b.modem.reset_refused = True
        b.start()
        b.run(30 * 60)
        self.assertIn('AT^RESET', [c[2] for c in b.heavy()])
        self.assertEqual(b.modem.firmware_resets, 0)   # the reset was refused
        self.assertTrue(b.spawned('portcycle'))

    def test_usb_reenum_does_not_clear_the_firmware_state(self):
        """Measured in situ: the device comes back, the radio stays off."""
        b = harness.Bench()
        b.modem.cfun = 4
        b.modem.write_locked = True
        b.modem.reset_refused = True      # force the ladder down to USB
        b.start()
        b.run(40 * 60)
        self.assertTrue(b.spawned('portcycle'))
        self.assertEqual(b.modem.cfun, 4)         # unchanged by the bus reset
        self.assertTrue(b.modem.write_locked)
        self.assertTrue(b.spawned('rebind'))      # so it escalated further


class ControllerRebindTests(unittest.TestCase):
    """Rebinding the USB controller re-enumerates everything on it. On the
    reference install that includes the BMV-712 battery monitor - the active
    battery service, with DVCC on - so the rung must look before it acts."""

    def _stubborn(self, **kw):
        b = harness.Bench(**kw)
        b.modem.creg = 3
        b.modem.creg_after_reset = 3      # nothing but the last rung is left
        return b

    def test_refused_when_the_battery_monitor_shares_the_controller(self):
        b = self._stubborn()
        b.system.add_usb_device('3-2', '0403', '6001', 'TTL232R-3V3')
        b.start()
        b.run(3 * 3600)
        self.assertTrue(b.spawned('portcycle'))       # the rungs below still run
        self.assertEqual(b.spawned('rebind'), [])
        refusals = b.logs('controller rebind refused: 3-2 (TTL232R-3V3) also on xhci-hcd.1')
        self.assertTrue(refusals, b.logs('recovery:'))
        hist = [h for h in (b.state_file() or {}).get('history', [])
                if h['rung'] == 'HCI_REBIND']
        self.assertTrue(hist and all(h['result'] == 'impossible' for h in hist), hist)

    def test_allowed_when_the_owner_accepted_the_collateral(self):
        b = self._stubborn(extra_cfg={'HCI_REBIND_SHARED': '1'})
        b.system.add_usb_device('3-2', '0403', '6001', 'TTL232R-3V3')
        b.start()
        b.run(3 * 3600)
        self.assertTrue(b.spawned('rebind'))
        self.assertEqual(b.logs('controller rebind refused'), [])

    def test_devices_on_the_other_controller_do_not_block_it(self):
        b = self._stubborn()
        b.system.add_usb_device('1-2.1', '1a86', '7523', 'USB Serial', hci='xhci-hcd.0')
        b.system.add_usb_device('1-1', '1546', '01a7', 'u-blox 7', hci='xhci-hcd.0')
        b.start()
        b.run(3 * 3600)
        self.assertTrue(b.spawned('rebind'))
        self.assertEqual(b.logs('controller rebind refused'), [])


class LevelTests(unittest.TestCase):

    def test_level_1_stops_before_the_modem_reset(self):
        b = harness.Bench(level=1)
        b.modem.creg = 3
        b.start()
        b.run(40 * 60)
        self.assertEqual(set(c[2] for c in b.heavy()), {'AT+COPS=0'})
        self.assertTrue(b.logs('skipped (RECOVERY_LEVEL=1)'))
        self.assertEqual(b.modem.firmware_resets, 0)
        self.assertEqual(b.system.spawned, [])

    def test_level_2_resets_but_never_touches_usb(self):
        b = harness.Bench(level=2)
        b.modem.creg = 3
        b.modem.creg_after_reset = 3
        b.start()
        b.run(3 * 3600)
        self.assertIn('AT^RESET', [c[2] for c in b.heavy()])
        self.assertEqual(b.system.spawned, [])
        self.assertTrue(b.logs('USB_REENUM skipped (RECOVERY_LEVEL=2)'))

    def test_level_3_stops_before_the_controller_rebind(self):
        b = harness.Bench(level=3)
        b.modem.creg = 3
        b.modem.creg_after_reset = 3
        b.start()
        b.run(3 * 3600)
        self.assertTrue(b.spawned('portcycle'))
        self.assertEqual(b.spawned('rebind'), [])

    def test_level_0_observes_only(self):
        b = harness.Bench(level=0)
        b.modem.creg = 3
        b.start()
        b.run(60 * 60)
        self.assertEqual(b.heavy(), [])
        self.assertTrue(b.logs('recovery: [reg] stuck'))
        self.assertEqual(b.system.spawned, [])


class MuteModemTests(unittest.TestCase):

    def test_mute_port_is_reset_after_six_polls(self):
        b = harness.Bench()
        b.modem.responsive = False
        b.start()
        self.assertTrue(b.logs('Modem not responding'))
        b.run(15 * 60)
        resets = [c for c in b.heavy() if c[2] == 'AT^RESET']
        self.assertEqual(len(resets), 1)
        self.assertEqual(resets[0][1], 'wdm')
        self.assertAlmostEqual(resets[0][0], 1060, delta=20)
        self.assertTrue(b.logs('recovery: [at_mute] stuck'))
        self.assertTrue(b.logs('recovered at rung 1/3 MODEM_RESET'))
        self.assertEqual(b.dbus('/Model'), 'E3372')

    def test_mute_port_without_wdm_goes_to_the_usb_rung(self):
        b = harness.Bench()
        b.modem.responsive = False
        b.modem.wdm_available = False
        b.start()
        b.run(15 * 60)
        self.assertTrue(b.spawned('portcycle'))
        self.assertTrue(b.logs('recovered at rung 2/3 USB_REENUM'))

    def test_an_intermittently_silent_poll_still_reaches_the_ladder(self):
        """One silent poll must not wipe the reason and restart the grace."""
        b = harness.Bench()
        b.modem.creg = 3
        state = {'n': 0}

        def flaky(cmd):
            state['n'] += 1
            return harness.fake_modem.NO_ANSWER if state['n'] % 8 == 0 else None
        b.modem.on('AT+CPIN?', flaky)
        b.start()
        b.run(20 * 60)
        self.assertTrue(b.logs('recovery: [reg] stuck'))
        self.assertIn('AT+COPS=0', [c[2] for c in b.heavy()])
        self.assertEqual(b.logs('recovery: [reg] cleared after'), [])

    def test_port_that_cannot_be_opened(self):
        b = harness.Bench()
        b.modem.port_open_ok = False
        b.start()
        self.assertTrue(b.logs('Failed to open modem, will keep trying'))
        b.run(10 * 60)
        self.assertIn('AT^RESET', [c[2] for c in b.heavy()])


class RobustnessTests(unittest.TestCase):

    def test_state_file_from_another_boot_is_ignored(self):
        b = harness.Bench()
        import json
        b.system.files['/run/e3372-recovery.json'] = json.dumps({
            'boot_id': 'boot-0', 'state': 'wait', 'reason': 'reg', 'ladder_count': 3,
            'next_ladder_at': 99999.0, 'busy_until': 99999.0})
        b.start()
        self.assertTrue(b.logs('from another boot, ignored'))
        self.assertEqual(b.svc.recovery.ladder_count, 0)
        self.assertEqual(b.svc.recovery.state, 'idle')

    def test_update_steps_are_isolated(self):
        b = harness.Bench()
        b.start()
        b.svc._update_status = lambda: 1 / 0
        b.run(30)
        self.assertTrue(b.logs('update step status failed'))
        self.assertIsNotNone(b.svc.recovery.last_snap)

        def boom(now):
            raise RuntimeError('tick')
        b.svc.recovery._tick = boom
        b.run(70)
        self.assertTrue(b.logs('too many failures, resetting to idle'))
        self.assertEqual(b.svc.recovery.state, 'idle')

    def test_a_slow_command_does_not_block_or_count_as_mute(self):
        b = harness.Bench()
        b.modem.creg = 3
        b.modem.on('AT+COPS=0', lambda c: harness.fake_modem.NO_ANSWER)
        b.start()
        b.run(300)
        self.assertIn('AT+COPS=0', [c[2] for c in b.heavy()])
        self.assertLessEqual(max(b.poll_durations), 8.0, b.poll_durations)
        self.assertEqual(b.svc.recovery.mute_polls, 0)
        self.assertEqual(b.logs('recovery: [at_mute]'), [])

    def test_register_retry(self):
        b = harness.Bench()
        harness.stubs.FakeVeDbusService.register_failures = 2
        self.assertTrue(b.start())
        self.assertTrue(b.svc.dbus.registered)
        self.assertEqual(len(b.logs('D-Bus name busy')), 2)

    def test_empty_csq_field_does_not_crash(self):
        """v1.2 died on int('') here and was respawned in a loop."""
        b = harness.Bench()
        b.modem.csq_empty = True
        self.assertTrue(b.start())
        b.run(60)
        self.assertEqual(b.logs('update step status failed'), [])
        self.assertEqual(b.dbus('/Connected'), 1)


if __name__ == '__main__':
    unittest.main()
