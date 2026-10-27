#!/usr/bin/python3 -u
"""Set the GX clock from NTP over the 4G link, then copy it into the RTC.

Part of the VenusOS-E3372h package. Run by the link watchdog
(/data/e3372_linkwatch.sh) as soon as the link is up, then every hour.

WHY THIS EXISTS
Venus OS sets its clock with connman's NTP client, and connman only runs it
while one of its own services is online (WiFi, Ethernet). wwan0 is brought up
by this package, outside connman, so on a GX whose only uplink is this modem
nothing ever sets the clock. The Pi 5 RTC has no battery either: on 2026-10-25
the GX came back from a power cut believing it was February 1970. Every TLS
connection failed (VRM over HTTPS, Tailscale), VRM lost contact, and the VRM
watchdog rebooted the GX every 35 minutes for five hours.

BusyBox on Venus has no ntpd, hence this small SNTP client (RFC 4330). It asks
up to three servers, keeps only well-formed answers that echo the nonce of our
request, takes the median offset so that one wrong server cannot move the
clock, steps the clock when it is off by more than STEP_S, and writes the RTC
so that a reboot (not a power cut) starts on the right time.

Usage: e3372_timesync.py [--dry-run] [server ...]
Servers default to TIMESYNC_SERVERS in /data/e3372-config.conf.
Exit status: 0 the clock is right or was set, 1 no usable answer.
"""
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import time

CONF = '/data/e3372-config.conf'
LOG = '/data/log/e3372/timesync.log'
STATE = '/run/e3372-timesync.json'
HWCLOCK = '/sbin/hwclock'
DEFAULT_SERVERS = ['pool.ntp.org', 'time.cloudflare.com', 'time.google.com']

NTP_EPOCH = 2208988800      # seconds from 1900-01-01 to 1970-01-01
NTP_PORT = 123
WANT = 3                    # answers to collect before deciding
AGREE_S = 2.0               # two answers further apart than this are not trusted
STEP_S = 0.5                # smaller offsets are left alone
QUERY_TIMEOUT = 2.0
BUDGET_S = 40               # the whole run, DNS included: getaddrinfo has no timeout
FLOOR = 1793059200          # 2026-10-27, this release: no right answer is older


class OutOfTime(Exception):
    pass


def _out_of_time(signum, frame):
    raise OutOfTime()


def ntp_to_unix(raw):
    """64-bit NTP timestamp to Unix seconds. Era 1 starts in February 2036."""
    secs, frac = raw >> 32, raw & 0xffffffff
    if secs < 0x80000000:
        secs += 1 << 32
    return secs - NTP_EPOCH + frac / 2.0 ** 32


def request(nonce):
    # LI 0, version 4, mode 3 (client). The transmit timestamp carries a random
    # nonce rather than our clock, which may be decades off: the server echoes
    # it back as the origin timestamp, and anything else is not our answer.
    return bytes([0x23]) + bytes(39) + struct.pack('!Q', nonce)


def parse_reply(data, nonce):
    """Return (receive, transmit) server times from an answer, or raise ValueError."""
    if len(data) < 48:
        raise ValueError('short answer (%d bytes)' % len(data))
    li, mode, stratum = data[0] >> 6, data[0] & 7, data[1]
    if mode != 4:
        raise ValueError('not a server answer (mode %d)' % mode)
    if li == 3 or not 1 <= stratum <= 15:
        # stratum 0 is a kiss-o'-death (rate limit, access denied)
        raise ValueError('server not synchronised (li %d, stratum %d)' % (li, stratum))
    origin, receive, transmit = struct.unpack('!QQQ', data[24:48])
    if origin != nonce:
        raise ValueError('answer does not echo our request')
    if not receive or not transmit:
        raise ValueError('answer without timestamps')
    return ntp_to_unix(receive), ntp_to_unix(transmit)


def query(addr, port=NTP_PORT, timeout=QUERY_TIMEOUT):
    """One exchange with one server. Return (offset, delay) in seconds."""
    nonce = struct.unpack('!Q', os.urandom(8))[0]
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.connect((addr, port))
        t1, m1 = time.time(), time.monotonic()
        s.send(request(nonce))
        data = s.recv(512)
        m4 = time.monotonic()
    finally:
        s.close()
    t2, t3 = parse_reply(data, nonce)
    t4 = t1 + (m4 - m1)     # timed on the monotonic clock: the wall clock may be stepped meanwhile
    return ((t2 - t1) + (t3 - t4)) / 2, (t4 - t1) - (t3 - t2)


