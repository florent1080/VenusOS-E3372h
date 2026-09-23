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
        b.system.watchdog_pids.add(4242)
        b.start()
        self.assertNotIn([HELPER], b.system.spawned)
        self.assertTrue(b.logs('linkwatch already running (pid 4242)'))

    def test_started_when_the_pid_now_belongs_to_something_else(self):
        # pids are reused: a live pid in a stale pidfile is not proof
        b = self._bench()
        b.system.files[PIDFILE] = '4242'
        b.system.live_pids.add(4242)            # alive, but not a watchdog
        b.start()
        self.assertIn([HELPER], b.system.spawned)

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


class IntentRepairTests(unittest.TestCase):
    """A USB action interrupted half-way must be undone by whoever comes next.

    The helper disables the modem's port, then re-enables it five seconds
    later. Killed in between, it would leave the only remote link down with
    nothing on the machine able to bring it back.
    """

    INTENT = '/run/e3372-intent.json'
    PORT = '/sys/bus/usb/devices/usb3/3-0:1.0/usb3-port1'
    PEER = '/sys/bus/usb/devices/usb4/4-0:1.0/usb4-port1'

    def _bench(self):
        b = harness.Bench()
        b.system.files[HELPER] = '#!/bin/sh'
        return b

    def _intent(self, b, boot='boot-1'):
        import json
        b.system.files[self.INTENT] = json.dumps({
            'boot_id': boot, 'action': 'portcycle', 'target': self.PORT,
            'peer': self.PEER, 'undo_file': 'disable', 'undo_value': '0',
            'undo_at': 1})

    def test_unfinished_port_cycle_is_repaired_at_startup(self):
        b = self._bench()
        b.system.files[self.PORT + '/disable'] = '1'    # the helper died here
        b.system.files[self.PEER + '/disable'] = '1'
        self._intent(b)
        b.start()
        self.assertEqual(b.system.files[self.PORT + '/disable'].strip(), '0')
        self.assertEqual(b.system.files[self.PEER + '/disable'].strip(), '0')
        self.assertTrue(b.logs('a USB action was left unfinished'))
        self.assertNotIn(self.INTENT, b.system.files)

    def test_intent_from_another_boot_is_dropped_not_replayed(self):
        b = self._bench()
        b.system.files[self.PORT + '/disable'] = '0'
        self._intent(b, boot='some-other-boot')
        b.start()
        self.assertEqual(b.system.files[self.PORT + '/disable'].strip(), '0')
        self.assertNotIn(self.INTENT, b.system.files)
        self.assertEqual(b.logs('a USB action was left unfinished'), [])

    def test_corrupt_intent_is_discarded(self):
        b = self._bench()
        b.system.files[self.INTENT] = 'not json at all'
        self.assertTrue(b.start())
        self.assertNotIn(self.INTENT, b.system.files)


if __name__ == '__main__':
    unittest.main()
