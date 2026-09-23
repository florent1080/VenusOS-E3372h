import unittest

import harness


class SessionTests(unittest.TestCase):

    def test_new_instance_keeps_a_session_that_is_up(self):
        b = harness.Bench()
        b.modem.ndis = 1
        b.system.files['/var/run/udhcpc.wwan0.pid'] = '999'
        b.system.udhcpc_pid = 999
        b.system.ip = '10.75.194.118'
        b.start()
        self.assertTrue(b.logs('session: already up, keeping it'))
        self.assertEqual(b.modem.sent_cmds('AT^NDISDUP'), [])
        b.run(60)
        self.assertEqual(b.system.udhcpc_pid, 999)          # DHCP untouched
        self.assertEqual(b.modem.sent_cmds('AT^NDISDUP'), [])
        self.assertEqual(b.dbus('/PPPStatus'), 2)
        self.assertEqual(b.dbus('/Connected'), 1)
        self.assertEqual(b.dbus('/IP'), '10.75.194.118')

    def test_two_phase_dial_never_blocks(self):
        b = harness.Bench()
        b.modem.dial_delay = 12
        b.start()
        self.assertEqual(len(b.modem.sent_cmds('AT^NDISDUP=1,1')), 1)
        b.run(60)
        self.assertLessEqual(max(b.poll_durations), 5.0, b.poll_durations)
        self.assertTrue(b.logs('session: up'))
        self.assertEqual(b.dbus('/Connected'), 1)
        self.assertEqual(b.svc.dial_failures, 0)
        self.assertIsNotNone(b.svc.session_up_at)
        # DHCP was (re)started after the session came up
        self.assertTrue(any(c[1] == 'sh' and c[2].startswith('udhcpc') for c in b.system.calls))

    def test_dial_failures_escalate_to_the_ladder(self):
        b = harness.Bench()
        b.modem.ndisdup_auto_up = False
        b.start()
        b.run(6 * 60)
        self.assertGreaterEqual(b.svc.dial_failures, 5)
        self.assertTrue(b.logs('session: dial failed, session still down (5 consecutive)'))
        self.assertTrue(b.logs('recovery: [dial] stuck (5 consecutive dial failures)'))
        self.assertEqual([c[2] for c in b.heavy()][:1], ['AT+COPS=0'])
        # after the settle time a verification dial is made before judging
        b.run(3 * 60)
        self.assertTrue(b.logs('recovery: [dial] verification dial'))
        # the modem fixed by the re-selection: the verification dial succeeds
        b2 = harness.Bench()
        b2.modem.ndisdup_auto_up = False
        b2.modem.on('AT+COPS=0', lambda c: (setattr(b2.modem, 'ndisdup_auto_up', True), ['OK'])[1])
        b2.start()
        b2.run(12 * 60)
        self.assertTrue(b2.logs('recovered at rung 1/3 COPS_AUTO'), b2.logs('recovery:'))
        self.assertEqual(b2.dbus('/Connected'), 1)
        self.assertEqual(b2.modem.firmware_resets, 0)

    def test_double_drop_keeps_the_backoff(self):
        b = harness.Bench()
        b.modem.ndis = 1
        b.system.ip = '10.0.0.2'
        b.start()
        b.run(30)
        b.modem.ndis = 0                       # first drop
        b.run(10)
        self.assertTrue(b.logs('session: down after'))
        self.assertTrue(b.logs('re-dialling (attempt 1, next retry in 30s)'))
        b.run(10)                              # session back up (dial_delay 3 s)
        self.assertEqual(b.dbus('/Connected'), 1)
        b.modem.ndis = 0                       # dies again 10-20 s later
        b.run(40)
        self.assertTrue(b.logs('re-dialling (attempt 2, next retry in 60s)'))
        self.assertEqual(b.svc.redial_count, 2)
        # stable for 60 s: backoff forgiven
        b.run(90)
        self.assertTrue(b.logs('re-dial backoff reset'))
        self.assertEqual(b.svc.redial_count, 0)

    def test_only_one_dhcp_client_is_started_after_a_dial(self):
        """udhcpc writes its pidfile a moment after it starts; the next poll
        must not take that for "nothing is running" and start a second one."""
        b = harness.Bench()
        b.modem.dial_delay = 3
        b.start()
        b.run(40)
        starts = [c for c in b.system.calls
                  if c[1] == 'sh' and c[2].startswith('udhcpc')]
        self.assertEqual(len(starts), 1, starts)

    def test_an_orphan_dhcp_client_is_stopped_with_the_others(self):
        """A client whose pid was overwritten in the pidfile ran unseen for
        21 h on the real Pi. A restart must stop every client on the
        interface, not only the one the pidfile names."""
        b = harness.Bench()
        b.modem.ndis = 1
        b.system.ip = '10.0.0.2'
        b.system.dhcp_pids.update({555, 999})          # 555 is the orphan
        b.system.files['/var/run/udhcpc.wwan0.pid'] = '999'
        b.start()
        b.run(20)
        b.modem.ndis = 0                               # drop -> redial -> restart
        b.run(60)
        killed = [c[2] for c in b.system.calls if c[1] == 'kill']
        self.assertIn(555, killed)
        self.assertIn(999, killed)
        self.assertEqual(len(b.system.dhcp_pids), 1, b.system.dhcp_pids)

    def test_duplicate_dhcp_clients_are_reaped_while_the_link_is_healthy(self):
        b = harness.Bench()
        b.modem.ndis = 1
        b.system.ip = '10.0.0.2'
        b.system.dhcp_pids.update({555, 999})
        b.system.files['/var/run/udhcpc.wwan0.pid'] = '999'
        b.start()
        b.run(70)
        self.assertEqual(b.system.dhcp_pids, {999})    # the pidfile's one is kept
        self.assertTrue(b.logs('2 DHCP clients on wwan0, keeping 999 and stopping 555'))
        self.assertEqual(b.dbus('/Connected'), 1)
        self.assertEqual(b.modem.sent_cmds('AT^NDISDUP'), [])   # the session was left alone

    def test_stale_dhcp_is_restarted(self):
        b = harness.Bench()
        b.modem.ndis = 1
        b.system.files['/var/run/udhcpc.wwan0.pid'] = '999'
        b.system.udhcpc_pid = 999             # a client that never gets a lease
        b.start()
        b.run(90)
        self.assertTrue(b.logs('no address for'))
        self.assertTrue(any(c[1] == 'kill' and c[2] == 999 for c in b.system.calls))
        self.assertNotEqual(b.system.udhcpc_pid, 999)
        b.run(20)
        self.assertEqual(b.dbus('/IP'), '10.0.0.2')

    def test_hangup_on_connect_off_and_redial_on_connect_on(self):
        b = harness.Bench()
        b.modem.ndis = 1
        b.start()
        b.svc.settings.change('connect', 0)
        self.assertTrue(b.modem.sent_cmds('AT^NDISDUP=1,0'))
        b.modem.ndis = 0
        b.run(60)
        self.assertEqual(b.modem.sent_cmds('AT^NDISDUP=1,1'), [])
        self.assertEqual(b.dbus('/Connected'), 0)
        b.svc.settings.change('connect', 1)
        b.run(20)
        self.assertEqual(len(b.modem.sent_cmds('AT^NDISDUP=1,1')), 1)
        self.assertEqual(b.dbus('/Connected'), 1)

    def test_roaming_not_permitted_stays_offline(self):
        b = harness.Bench()
        b.modem.creg = 5
        b.start()
        b.run(20 * 60)
        self.assertEqual(b.modem.sent_cmds('AT^NDISDUP=1,1'), [])
        self.assertEqual(b.heavy_cmds(), [])
        self.assertTrue(b.logs('roaming not permitted'))
        self.assertEqual(b.dbus('/Roaming'), True)
        b.svc.settings.change('roaming', 1)
        b.run(30)
        self.assertEqual(len(b.modem.sent_cmds('AT^NDISDUP=1,1')), 1)

    def test_signal_and_transitions_are_logged(self):
        b = harness.Bench()
        b.start()
        b.run(130)
        sig = b.logs('signal: LTE rssi=-82dBm rsrp=-114dBm sinr=-3dB rsrq=-11dB')
        self.assertGreaterEqual(len(sig), 2)
        self.assertEqual(b.dbus('/Signal/Rsrp'), -114)
        self.assertTrue(b.logs('sim: unknown -> 1000 (ready)'))
        self.assertTrue(b.logs('reg: unknown -> 1 (home)'))
        self.assertEqual(b.dbus('/NetworkType'), 'LTE')
        self.assertEqual(b.dbus('/NetworkName'), 'Bouygues Telecom')
        self.assertTrue(b.modem.sent_cmds('AT^CURC=0'))


if __name__ == '__main__':
    unittest.main()
