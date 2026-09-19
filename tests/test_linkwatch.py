import unittest

import harness

HELPER = '/data/e3372_linkwatch.sh'
PIDFILE = '/var/run/e3372-linkwatch.pid'


class LinkwatchTests(unittest.TestCase):
    """The last-resort watchdog is started detached, exactly once."""

    def _bench(self, **kw):
        b = harness.Bench(**kw)
        b.system.files[HELPER] = '#!/bin/sh\n'
        return b

    def test_started_detached_at_startup(self):
        b = self._bench()
        b.start()
        self.assertIn([HELPER], b.system.spawned)
        self.assertTrue(b.logs('linkwatch started'))

    def test_not_started_twice(self):
        b = self._bench()
        b.system.files[PIDFILE] = '4242'
        b.system.live_pids.add(4242)
        b.start()
        self.assertNotIn([HELPER], b.system.spawned)
        self.assertTrue(b.logs('linkwatch already running (pid 4242)'))

    def test_restarted_when_the_recorded_process_is_gone(self):
        b = self._bench()
        b.system.files[PIDFILE] = '4242'      # stale pidfile, no such process
        b.start()
        self.assertIn([HELPER], b.system.spawned)

    def test_disabled_by_config(self):
        b = self._bench(linkwatch='0')
        b.start()
        self.assertEqual(b.system.spawned, [])
        self.assertEqual(b.logs('linkwatch'), [])

    def test_missing_helper_is_reported_not_fatal(self):
        b = harness.Bench()          # helper absent from the fake filesystem
        self.assertTrue(b.start())
        self.assertTrue(b.logs('no last-resort link watchdog'))
        self.assertEqual(b.system.spawned, [])


if __name__ == '__main__':
    unittest.main()
