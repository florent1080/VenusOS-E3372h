#!/usr/bin/python3 -u
"""Send AT commands on /dev/cdc-wdm0 and print the answers.

Part of the VenusOS-E3372h package. This is the safe way to query the modem
while the service owns its serial port: the two channels are independent.

The cdc_wdm driver needs the node opened read/write and the answer read back:
a shell redirection (printf > /dev/cdc-wdm0) silently does nothing.
Usage: at_wdm.py 'AT+CREG?' 'AT+COPS=0' ...
"""
import os
import select
import sys
import time

DEV = '/dev/cdc-wdm0'


def main():
    if len(sys.argv) < 2:
        print('usage: at_wdm.py CMD [CMD ...]')
        return 1
    try:
        fd = os.open(DEV, os.O_RDWR | os.O_NONBLOCK)
    except OSError as e:
        print('cannot open %s: %s' % (DEV, e))
        return 1
    rc = 0
    try:
        for cmd in sys.argv[1:]:
            try:
                os.write(fd, (cmd + '\r').encode())
            except OSError as e:
                print('%s -> write error: %s' % (cmd, e))
                rc = 1
                continue
            buf = b''
            end = time.time() + 5
            while time.time() < end:
                r, _, _ = select.select([fd], [], [], 0.5)
                if not r:
                    continue
                try:
                    buf += os.read(fd, 4096)
                except BlockingIOError:
                    continue
                except OSError as e:
                    buf += ('<read error: %s>' % e).encode()
                    break
                if b'OK' in buf or b'ERROR' in buf:
                    break
            answer = buf.decode(errors='replace').strip().replace('\r\n', ' | ')
            print('%s %s -> %s' % (time.strftime('%H:%M:%S'), cmd, answer or '(no answer)'))
            if not answer:
                rc = 1
    finally:
        os.close(fd)
    return rc


if __name__ == '__main__':
    sys.exit(main())
