"""TeleImager single-part JPEG input, bounded latest frames and same-host snapshots."""

import hashlib
import io
import time
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageStat

from .contracts import SOURCES, SnapshotOutput


@dataclass(frozen=True)
class Frame:
    jpeg: bytes
    sequence: int
    received: float
    brightness: float


class Cameras:
    def __init__(
        self, config: dict, settings, snapshot_dir: Path, *, simulated=False, clock=time.monotonic
    ):
        self.config, self.settings = config, settings
        self.snapshot_dir = snapshot_dir.resolve()
        self.simulated, self.clock = simulated, clock
        self.frames = {}
        self.sequences = {source: 0 for source in SOURCES}
        self.errors = {}
        self.sockets = {}
        self.context = None
        self._synthetic = {}

    def open(self):
        if self.simulated:
            return
        import zmq

        self.context = zmq.Context()
        try:
            for source in SOURCES:
                socket = self.context.socket(zmq.SUB)
                self.sockets[source] = socket
                socket.setsockopt(zmq.SUBSCRIBE, b"")
                socket.setsockopt(zmq.CONFLATE, 1)
                socket.setsockopt(zmq.MAXMSGSIZE, self.settings.max_jpeg_bytes)
                socket.setsockopt(zmq.LINGER, 0)
                socket.connect(self.config[source]["stream_url"])
        except BaseException:
            self.close()
            raise

    def ingest(self, source: str, jpeg: bytes):
        if source not in SOURCES or not 0 < len(jpeg) <= self.settings.max_jpeg_bytes:
            raise ValueError("invalid camera source or JPEG size")
        expected = self.config[source]
        with Image.open(io.BytesIO(jpeg)) as image:
            if image.format != "JPEG" or image.size != (expected["width"], expected["height"]):
                raise ValueError("camera JPEG format or dimensions do not match configuration")
            image.load()
            brightness = ImageStat.Stat(image.convert("L")).mean[0]
        self.sequences[source] += 1
        self.frames[source] = Frame(jpeg, self.sequences[source], self.clock(), brightness)
        self.errors.pop(source, None)

    def poll(self) -> list[str]:
        """Receive and decode in one synchronous call.

        Kept for tests and one-shot callers. The node uses `drain` plus `ingest_all`
        so the decode never runs on the event loop it shares with Action control.
        """
        return self.ingest_all(self.drain())

    def drain(self) -> list[tuple[str, bytes]]:
        """Receive raw JPEG payloads without decoding them.

        Socket reads (and, in the mock runtime, one cached synthetic frame per
        source) are cheap enough for the event loop. Decoding is not: three 30 fps
        streams cost milliseconds per tick on the same loop that runs the height and
        arm control loops, so `ingest_all` is meant to be called in a worker thread.
        A receive failure is recorded here and still drops the preceding frame, so a
        corrupt replacement can never leave the older image usable.
        """
        if self.simulated:
            received = []
            for source in SOURCES:
                old = self.frames.get(source)
                if old and self.clock() - old.received < 0.1:
                    continue
                try:
                    received.append((source, self.synthetic_jpeg(source)))
                except Exception as error:
                    self.errors[source] = str(error)
                    self.frames.pop(source, None)
            return received

        import zmq

        received = []
        for source in SOURCES:
            try:
                parts = self.sockets[source].recv_multipart(flags=zmq.NOBLOCK)
            except zmq.Again:
                continue
            except Exception as error:
                self.errors[source] = str(error)
                self.frames.pop(source, None)
                continue
            if len(parts) != 1:
                self.errors[source] = "expected a single-part TeleImager JPEG"
                self.frames.pop(source, None)
                continue
            received.append((source, parts[0]))
        return received

    def ingest_all(self, payloads) -> list[str]:
        """Validate and decode received payloads; safe to run off the event loop.

        While this runs in a worker the loop may serve `frame()` from a Query or an
        Action. That is safe without a lock: `Frame` is frozen, and every mutation
        here is a single dict assignment or pop, so a concurrent reader sees either
        the previous frame or the replacement, never a partially updated one.
        """
        changed = []
        for source, jpeg in payloads:
            try:
                self.ingest(source, jpeg)
                changed.append(source)
            except Exception as error:
                self.errors[source] = str(error)
                # Never let a corrupt replacement leave the preceding frame usable.
                self.frames.pop(source, None)
        return changed

    def synthetic_jpeg(self, source: str) -> bytes:
        """Build the labelled mock frame once per source and reuse it.

        PIL encoding costs more than decoding, and the mock image is a constant, so
        caching it keeps the simulated runtime off the event loop as well. Freshness
        is carried by the receive time and sequence, not by new pixels.
        """
        cached = self._synthetic.get(source)
        if cached is None:
            cfg = self.config[source]
            image = Image.new("RGB", (cfg["width"], cfg["height"]), (80, 120, 150))
            ImageDraw.Draw(image).text((30, 30), f"SIMULATION / {source}", fill="white")
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG")
            cached = self._synthetic[source] = buffer.getvalue()
        return cached

    def frame(self, source: str, max_age_s: float) -> Frame:
        frame = self.frames.get(source)
        if frame is None or not 0 <= self.clock() - frame.received <= min(
            max_age_s, self.settings.camera_max_age_s
        ):
            raise ValueError(f"camera {source} has no fresh frame")
        if frame.brightness < self.settings.min_brightness:
            raise ValueError(
                f"camera {source} is too dark for the configured observation threshold"
            )
        return frame

    def snapshot(self, source: str, max_age_s: float) -> dict:
        frame = self.frame(source, max_age_s)
        return self.snapshot_frame(source, frame)

    def snapshot_frame(self, source: str, frame: Frame) -> dict:
        """Persist the exact captured frame, even if the latest-frame cache has advanced."""
        digest = hashlib.sha256(frame.jpeg).hexdigest()
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        # Unique immutable content paths preserve earlier Query references.
        path = self.snapshot_dir / f"{source}-{digest}.jpg"
        try:
            with path.open("xb") as stream:
                stream.write(frame.jpeg)
        except FileExistsError:
            if path.read_bytes() != frame.jpeg:
                raise ValueError("snapshot content does not match its digest") from None
        cfg = self.config[source]
        return SnapshotOutput(
            source=source,
            image_path=str(path),
            sha256=digest,
            sequence=frame.sequence,
            age_ms=(self.clock() - frame.received) * 1000,
            width=cfg["width"],
            height=cfg["height"],
            binocular=cfg["binocular"],
            mean_brightness=frame.brightness,
            simulated=self.simulated,
        ).model_dump()

    def close(self):
        for socket in self.sockets.values():
            socket.close(linger=0)
        self.sockets.clear()
        if self.context is not None:
            self.context.term()
            self.context = None
