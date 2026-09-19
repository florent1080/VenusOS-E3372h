import unittest

import harness


class SimPinTests(unittest.TestCase):

    def test_correct_pin_is_sent_once(self):
        b = harness.Bench(settings={'pin': '1234'})
        b.modem.cpin = 'SIM PIN'
        b.modem.pin = '1234'
        b.start()
        b.run(5 * 60)
        self.assertEqual([c[2] for c in b.modem.sent_cmds('AT+CPIN=')], ['AT+CPIN="1234"'])
        self.assertEqual(b.dbus('/SimStatus'), 1000)
        self.assertTrue(b.logs('sim: 11 (PIN required) -> 1000 (ready)'))
        self.assertEqual(b.heavy_cmds(), [])
        self.assertEqual(b.dbus('/Connected'), 1)

    def test_wrong_pin_is_sent_once_and_cleared(self):
        b = harness.Bench(settings={'pin': '1234'})
        b.modem.cpin = 'SIM PIN'
        b.modem.pin = '0000'
        b.start()
        b.run(60 * 60)
        self.assertEqual(len(b.modem.sent_cmds('AT+CPIN=')), 1)
        self.assertEqual(b.svc.settings['pin'], '')
        self.assertEqual(b.dbus('/SimStatus'), 16)
        self.assertTrue(b.logs('wrong PIN, clearing'))
        self.assertEqual(b.heavy_cmds(), [])
        # a new PIN from the GUI is tried once
        b.svc.settings.change('pin', '0000')
        b.run(30)
        self.assertEqual(len(b.modem.sent_cmds('AT+CPIN=')), 2)
        self.assertEqual(b.dbus('/SimStatus'), 1000)

    def test_missing_pin_is_nagged_not_escalated(self):
        b = harness.Bench()
        b.modem.cpin = 'SIM PIN'
        b.modem.pin = '1234'
        b.start()
        b.run(30 * 60)
        self.assertEqual(b.modem.sent_cmds('AT+CPIN='), [])
        self.assertEqual(b.heavy_cmds(), [])
        nags = b.logs('PIN required but /Settings/Modem/PIN is empty')
        self.assertGreaterEqual(len(nags), 2)
        self.assertLessEqual(len(nags), 4)
        self.assertEqual(b.dbus('/SimStatus'), 11)

    def test_sim_relocks_after_radio_cycle(self):
        b = harness.Bench(settings={'pin': '1234'})
        b.modem.pin = '1234'          # SIM locked, but already unlocked at start
        b.modem.creg = 3              # forces a ladder with a CFUN cycle
        b.modem.creg_after_cfun = 1
        b.start()
        b.run(20 * 60)
        self.assertIn('AT+CFUN=1', b.heavy_cmds())
        self.assertEqual(len(b.modem.sent_cmds('AT+CPIN=')), 1)
        self.assertEqual(b.dbus('/SimStatus'), 1000)
        self.assertTrue(b.logs('recovered at rung 2/4 CFUN_CYCLE'))


class SimStateTests(unittest.TestCase):

    def test_no_sim_is_published_and_reset_after_grace(self):
        b = harness.Bench()
        b.modem.cpin = ('CME', 10)
        b.start()
        self.assertEqual(b.dbus('/SimStatus'), 10)
        self.assertEqual(b.dbus('/Connected'), 0)
        b.run(4 * 60)
        self.assertEqual(b.heavy_cmds(), [])          # 5 min grace
        self.assertTrue(b.logs('recovery: [sim] stuck (SIM no SIM)'))
        b.run(3 * 60)
        self.assertEqual(b.heavy_cmds()[:2], ['AT+CFUN=0', 'AT+CFUN=1'])
        # SIM back after the modem reset
        b.modem.cpin = 'READY'
        b.run(10 * 60)
        self.assertTrue(b.logs('recovered at rung'))
        self.assertEqual(b.dbus('/SimStatus'), 1000)

    def test_puk_needs_a_human(self):
        b = harness.Bench()
        b.modem.cpin = 'SIM PUK'
        b.start()
        b.run(30 * 60)
        self.assertEqual(b.dbus('/SimStatus'), 12)
        self.assertEqual(b.heavy_cmds(), [])
        self.assertTrue(b.logs('human intervention needed'))
        self.assertEqual(b.logs('recovery: [sim]'), [])

    def test_sim_busy_then_ready_within_grace(self):
        b = harness.Bench()
        b.modem.cpin = ('CME', 14)
        b.modem.schedule(60, cpin='READY')
        b.start()
        b.run(10 * 60)
        self.assertEqual(b.heavy_cmds(), [])
        self.assertTrue(b.logs('recovery: [sim] cleared'))
        self.assertEqual(b.dbus('/SimStatus'), 1000)
        self.assertEqual(b.dbus('/Connected'), 1)


if __name__ == '__main__':
    unittest.main()
