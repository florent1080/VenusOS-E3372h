import re
import unittest

import harness

HELPER = '/data/e3372_usb_reset.sh'
SYSFS = '/sys/bus/usb/devices/3-1'


class DeniedLadderTests(unittest.TestCase):

    def test_denied_escalates_to_modem_reset_and_recovers(self):
        b = harness.Bench()
        b.modem.creg = 3                 # registration denied from the start
        b.modem.creg_after_reset = 1     # only a full reset clears it
        b.start()
        b.run(40 * 60)

        cmds = b.heavy()
        names = [c[2] for c in cmds]
        self.assertEqual(names, ['AT+COPS=2', 'AT+COPS=0', 'AT+CFUN=0', 'AT+CFUN=1', 'AT+CFUN=1,1'])
        self.assertTrue(all(c[1] == 'wdm' for c in cmds), cmds)
        # nothing before the 120 s grace period
        self.assertGreaterEqual(cmds[0][0] - 1000, 120)
        # the two commands of a cycle are one poll apart, then a settle period
        self.assertGreaterEqual(cmds[1][0] - cmds[0][0], 10)
        self.assertGreaterEqual(cmds[2][0] - cmds[1][0], 90)
        self.assertGreaterEqual(cmds[4][0] - cmds[3][0], 120)
        # the state was persisted before the destructive command
        t_reset = cmds[4][0]
        saved = [w for w in b.system.writes if w[1].endswith('recovery.json') and w[0] <= t_reset]
        self.assertTrue(saved)
        import json
        last = json.loads(saved[-1][2])
        self.assertEqual(last['state'], 'rung')
        self.assertEqual(last['rung_idx'], 2)
        self.assertEqual(last['phase'], 'settle')
        # the modem went away, serial-starter restarted the service
        self.assertEqual(b.modem.reset_count, 1)
        self.assertEqual(b.restarts, 1)
        # the new instance waited out the settle time, then judged
        self.assertTrue(b.logs('loaded, ladder_count 0'))
        self.assertTrue(b.logs('recovered at rung 3/4 CFUN_RESET'), b.logs('recovery:'))
        self.assertEqual(b.svc.recovery.state, 'idle')
        self.assertEqual(b.svc.recovery.ladder_count, 0)
        # and the session was dialled and came up
        self.assertTrue(b.modem.sent_cmds('AT^NDISDUP=1,1'))
        self.assertEqual(b.dbus('/Connected'), 1)
        self.assertEqual(b.dbus('/RegStatus'), 1)
        self.assertEqual(b.dbus('/Recovery/State'), 'idle')

    def test_exhausted_ladder_backs_off_and_never_gives_up(self):
        b = harness.Bench()
        b.modem.creg = 3
        b.modem.creg_after_reset = 3     # nothing helps
        b.start()
        b.run(13 * 3600)

        # the USB helper was launched with the right sysfs path, DHCP stopped first
        self.assertIn([HELPER, SYSFS], b.system.spawned)
        t_usb = [c[0] for c in b.system.calls if c[1] == 'spawn'][0]
        kills = [c for c in b.system.calls if c[1] == 'kill' and c[0] <= t_usb]
        self.assertTrue(kills or b.system.udhcpc_pid is None)
        # backoff between full ladders: 15m, 30m, 1h, 2h, 4h, 4h ...
        waits = [re.search(r'next ladder in (\S+)', m).group(1)
                 for m in b.logs('exhausted after')]
        self.assertEqual(waits[:6], ['15m00s', '30m00s', '1h00m', '2h00m', '4h00m', '4h00m'])
        self.assertGreaterEqual(b.svc.recovery.ladder_count, 6)
        # while waiting, no recovery command is sent
        starts = [m for m in b.logs('ladder #') if 'starting' in m]
        self.assertGreaterEqual(len(starts), 6)
        # never more than two destructive resets per 30 minutes
        resets = [c[0] for c in b.heavy() if c[2] == 'AT+CFUN=1,1'] + \
                 [c[0] for c in b.system.calls if c[1] == 'spawn']
        resets.sort()
        for i in range(len(resets)):
            window = [t for t in resets if resets[i] <= t < resets[i] + 1800]
            self.assertLessEqual(len(window), 2, resets)
        self.assertTrue(b.logs('refused: 2 destructive resets'))
        # the state file survives the restarts and carries the backoff
        self.assertEqual(b.state_file()['ladder_count'], b.svc.recovery.ladder_count)

    def test_recovery_mid_ladder_stops_escalation(self):
        b = harness.Bench()
        b.modem.creg = 3
        b.modem.on('AT+COPS=0', lambda c: (b.modem.schedule(30, creg=1), ['OK'])[1])
        b.start()
        b.run(20 * 60)
        self.assertEqual(b.heavy_cmds(), ['AT+COPS=2', 'AT+COPS=0'])
        self.assertTrue(b.logs('recovered at rung 1/4 COPS_CYCLE'))
        self.assertEqual(b.svc.recovery.ladder_count, 0)
        self.assertEqual(b.svc.recovery.state, 'idle')
        self.assertEqual(b.dbus('/Connected'), 1)
        self.assertTrue(any(h['result'] == 'recovered' for h in b.svc.recovery.history))

    def test_cops2_refused_by_firmware_still_sends_cops0(self):
        # The real E3372h answers "+CME ERROR: 50" to AT+COPS=2. The rung must
        # carry on to AT+COPS=0, which is the command that actually repairs a
        # stuck network selection.
        b = harness.Bench()
        b.modem.creg = 3
        b.modem.on('AT+COPS=2', lambda c: ['+CME ERROR: 50'])
        b.modem.on('AT+COPS=0', lambda c: (b.modem.schedule(20, creg=1), ['OK'])[1])
        b.start()
        b.run(20 * 60)
        self.assertEqual(b.heavy_cmds(), ['AT+COPS=2', 'AT+COPS=0'])
        self.assertTrue(b.logs('step 1 of COPS_CYCLE is optional, carrying on'))
        self.assertTrue(b.logs('recovered at rung 1/4 COPS_CYCLE'))
        self.assertEqual(b.dbus('/Connected'), 1)

    def test_whole_rung_refused_goes_to_the_next_one(self):
        b = harness.Bench()
        b.modem.creg = 3
        b.modem.on('AT+COPS=', lambda c: ['+CME ERROR: 50'])
        b.modem.creg_after_cfun = 1
        b.start()
        b.run(20 * 60)
        cmds = b.heavy_cmds()
        self.assertEqual(cmds[:4], ['AT+COPS=2', 'AT+COPS=0', 'AT+CFUN=0', 'AT+CFUN=1'])
        self.assertTrue(b.logs('recovered at rung 2/4 CFUN_CYCLE'))

    def test_radio_off_is_recovered_by_the_cfun_cycle(self):
        # AT+CFUN=4 (radio off) is how the in-situ test reproduces a total loss
        # of registration: the SIM stays readable, CREG answers 0, and no
        # network selection can succeed - only the radio cycle brings it back.
        b = harness.Bench()
        b.modem.cfun = 4
        b.start()
        b.run(25 * 60)
        cmds = b.heavy_cmds()
        self.assertEqual(cmds[:4], ['AT+COPS=2', 'AT+COPS=0', 'AT+CFUN=0', 'AT+CFUN=1'])
        self.assertTrue(b.logs('recovery: [reg] stuck (CREG 0 not registered)'))
        self.assertTrue(b.logs('step 1 of COPS_CYCLE is optional, carrying on'))
        self.assertTrue(b.logs('recovered at rung 2/4 CFUN_CYCLE'), b.logs('recovery:'))
        self.assertEqual(b.modem.cfun, 1)
        self.assertEqual(b.dbus('/RegStatus'), 1)
        self.assertEqual(b.dbus('/Connected'), 1)
        self.assertEqual(b.modem.reset_count, 0)   # no modem reset was needed

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
        self.assertEqual(b.heavy_cmds(), [])
        self.assertTrue(b.logs('reg: 3 (denied) -> 1 (home)'))

    def test_connect_off_cancels_the_ladder(self):
        b = harness.Bench()
        b.modem.creg = 3
        b.start()
        b.run(130)
        self.assertEqual(b.heavy_cmds()[:1], ['AT+COPS=2'])
        b.svc.settings.change('connect', 0)
        self.assertEqual(b.svc.recovery.state, 'idle')
        n = len(b.heavy_cmds())
        b.run(30 * 60)
        self.assertEqual(len(b.heavy_cmds()), n)
        self.assertTrue(b.logs('cancelled'))


