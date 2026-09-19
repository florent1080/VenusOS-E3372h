"""Virtual clock. Replaces the `time` module of the service: sleep() advances
the clock instead of waiting, and scheduled events fire as time passes."""


class FakeClock:
    def __init__(self, start=1000.0):
        self.t = float(start)
        self.scheduled = []

    # time module interface
    def monotonic(self):
        return self.t

    def time(self):
        return self.t + 1700000000.0

    def sleep(self, seconds):
        self.advance(seconds)

    # bench interface
    def at(self, delay, fn):
        self.scheduled.append((self.t + delay, fn))

    def advance(self, seconds):
        target = self.t + seconds
        while True:
            due = [ev for ev in self.scheduled if ev[0] <= target]
            if not due:
                break
            due.sort(key=lambda ev: ev[0])
            ev = due[0]
            self.scheduled.remove(ev)
            self.t = max(self.t, ev[0])
            ev[1]()
        self.t = target
