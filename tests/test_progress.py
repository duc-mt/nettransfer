from app.core.formatting import human_size, human_speed
from app.core.progress import ProgressTracker


class FakeClock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def test_throttles_and_always_emits_final():
    clock, events = FakeClock(), []
    tr = ProgressTracker(1000, events.append, peer="10.0.0.2", min_interval=0.1, clock=clock)
    tr.update(100)  # first update always emits
    clock.t += 0.01
    tr.update(200)  # throttled
    clock.t += 0.2
    tr.update(500)  # emits
    clock.t += 0.01
    tr.update(1000)  # final: emitted despite throttle window
    assert [e.sent for e in events] == [100, 500, 1000]
    assert events[-1].peer == "10.0.0.2"


def test_speed_uses_sliding_window():
    clock, events = FakeClock(), []
    tr = ProgressTracker(10_000, events.append, min_interval=0, window=2.0, clock=clock)
    clock.t += 1.0
    tr.update(1000)
    assert round(events[-1].bps) == 1000
    clock.t += 1.0
    tr.update(3000)
    assert round(events[-1].bps) == 1500  # 3000 bytes over 2s


def test_finish_forces_full_event():
    events = []
    tr = ProgressTracker(500, events.append, min_interval=10)
    tr.update(10)
    tr.finish()
    assert events[-1].sent == 500 and events[-1].total == 500


def test_formatting():
    assert human_size(512) == "512 B"
    assert human_size(1536) == "1.5 KB"
    assert human_speed(2 * 1024 * 1024) == "2.0 MB/s"
