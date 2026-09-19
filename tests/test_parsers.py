import unittest

import harness


class ParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = harness.load_service()

    def test_hcsq_lte(self):
        sig = self.mod.hcsq_to_dbm('^HCSQ:"LTE",39,27,86,18')
        self.assertEqual(sig, {'mode': 'LTE', 'rssi': -82, 'rsrp': -114, 'sinr': -3.0, 'rsrq': -11.0})

    def test_hcsq_floor_ceiling_unknown(self):
        sig = self.mod.hcsq_to_dbm('^HCSQ:"LTE",0,97,255,35')
        self.assertEqual(sig['rssi'], -120)     # below floor
        self.assertEqual(sig['rsrp'], -45)      # above ceiling, clamped
        self.assertIsNone(sig['sinr'])          # unknown
        self.assertEqual(sig['rsrq'], -3.0)     # above ceiling, clamped

    def test_hcsq_wcdma_gsm_noservice_garbage(self):
        self.assertEqual(self.mod.hcsq_to_dbm('^HCSQ:"WCDMA",50,40,30'),
                         {'mode': 'WCDMA', 'rssi': -71, 'rscp': -81, 'ecio': -17.5})
        self.assertEqual(self.mod.hcsq_to_dbm('^HCSQ:"GSM",50'), {'mode': 'GSM', 'rssi': -71})
        self.assertEqual(self.mod.hcsq_to_dbm('^HCSQ:"NOSERVICE"'), {'mode': 'NOSERVICE'})
        self.assertEqual(self.mod.hcsq_to_dbm('garbage'), {})
        self.assertEqual(self.mod.hcsq_to_dbm('^HCSQ:"LTE",x,y'), {})

    def test_fmt_signal(self):
        s = self.mod.fmt_signal({'mode': 'LTE', 'rssi': -82, 'rsrp': -114, 'sinr': -3.0, 'rsrq': -11.0}, 15)
        self.assertEqual(s, 'signal: LTE rssi=-82dBm rsrp=-114dBm sinr=-3dB rsrq=-11dB csq=15')
        self.assertEqual(self.mod.fmt_signal({'mode': 'NOSERVICE'}), 'signal: no service')
        self.assertEqual(self.mod.fmt_signal({}), 'signal: unknown')

    def test_parse_cme(self):
        self.assertEqual(self.mod.parse_cme('+CME ERROR: 10'), 10)
        self.assertIsNone(self.mod.parse_cme('+CME ERROR: SIM not inserted'))
        self.assertIsNone(self.mod.parse_cme('ERROR'))

    def test_sim_tables(self):
        self.assertEqual(self.mod.CPIN_TEXT['READY'], 1000)
        self.assertEqual(self.mod.CPIN_TEXT['SIM PIN'], 11)
        self.assertEqual(self.mod.CPIN_TEXT['SIM PUK'], 12)
        self.assertIn(10, self.mod.CME_SIM_CODES)
        self.assertIn(16, self.mod.CME_SIM_CODES)
        self.assertEqual(self.mod.ACT_NAMES[7], 'LTE')
        self.assertEqual(self.mod.ACT_NAMES[3], 'EDGE')
        self.assertEqual(self.mod.ACT_NAMES[2], 'UMTS')

    def test_fmt_duration(self):
        self.assertEqual(self.mod.fmt_duration(37), '37s')
        self.assertEqual(self.mod.fmt_duration(252), '4m12s')
        self.assertEqual(self.mod.fmt_duration(4980), '1h23m')
        self.assertEqual(self.mod.fmt_duration(-5), '0s')

    def test_config_defaults_and_bounds(self):
        cfg = self.mod.Config(raw={})
        self.assertEqual(cfg.probe_hosts, ['8.8.8.8', '1.1.1.1'])
        self.assertEqual(cfg.recovery_level, 4)
        self.assertEqual(cfg.timescale, 1.0)
        cfg = self.mod.Config(raw={'PROBE_HOST': '', 'RECOVERY_LEVEL': '9',
                                   'RECOVERY_TIMESCALE': '0.01', 'APN': ''})
        self.assertEqual(cfg.probe_hosts, [])
        self.assertEqual(cfg.recovery_level, 4)
        self.assertEqual(cfg.timescale, 0.1)
        self.assertEqual(cfg.apn, 'mmsbouygtel.com')
        cfg = self.mod.Config(raw={'RECOVERY_LEVEL': 'x', 'PROBE_HOST': ' 8.8.8.8 , 9.9.9.9 '})
        self.assertEqual(cfg.recovery_level, 4)
        self.assertEqual(cfg.probe_hosts, ['8.8.8.8', '9.9.9.9'])


class AtFilterTests(unittest.TestCase):
    """Unsolicited ^XXX: lines never pollute a response."""

    def test_unsolicited_lines_are_dropped(self):
        b = harness.Bench()
        b.modem.on('AT+CGMM', lambda c: ['^RSSI:20', '^HCSQ:"LTE",1,2,3,4', 'E3372', 'OK'])
        b.modem.on('AT^NDISSTATQRY?', lambda c: ['^NDISSTAT:0,,,"IPV4"', '^NDISSTATQRY:1,,,"IPV4"', 'OK'])
        b.modem.ndis = 1
        b.start()
        self.assertEqual(b.dbus('/Model'), 'E3372')
        self.assertTrue(b.svc.ncm_connected)
        b.run(20)
        self.assertEqual(b.dbus('/Connected'), 1)

    def test_last_error_codes(self):
        b = harness.Bench()
        b.start()
        m = b.svc.modem
        b.modem.on('AT+XYZ', lambda c: ['+CME ERROR: 10'])
        self.assertIsNone(m.at('AT+XYZ'))
        self.assertEqual(m.last_error, 10)
        self.assertTrue(m.responded())
        b.modem.on('AT+ABC', lambda c: harness.fake_modem.NO_ANSWER)
        self.assertIsNone(m.at('AT+ABC', timeout=1))
        self.assertEqual(m.last_error, 'timeout')
        self.assertFalse(m.responded())
        self.assertEqual(m.at('AT'), [])
        self.assertIsNone(m.last_error)
        b.modem.died = True
        self.assertIsNone(m.at('AT'))
        self.assertEqual(m.last_error, 'nodev')


if __name__ == '__main__':
    unittest.main()