class LevelTests(unittest.TestCase):

    def test_level_1_only_reselects(self):
        b = harness.Bench(level=1)
        b.modem.creg = 3
        b.start()
        b.run(30 * 60)
        self.assertEqual(set(b.heavy_cmds()), {'AT+COPS=2', 'AT+COPS=0'})
        self.assertTrue(b.logs('skipped (RECOVERY_LEVEL=1)'))
        self.assertTrue(b.logs('next ladder in 15m00s'))
        # two short ladders fit in 30 min (15 min pause after the first one)
        self.assertEqual(b.state_file()['ladder_count'], 2)
        self.assertEqual(len(b.heavy_cmds()), 4)

    def test_level_0_observes_only(self):
        b = harness.Bench(level=0)
        b.modem.creg = 3
        b.start()
        b.run(60 * 60)
        self.assertEqual(b.heavy_cmds(), [])
        self.assertTrue(b.logs('recovery: [reg] stuck'))
        self.assertTrue(b.logs('exhausted'))
        self.assertEqual(b.system.spawned, [])


class MuteModemTests(unittest.TestCase):

    def test_mute_port_is_reset_via_wdm_after_six_polls(self):
        b = harness.Bench()
        b.modem.responsive = False
        b.start()
        self.assertTrue(b.logs('Modem not responding'))
        b.run(15 * 60)
        resets = [c for c in b.heavy() if c[2] == 'AT+CFUN=1,1']
        self.assertEqual(len(resets), 1)
        self.assertEqual(resets[0][1], 'wdm')
        # sixth poll: t = 1010 + 5 * 10
        self.assertAlmostEqual(resets[0][0], 1060, delta=15)
        self.assertTrue(b.logs('recovery: [at_mute] stuck'))
        self.assertTrue(b.logs('recovered at rung 1/2 CFUN_RESET'))
        self.assertEqual(b.dbus('/Model'), 'E3372')

    def test_mute_port_without_wdm_goes_to_usb_reset(self):
        b = harness.Bench()
        b.modem.responsive = False
        b.modem.wdm_available = False
        b.start()
        b.run(15 * 60)
        self.assertEqual([c[1] for c in b.heavy() if c[2] == 'AT+CFUN=1,1'], ['serial'])
        self.assertIn([HELPER, SYSFS], b.system.spawned)
        self.assertTrue(b.logs('recovered at rung 2/2 USB_RESET'))

    def test_port_that_cannot_be_opened(self):
        b = harness.Bench()
        b.modem.port_open_ok = False
        b.start()
        self.assertTrue(b.logs('Failed to open modem, will keep trying'))
        b.run(10 * 60)
        self.assertIn('AT+CFUN=1,1', b.heavy_cmds())
        self.assertTrue(b.logs('recovered at rung 1/2 CFUN_RESET'))


