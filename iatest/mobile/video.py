"""Bounded H264 access units, real host decoding and newest decoded frame only."""

from __future__ import annotations

import os
import queue
import shutil
import struct
import subprocess
import threading
import time
import uuid
from collections import deque

from iatest.mobile.protocol import ProtocolError


class EncodedPacketSource:
    """Pull complete access units directly to one browser decoder; no host pixel decode."""

    def __init__(self, channel, *, duration=120, byte_limit=67108864):
        self.channel = channel
        self.id = uuid.uuid4().hex
        self.stop = threading.Event()
        self.started = time.monotonic()
        self.total = 0
        self.generation = None
        self.duration, self.byte_limit = duration, byte_limit

    def frame(self, request_keyframe=False):
        try:
            return self._frame(request_keyframe)
        except BaseException:
            self.close()
            raise

    def _frame(self, request_keyframe=False):
        if self.stop.is_set() or time.monotonic() - self.started > self.duration:
            raise ProtocolError("Encoded preview stopped or duration budget exhausted")
        for attempt in range(3):
            value = self.channel.request(
                "video_next",
                {
                    "request_keyframe": request_keyframe
                    or self.generation is None
                    or attempt > 0
                },
                timeout=3,
            )
            data = value.get("binary", b"")
            idle = value.get("idle") is True
            self.total += len(data)
            if (
                value.get("codec") != "h264-annexb"
                or value.get("no_b_frames") is not True
                or self.total > self.byte_limit
                or (idle and (data or value.get("key_frame") is not False))
                or (
                    not idle
                    and (
                        not 0 < len(data) <= 2097152
                        or not data.startswith((b"\x00\x00\x01", b"\x00\x00\x00\x01"))
                    )
                )
            ):
                raise ProtocolError("Invalid bounded direct H264 access unit")
            if any(
                type(value.get(k)) is not int or not 0 < value[k] <= 1280
                for k in ("width", "height")
            ) or not isinstance(value.get("generation"), str):
                raise ProtocolError("Invalid encoded frame geometry/generation")
            if (
                not idle
                and self.generation != value["generation"]
                and value.get("key_frame") is not True
            ):
                continue
            if not idle:
                self.generation = value["generation"]
            return {
                **value,
                "receiver_id": self.id,
                "transport": "h264-webcodecs",
                "received_bytes": self.total,
                "host_decode": False,
            }
        raise ProtocolError("Key frame recovery budget exhausted")

    def close(self):
        self.stop.set()
        self.channel.close()


