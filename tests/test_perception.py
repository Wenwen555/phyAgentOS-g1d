"""Continuous-perception store: read-time ageing and the changed-since-last-read mark."""

from paos_g1d.observe import ObservedImage
from paos_g1d.perception.state_store import StateStore


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def frame(source="head", digest="a" * 64, received=0.0, sequence=1):
    return ObservedImage(
        source=source,
        image_path=f"/tmp/{source}-{digest}.jpg",
        sha256=digest,
        sequence=sequence,
        age_ms=0.0,
        width=640,
        height=480,
        binocular=False,
        mean_brightness=50.0,
        simulated=True,
        received_monotonic_s=received,
    )


def test_first_read_reports_every_frame_as_changed():
    store = StateStore()
    store.update([frame()], active=True, simulated=True)
    output = store.snapshot(1.0)
    assert [f.changed_since_last_read for f in output.frames] == [True]


def test_repeated_identical_content_is_reported_as_unchanged():
    clock = Clock()
    store = StateStore(clock=clock)
    store.update([frame(digest="a" * 64)], active=True, simulated=True)
    assert store.snapshot(1.0).frames[0].changed_since_last_read is True

    clock.now += 0.5
    store.update([frame(digest="a" * 64, received=0.5, sequence=2)], active=True, simulated=True)
    assert store.snapshot(1.0).frames[0].changed_since_last_read is False


def test_new_content_is_reported_as_changed_again():
    clock = Clock()
    store = StateStore(clock=clock)
    store.update([frame(digest="a" * 64)], active=True, simulated=True)
    store.snapshot(1.0)

    clock.now += 0.5
    store.update([frame(digest="b" * 64, received=0.5, sequence=2)], active=True, simulated=True)
    assert store.snapshot(1.0).frames[0].changed_since_last_read is True


def test_stale_read_returns_nothing_and_does_not_advance_the_watermark():
    clock = Clock()
    store = StateStore(clock=clock)
    store.update([frame(digest="a" * 64)], active=True, simulated=True)
    assert store.snapshot(1.0).frames[0].changed_since_last_read is True

    # The store moves on to new content, but the caller's window is too tight to see it.
    clock.now += 1.0
    store.update([frame(digest="b" * 64, received=1.0, sequence=2)], active=True, simulated=True)
    clock.now += 0.5
    assert store.snapshot(0.1) is None

    # Nothing was returned by the stale read, so the frame is still one the caller has
    # never seen: the watermark must not have moved to the unread digest.
    assert store.snapshot(2.0).frames[0].changed_since_last_read is True


def test_age_is_rederived_at_read_time():
    clock = Clock()
    store = StateStore(clock=clock)
    store.update([frame(received=0.0)], active=True, simulated=True)
    clock.now += 0.25
    output = store.snapshot(1.0)
    assert output.frames[0].age_ms == 250.0
    assert output.store_age_ms == 250.0


def test_a_source_that_appears_later_is_changed():
    clock = Clock()
    store = StateStore(clock=clock)
    store.update([frame(source="head", digest="a" * 64)], active=True, simulated=True)
    store.snapshot(1.0)

    clock.now += 0.5
    store.update(
        [
            frame(source="head", digest="a" * 64, received=0.5, sequence=2),
            frame(source="left_wrist", digest="c" * 64, received=0.5, sequence=1),
        ],
        active=True,
        simulated=True,
    )
    marks = {f.source: f.changed_since_last_read for f in store.snapshot(1.0).frames}
    assert marks == {"head": False, "left_wrist": True}
