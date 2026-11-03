"""Optional separate PNG receiver: one newest frame, finite age/time/byte budgets."""

from __future__ import annotations

import struct
import threading
import time
import uuid

from iatest.mobile.protocol import ProtocolError


class MediaReceiver:
    def __init__(self, channel, *, duration=120, byte_limit=67108864, max_age=1):
        self.channel = channel
        self.id = uuid.uuid4().hex
        self.started = time.monotonic()
        self.duration, self.byte_limit, self.max_age = duration, byte_limit, max_age
        self.stop = threading.Event()
        self.condition = threading.Condition()
        self.latest, self.error, self.total, self.frames = None, "", 0, 0
        self.thread = threading.Thread(
            target=self.receive, name="iatest-mobile-media", daemon=True
        )
        self.thread.start()

    def receive(self):
        try:
            while not self.stop.is_set():
                began = time.monotonic()
                if began - self.started >= self.duration:
                    raise ProtocolError("Media session duration budget exhausted")
                value = self.channel.request("screenshot", {}, timeout=2)
                data = value.pop("binary", b"")
                if not data.startswith(b"\x89PNG\r\n\x1a\n") or len(data) < 24:
                    raise ProtocolError(
                        "Media receiver accepts bounded PNG frames only"
                    )
                width, height = struct.unpack(">II", data[16:24])
                if (
                    not 0 < width <= 1280
                    or not 0 < height <= 1280
                    or len(data) > 8388608
                    or (width, height) != (value.get("width"), value.get("height"))
                ):
                    raise ProtocolError("Media frame geometry/byte bounds invalid")
                self.total += len(data)
                if self.total > self.byte_limit:
                    raise ProtocolError("Media session byte budget exhausted")
                now = time.monotonic()
                # Includes request/encoding time, rather than pretending receipt time is capture time.
                if now - began > self.max_age:
                    raise ProtocolError("Media frame became stale in transport")
                self.frames += 1
                with self.condition:
                    self.latest = (
                        began,
                        data,
                        {
                            **value,
                            "receiver_id": self.id,
                            "received_frames": self.frames,
                            "received_bytes": self.total,
                            "transport": "separate-png",
                            "continuous_video": False,
                        },
                    )
                    self.condition.notify_all()
                self.stop.wait(max(0, 0.5 - (time.monotonic() - began)))
        except (OSError, EOFError, RuntimeError) as exc:
            self.error = type(exc).__name__ + ": " + str(exc)
        finally:
            self.stop.set()
            self.channel.close()
            with self.condition:
                self.condition.notify_all()

    def frame(self, timeout=3):
        deadline = time.monotonic() + timeout
        with self.condition:
            while (
                self.latest is None
                and not self.stop.is_set()
                and time.monotonic() < deadline
            ):
                self.condition.wait(min(0.1, max(0, deadline - time.monotonic())))
            if (
                self.latest is None
                or self.stop.is_set()
                or time.monotonic() - self.latest[0] > self.max_age
            ):
                raise ProtocolError(self.error or "No fresh media frame")
            _, data, metadata = self.latest
            return {**metadata, "binary": data}

    def close(self):
        self.stop.set()
        self.channel.close()
        self.thread.join(2.5)