class VideoDecoder:
    def __init__(self, callback, *, executable=None):
        executable = executable or shutil.which("ffmpeg")
        if not executable:
            raise ProtocolError(
                "Encoded preview requires an installed FFmpeg executable"
            )
        self.callback = callback
        self.stop = threading.Event()
        self.error = ""
        self.pending = deque(maxlen=32)
        self.lock = threading.Lock()
        self.queue = queue.Queue(maxsize=4)
        self.process = subprocess.Popen(
            [
                executable,
                "-hide_banner",
                "-loglevel",
                "error",
                "-probesize",
                "32",
                "-analyzeduration",
                "0",
                "-flags",
                "low_delay",
                "-f",
                "h264",
                "-i",
                "pipe:0",
                "-an",
                "-fps_mode",
                "passthrough",
                "-threads",
                "1",
                "-f",
                "image2pipe",
                "-vcodec",
                "png",
                "-flush_packets",
                "1",
                "pipe:1",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=os.name == "posix",
        )
        self.writer = threading.Thread(
            target=self.write, daemon=True, name="iatest-video-input"
        )
        self.reader = threading.Thread(
            target=self.read, daemon=True, name="iatest-video-output"
        )
        self.writer.start()
        self.reader.start()

    def feed(self, data, metadata):
        if self.error or self.stop.is_set() or self.process.poll() is not None:
            raise ProtocolError(self.error or "Video decoder exited")
        with self.lock:
            if len(self.pending) >= 32:
                raise ProtocolError(
                    "Decoder frame queue exhausted; reset at a key frame"
                )
            self.pending.append(metadata)
        try:
            self.queue.put_nowait(data)
        except queue.Full as exc:
            raise ProtocolError(
                "Decoder input queue exhausted; reset the complete codec"
            ) from exc

    def write(self):
        try:
            while not self.stop.is_set():
                try:
                    data = self.queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                if data is None:
                    self.process.stdin.close()
                    return
                self.process.stdin.write(data)
                self.process.stdin.flush()
        except (OSError, ValueError) as exc:
            if not self.stop.is_set():
                self.error = "Decoder input failed: " + type(exc).__name__

    def exact(self, size):
        data = bytearray()
        while len(data) < size:
            part = self.process.stdout.read(size - len(data))
            if not part:
                raise EOFError("Decoder output closed")
            data.extend(part)
        return bytes(data)

    def read(self):
        try:
            while not self.stop.is_set():
                data = bytearray(self.exact(8))
                if data != b"\x89PNG\r\n\x1a\n":
                    raise ProtocolError("Decoder returned an invalid PNG stream")
                width = height = None
                while True:
                    header = self.exact(8)
                    length, kind = struct.unpack(">I4s", header)
                    if length > 8388608 or len(data) + length + 12 > 8388608:
                        raise ProtocolError("Decoded frame exceeds 8 MiB")
                    payload = self.exact(length + 4)
                    data.extend(header + payload)
                    if kind == b"IHDR":
                        width, height = struct.unpack(">II", payload[:8])
                        if not 0 < width <= 1280 or not 0 < height <= 1280:
                            raise ProtocolError("Decoded geometry exceeds bounds")
                    if kind == b"IEND":
                        break
                with self.lock:
                    if not self.pending:
                        raise ProtocolError(
                            "Decoded frame has no correlated access unit"
                        )
                    metadata = self.pending.popleft()
                if (width, height) != (metadata["width"], metadata["height"]):
                    raise ProtocolError("Decoder geometry differs from source binding")
                self.callback(bytes(data), metadata)
        except (OSError, EOFError, ValueError, RuntimeError) as exc:
            if not self.stop.is_set():
                self.error = str(exc)

    def finish(self):
        self.queue.put(None, timeout=1)

    def close(self):
        self.stop.set()
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=1)
        self.writer.join(1)
        self.reader.join(1)
        for stream in (self.process.stdin, self.process.stdout):
            if stream:
                stream.close()