class RobustnessTests(unittest.TestCase):

    def test_cfun0_does_not_look_like_a_missing_sim(self):
        b = harness.Bench()
        b.modem.creg = 3
        b.modem.creg_after_reset = 1
        b.start()
        b.run(40 * 60)
        self.assertIn('AT+CFUN=1,1', b.heavy_cmds())
        self.assertEqual(b.logs('recovery: [sim]'), [])
        self.assertTrue(b.logs('recovered at rung 3/4'))

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
        self.assertEqual(b.svc.recovery.busy_until, 0.0)

    def test_update_steps_are_isolated(self):
        b = harness.Bench()
        b.start()
        b.svc._update_status = lambda: 1 / 0
        b.run(30)
        self.assertTrue(b.logs('update step status failed'))
        self.assertIsNotNone(b.svc.recovery.last_snap)
        # six tick failures in a row reset the automaton
        def boom(now):
            raise RuntimeError('tick')
        b.svc.recovery._tick = boom
        b.run(70)
        self.assertTrue(b.logs('too many failures, resetting to idle'))
        self.assertEqual(b.svc.recovery.state, 'idle')

    def test_slow_cops_does_not_block_or_count_as_mute(self):
        b = harness.Bench()
        b.modem.creg = 3
        b.modem.on('AT+COPS=0', lambda c: harness.fake_modem.NO_ANSWER)
        b.start()
        b.run(270)      # up to the radio cycle, before the modem reset
        self.assertIn('AT+COPS=0', b.heavy_cmds())
        self.assertLessEqual(max(b.poll_durations), 6.0, b.poll_durations)
        self.assertIsNotNone(b.svc)
        self.assertEqual(b.svc.recovery.mute_polls, 0)
        self.assertIn('AT+CFUN=0', b.heavy_cmds())   # the ladder went on
        self.assertEqual(b.logs('recovery: [at_mute]'), [])

    def test_register_retry(self):
        b = harness.Bench()
        harness.stubs.FakeVeDbusService.register_failures = 2
        self.assertTrue(b.start())
        self.assertTrue(b.svc.dbus.registered)
        self.assertEqual(len(b.logs('D-Bus name busy')), 2)


if __name__ == '__main__':
    unittest.main()
