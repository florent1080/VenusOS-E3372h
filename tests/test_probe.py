import unittest

import harness


class ProbeTests(unittest.TestCase):

    def _up_bench(self, **kw):
        b = harness.Bench(**kw)
        b.modem.ndis = 1
        b.system.ip = '10.0.0.2'
        b.start()
        return b

    def test_any_host_answering_is_enough(self):
        b = self._up_bench()
        b.system.ping_results = lambda host: host == '1.1.1.1'
        b.run(15 * 60)
        self.assertEqual(b.svc.probe_failures, 0)
        self.assertEqual(b.modem.sent_cmds('AT^NDISDUP=1,1'), [])
        hosts = [c[2] for c in b.system.pings()]
        self.assertIn('8.8.8.8', hosts)
        self.assertIn('1.1.1.1', hosts)

    def test_probe_failures_redial_then_reset_then_suspend(self):
        b = self._up_bench()
        b.system.ping_results = False
        b.run(90 * 60)
        self.assertTrue(b.logs('connectivity probe failed (3/3)'))
        self.assertTrue(b.logs('no connectivity after 3 probes - re-dialling'))
        self.assertGreaterEqual(len(b.modem.sent_cmds('AT^NDISDUP=1,1')), 3)
        self.assertTrue(b.logs('recovery: [traffic] stuck (session up but no traffic)'))
        # a full 30 min grace before anything heavy: filtered ICMP on a
        # healthy link must not cost the modem a restart.
        stuck = [m for m in b.logs('[traffic] stuck')]
        self.assertTrue(stuck)
        self.assertEqual([c[2] for c in b.heavy()], ['AT^RESET'])
        self.assertTrue(b.logs('suspending the probe for 6h00m'))
        t_suspend = b.now
        n_pings = len(b.system.pings())
        b.run(5 * 3600)
        self.assertEqual(len(b.system.pings()), n_pings)     # silent meanwhile
        b.run(2 * 3600)
        self.assertGreater(len(b.system.pings()), n_pings)   # resumed after 6 h
        self.assertGreater(b.svc.probe_suspended_until, t_suspend)

    def test_received_bytes_override_a_failed_ping(self):
        """An APN that filters ICMP must never cost the modem a restart."""
        b = self._up_bench()
        b.system.ping_results = False
        for i in range(40):
            b.run(60)
            b.system.rx_bytes += 5000      # Tailscale keepalives keep coming
        self.assertEqual(b.heavy(), [])
        self.assertFalse(b.svc.probe_exhausted)
        self.assertEqual(b.modem.firmware_resets, 0)
        self.assertTrue(b.logs('the link is alive'))

    def test_traffic_restored_by_the_modem_reset(self):
        b = self._up_bench()
        b.system.ping_results = False
        b.modem.on('AT^RESET', lambda c: (setattr(b.system, 'ping_results', True),
                                          b.modem.reset(), ['OK'])[2])
        b.run(90 * 60)
        self.assertEqual([c[2] for c in b.heavy()], ['AT^RESET'])
        self.assertTrue(b.logs('recovered at rung 1/1 MODEM_RESET'), b.logs('recovery:'))
        self.assertEqual(b.svc.probe_suspended_until, 0.0)
        self.assertFalse(b.svc.probe_exhausted)

    def test_probe_disabled_when_no_host(self):
        b = self._up_bench(probe_hosts='')
        b.system.ping_results = False
        b.run(60 * 60)
        self.assertEqual(b.system.pings(), [])
        self.assertEqual(b.modem.sent_cmds('AT^NDISDUP=1,1'), [])
        self.assertEqual(b.heavy_cmds(), [])


if __name__ == '__main__':
    unittest.main()
