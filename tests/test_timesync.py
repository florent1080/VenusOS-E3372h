import contextlib
import importlib.util
import io
import os
import socket
import struct
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
HELPER = os.path.join(os.path.dirname(HERE), 'e3372_timesync.py')


def load():
    spec = importlib.util.spec_from_file_location('e3372_timesync', HELPER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def ntp_ts(unix):
    secs = unix + 2208988800
    return (int(secs) & 0xffffffff) << 32 | int((secs - int(secs)) * 2 ** 32)


def answer(origin, receive, transmit, first=0x24, stratum=2):
    # 0x24 = LI 0, version 4, mode 4 (server)
    return (bytes([first, stratum]) + bytes(22)
            + struct.pack('!QQQ', origin, ntp_ts(receive), ntp_ts(transmit)))


class FakeServer:
    """A local NTP server whose clock runs `skew` seconds ahead of ours."""

    def __init__(self, skew, mangle=None):
        self.skew, self.mangle = skew, mangle
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(('127.0.0.1', 0))
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        data, peer = self.sock.recvfrom(512)
        now = time.time() + self.skew
        reply = answer(struct.unpack('!Q', data[40:48])[0], now, now)
        if self.mangle:
            reply = self.mangle(reply)
        self.sock.sendto(reply, peer)

    def close(self):
        self.sock.close()


class PacketTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load()

    def test_request_carries_the_nonce_not_our_clock(self):
        req = self.mod.request(0x0123456789abcdef)
        self.assertEqual(len(req), 48)
        self.assertEqual(req[0], 0x23)                  # version 4, client
        self.assertEqual(req[40:48], bytes.fromhex('0123456789abcdef'))
        self.assertEqual(req[1:40], bytes(39))

    def test_good_answer_is_decoded(self):
        t2, t3 = self.mod.parse_reply(answer(42, 1793100000.25, 1793100000.5), 42)
        self.assertAlmostEqual(t2, 1793100000.25, places=6)
        self.assertAlmostEqual(t3, 1793100000.5, places=6)

    def test_answer_to_someone_else_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'echo'):
            self.mod.parse_reply(answer(41, 1793100000, 1793100000), 42)

    def test_kiss_of_death_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'stratum 0'):
            self.mod.parse_reply(answer(42, 1793100000, 1793100000, stratum=0), 42)

    def test_unsynchronised_server_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'li 3'):
            self.mod.parse_reply(answer(42, 1793100000, 1793100000, first=0xe4), 42)

    def test_client_packet_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'mode 3'):
            self.mod.parse_reply(answer(42, 1793100000, 1793100000, first=0x23), 42)

    def test_short_answer_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'short'):
            self.mod.parse_reply(bytes(20), 42)

    def test_era_1_after_2036(self):
        # 2040-01-01 wraps the 32-bit NTP seconds
        self.assertAlmostEqual(self.mod.ntp_to_unix(ntp_ts(2208988800)), 2208988800, places=3)


class ExchangeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load()

    def test_offset_of_a_clock_one_hour_ahead(self):
        srv = FakeServer(3600)
        try:
            offset, delay = self.mod.query('127.0.0.1', port=srv.port)
        finally:
            srv.close()
        self.assertAlmostEqual(offset, 3600, delta=0.5)
        self.assertGreaterEqual(delay, 0)

    def test_offset_from_1970(self):
        # what the GX saw on 2026-10-25: our clock is 56 years behind
        srv = FakeServer(1793059200 - 2854784)
        try:
            offset, _ = self.mod.query('127.0.0.1', port=srv.port)
        finally:
            srv.close()
        self.assertAlmostEqual(offset, 1793059200 - 2854784, delta=0.5)

    def test_answer_with_a_wrong_nonce_is_refused(self):
        def wrong(reply):
            return reply[:24] + struct.pack('!Q', 7) + reply[32:]
        srv = FakeServer(0, mangle=wrong)
        try:
            with self.assertRaisesRegex(ValueError, 'echo'):
                self.mod.query('127.0.0.1', port=srv.port)
        finally:
            srv.close()

    def test_silent_server_times_out(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(('127.0.0.1', 0))
        try:
            with self.assertRaises(OSError):
                self.mod.query('127.0.0.1', port=s.getsockname()[1], timeout=0.3)
        finally:
            s.close()


class DecisionTests(unittest.TestCase):
    NOW = 1793100000

    @classmethod
    def setUpClass(cls):
        cls.mod = load()

    def test_median_of_three_ignores_one_wrong_server(self):
        samples = [(0.2, 0.05, 'a'), (86400.0, 0.04, 'b'), (0.3, 0.06, 'c')]
        self.assertEqual(self.mod.decide(samples, self.NOW), (0.3, None))

    def test_two_that_agree_keep_the_shortest_round_trip(self):
        samples = [(0.9, 0.30, 'a'), (0.4, 0.05, 'b')]
        self.assertEqual(self.mod.decide(samples, self.NOW), (0.4, None))

    def test_two_that_disagree_change_nothing(self):
        offset, why = self.mod.decide([(0.1, 0.05, 'a'), (3600.0, 0.05, 'b')], self.NOW)
        self.assertIsNone(offset)
        self.assertIn('disagree', why)

    def test_a_single_answer_is_used(self):
        self.assertEqual(self.mod.decide([(12.0, 0.1, 'a')], self.NOW), (12.0, None))

    def test_no_answer(self):
        self.assertEqual(self.mod.decide([], self.NOW), (None, None))

    def test_never_set_a_date_older_than_the_package(self):
        offset, why = self.mod.decide([(-10 * 86400.0, 0.1, 'a')], self.NOW)
        self.assertIsNone(offset)
        self.assertIn('older than this package', why)


class ConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load()

    def _conf(self, text):
        fd, path = tempfile.mkstemp()
        with os.fdopen(fd, 'w', newline='') as f:
            f.write(text)
        self.addCleanup(os.remove, path)
        return path

    def test_servers_are_read(self):
        path = self._conf('APN=x\nTIMESYNC_SERVERS=a.example, b.example\n')
        self.assertEqual(self.mod.conf_servers(path), ['a.example', 'b.example'])

    def test_crlf_and_quotes(self):
        path = self._conf('TIMESYNC_SERVERS="a.example,b.example"\r\n')
        self.assertEqual(self.mod.conf_servers(path), ['a.example', 'b.example'])

    def test_last_line_wins(self):
        path = self._conf('TIMESYNC_SERVERS=old.example\nTIMESYNC_SERVERS=new.example\n')
        self.assertEqual(self.mod.conf_servers(path), ['new.example'])

    def test_empty_or_missing_falls_back(self):
        self.assertEqual(self.mod.conf_servers(self._conf('TIMESYNC_SERVERS=\n')),
                         self.mod.DEFAULT_SERVERS)
        self.assertEqual(self.mod.conf_servers('/nonexistent/e3372-config.conf'),
                         self.mod.DEFAULT_SERVERS)

    def test_the_config_is_never_executed(self):
        path = self._conf('TIMESYNC_SERVERS=$(touch /tmp/pwned)\n')
        self.assertEqual(self.mod.conf_servers(path), ['$(touch /tmp/pwned)'])


class RunTests(unittest.TestCase):
    """main(): what is stepped, written and logged, with the network faked."""

    def setUp(self):
        self.mod = load()
        tmp = tempfile.mkdtemp()
        self.mod.LOG = os.path.join(tmp, 'timesync.log')
        self.mod.STATE = os.path.join(tmp, 'state.json')
        self.steps, self.rtc = [], []
        self.mod.step = self.steps.append
        self.mod.save_rtc = lambda: self.rtc.append(True) or True
        self.offsets = []

        def collect(servers, samples, errors):
            if self.offsets:
                samples.extend((o, 0.05, 'srv%d' % i) for i, o in enumerate(self.offsets))
            else:
                errors.append('pool.ntp.org: Network is unreachable')
        self.mod.collect = collect

    def _log(self):
        try:
            with open(self.mod.LOG) as f:
                return f.read().splitlines()
        except OSError:
            return []

    def test_a_clock_in_1970_is_stepped_and_saved_to_the_rtc(self):
        self.offsets = [1.79e9, 1.79e9 + 0.01, 1.79e9 - 0.01]
        self.assertEqual(self.mod.main([]), 0)
        self.assertEqual(self.steps, [1.79e9])
        self.assertEqual(self.rtc, [True])
        self.assertIn('clock stepped', self._log()[-1])

    def test_a_small_offset_is_left_alone(self):
        self.offsets = [0.1, 0.12, 0.08]
        self.assertEqual(self.mod.main([]), 0)
        self.assertEqual(self.steps, [])
        self.assertEqual(self.rtc, [True])
        self.assertIn('clock is right', self._log()[-1])
        self.mod.main([])
        self.assertEqual(len(self._log()), 1)      # quiet while all is well

    def test_failures_are_logged_once_per_series(self):
        self.assertEqual(self.mod.main([]), 1)
        self.assertEqual(self.mod.main([]), 1)
        self.assertEqual(len(self._log()), 1)
        self.assertIn('Network is unreachable', self._log()[0])
        self.assertEqual(self.steps, [])
        self.assertEqual(self.rtc, [])

    def test_dry_run_changes_nothing(self):
        self.offsets = [3600.0, 3600.0, 3600.0]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.mod.main(['--dry-run']), 0)
        self.assertIn('offset +3600.000 s', out.getvalue())
        self.assertEqual(self.steps, [])
        self.assertEqual(self.rtc, [])
        self.assertEqual(self._log(), [])


if __name__ == '__main__':
    unittest.main()