class EncodedMediaReceiver:
    def __init__(self, channel, *, duration=120, byte_limit=67108864, max_age=2):
        self.channel = channel
        self.id = uuid.uuid4().hex
        self.started = time.monotonic()
        self.duration, self.byte_limit, self.max_age = duration, byte_limit, max_age
        self.condition = threading.Condition()
        self.stop = threading.Event()
        self.latest, self.error, self.total, self.frames = None, "", 0, 0
        self.decoder, self.generation, self.restarts = None, None, 0
        self.decoder_epoch = 0
        self.source_checked = 0
        self.source_idle = False
        self.thread = threading.Thread(
            target=self.receive, daemon=True, name="iatest-h264-media"
        )
        self.thread.start()

    def decoded(self, data, metadata):
        with self.condition:
            if (
                metadata["generation"] != self.generation
                or metadata.get("decoder_epoch") != self.decoder_epoch
                or self.stop.is_set()
            ):
                return
            self.frames += 1
            self.latest = (
                metadata["began"],
                data,
                {
                    **{
                        k: v
                        for k, v in metadata.items()
                        if k not in {"began", "decoder_epoch"}
                    },
                    "receiver_id": self.id,
                    "received_frames": self.frames,
                    "received_bytes": self.total,
                    "transport": "h264-annexb",
                    "continuous_video": True,
                    "host_decode": True,
                    "decoder_restarts": self.restarts,
                },
            )
            self.condition.notify_all()

    def reset(self):
        with self.condition:
            self.decoder_epoch += 1
            self.latest = None
        if self.decoder:
            self.decoder.close()
        self.decoder = None
        with self.condition:
            self.latest = None

    def receive(self):
        request_key = True
        try:
            while not self.stop.is_set():
                began = time.monotonic()
                if began - self.started > self.duration:
                    raise ProtocolError("Video duration budget exhausted")
                value = self.channel.request(
                    "video_next", {"request_keyframe": request_key}, timeout=2
                )
                data = value.pop("binary", b"")
                idle = value.get("idle") is True
                if (
                    value.get("codec") != "h264-annexb"
                    or value.get("no_b_frames") is not True
                    or (idle and (data or value.get("key_frame") is not False))
                    or (
                        not idle
                        and (
                            not 0 < len(data) <= 2097152
                            or not data.startswith(
                                (b"\x00\x00\x01", b"\x00\x00\x00\x01")
                            )
                        )
                    )
                ):
                    raise ProtocolError("Invalid bounded H264 access unit")
                if (
                    not isinstance(value.get("generation"), str)
                    or len(value["generation"]) > 100
                    or any(
                        type(value.get(k)) is not int or not 0 < value[k] <= 1280
                        for k in ("width", "height")
                    )
                ):
                    raise ProtocolError("Invalid video generation/geometry")
                self.total += len(data)
                if self.total > self.byte_limit:
                    raise ProtocolError("Video byte budget exhausted")
                changed = self.generation != value["generation"]
                unhealthy = self.decoder and (
                    self.decoder.error
                    or (
                        self.latest
                        and began - max(self.latest[0], self.source_checked)
                        > self.max_age
                    )
                )
                if changed or unhealthy:
                    self.reset()
                    self.generation = value["generation"]
                    if unhealthy:
                        self.restarts += 1
                        if self.restarts > 3:
                            raise ProtocolError(
                                "Video decoder recovery budget exhausted"
                            )
                with self.condition:
                    self.source_checked = began
                    self.source_idle = idle
                    self.condition.notify_all()
                if idle:
                    request_key = self.decoder is None
                    self.stop.wait(0.01)
                    continue
                if self.decoder is None:
                    if value.get("key_frame") is not True:
                        request_key = True
                        continue
                    self.decoder = VideoDecoder(self.decoded)
                try:
                    self.decoder.feed(
                        data,
                        {**value, "began": began, "decoder_epoch": self.decoder_epoch},
                    )
                    request_key = False
                except ProtocolError:
                    self.reset()
                    self.restarts += 1
                    if self.restarts > 3:
                        raise
                    request_key = True
                self.stop.wait(0.01)
        except (OSError, EOFError, RuntimeError) as exc:
            self.error = type(exc).__name__ + ": " + str(exc)
        finally:
            self.stop.set()
            self.channel.close()
            self.reset()
            with self.condition:
                self.condition.notify_all()

    def frame(self, timeout=4):
        deadline = time.monotonic() + timeout
        with self.condition:
            while not self.stop.is_set() and (
                self.latest is None
                or time.monotonic()
                - max(self.latest[0], self.source_checked if self.source_idle else 0)
                > self.max_age
            ):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.condition.wait(min(0.1, remaining))
            if (
                self.stop.is_set()
                or self.latest is None
                or time.monotonic()
                - max(self.latest[0], self.source_checked if self.source_idle else 0)
                > self.max_age
            ):
                raise ProtocolError(self.error or "No fresh decoded video frame")
            began, data, metadata = self.latest
            return {
                **metadata,
                "binary": data,
                "source_idle": self.source_idle,
                "last_frame_age_ms": (time.monotonic() - began) * 1000,
            }

    def close(self):
        self.stop.set()
        self.channel.close()
        self.thread.join(3)
        self.reset()