def collect(servers, samples, errors):
    """Fill samples with (offset, delay, host), at most one per server.

    The lists are filled in place so that what was gathered survives the
    run running out of time.
    """
    for host in servers:
        try:
            infos = socket.getaddrinfo(host, NTP_PORT, socket.AF_INET, socket.SOCK_DGRAM)
        except OSError as e:
            errors.append('%s: %s' % (host, e))
            continue
        addrs = []
        for info in infos:
            if info[4][0] not in addrs:
                addrs.append(info[4][0])
        for addr in addrs[:2]:
            try:
                offset, delay = query(addr)
            except (OSError, ValueError) as e:
                errors.append('%s (%s): %s' % (host, addr, e))
                continue
            samples.append((offset, delay, host))
            break
        if len(samples) >= WANT:
            break


def decide(samples, now):
    """Return (offset, None) to apply, or (None, reason) to leave the clock alone."""
    if not samples:
        return None, None
    if len(samples) >= 3:
        offset = sorted(s[0] for s in samples)[len(samples) // 2]
    elif len(samples) == 2:
        a, b = samples
        if abs(a[0] - b[0]) > AGREE_S:
            return None, 'servers disagree: %s %+.3f s, %s %+.3f s' % (a[2], a[0], b[2], b[0])
        offset = min(samples, key=lambda s: s[1])[0]
    else:
        # A single answer still beats a clock in 1970.
        offset = samples[0][0]
    if now + offset < FLOOR:
        return None, 'answer is older than this package (%s)' % time.strftime(
            '%Y-%m-%d', time.gmtime(now + offset))
    return offset, None


def conf_servers(path=CONF):
    """TIMESYNC_SERVERS from the config, read as text: the file is never executed."""
    value = None
    try:
        with open(path) as f:
            for line in f:
                key, sep, v = line.partition('=')
                if sep and key.strip() == 'TIMESYNC_SERVERS':
                    value = v.strip().strip('"\'')
    except OSError:
        pass
    hosts = [h.strip() for h in (value or '').split(',') if h.strip()]
    return hosts or DEFAULT_SERVERS


def uptime():
    try:
        with open('/proc/uptime') as f:
            return int(float(f.read().split()[0]))
    except (OSError, ValueError, IndexError):
        return 0


def log(msg):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, 'a') as f:
            f.write('%s [%d] - %s\n' % (time.strftime('%a %b %d %H:%M:%S %Z %Y'), uptime(), msg))
    except OSError:
        pass


def read_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_state(**kw):
    kw.update(at=int(time.time()), uptime=uptime())
    try:
        with open(STATE + '.new', 'w') as f:
            json.dump(kw, f)
        os.replace(STATE + '.new', STATE)
    except OSError:
        pass


def step(offset):
    time.clock_settime(time.CLOCK_REALTIME, time.time() + offset)


def save_rtc():
    # --noadjfile: no write to /etc/adjtime, so the hourly run never touches the rootfs
    try:
        return subprocess.call([HWCLOCK, '--systohc', '--utc', '--noadjfile'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0
    except OSError:
        return False


def main(argv):
    dry = '--dry-run' in argv
    servers = [a for a in argv if not a.startswith('--')] or conf_servers()
    samples, errors = [], []
    budget = hasattr(signal, 'SIGALRM')     # always on the GX, absent on a Windows test bench
    if budget:
        signal.signal(signal.SIGALRM, _out_of_time)
        signal.alarm(BUDGET_S)
    try:
        collect(servers, samples, errors)
    except OutOfTime:
        errors.append('gave up after %d s' % BUDGET_S)
    finally:
        if budget:
            signal.alarm(0)

    offset, reason = decide(samples, time.time())
    prev = read_state()
    if offset is None:
        msg = reason or 'no usable NTP answer: ' + '; '.join(errors)
        if dry:
            print(msg)
            return 1
        # Log the first failure of a series only: a GX without a link would
        # otherwise write a line every two minutes for as long as it lasts.
        if prev is None or prev.get('ok'):
            log(msg)
        write_state(ok=False, error=msg)
        return 1

    hosts = ', '.join(s[2] for s in samples)
    if dry:
        print('offset %+.3f s (%s)' % (offset, hosts))
        return 0
    stepped = abs(offset) >= STEP_S
    if stepped:
        step(offset)
    rtc = save_rtc()
    if stepped:
        log('clock stepped by %+.3f s (%s)' % (offset, hosts))
    elif prev is None or not prev.get('ok'):
        log('clock is right, offset %+.3f s (%s)' % (offset, hosts))
    if not rtc and (prev is None or prev.get('rtc', True)):
        log('could not write the RTC with %s' % HWCLOCK)
    write_state(ok=True, offset=round(offset, 3), stepped=stepped, rtc=rtc, servers=hosts)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
