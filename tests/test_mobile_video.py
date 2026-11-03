import shutil
import subprocess
import threading
import time

import pytest

from iatest.mobile.protocol import ProtocolError
from iatest.mobile.video import (
    EncodedMediaReceiver,
    EncodedPacketSource,
    VideoDecoder,
)


@pytest.fixture
def encoded():
    if not shutil.which("ffmpeg"):
        pytest.skip("Requires installed FFmpeg for real codec acceptance")
    value = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=96x64:rate=15",
            "-frames:v",
            "20",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-bf",
            "0",
            "-g",
            "5",
            "-x264-params",
            "aud=1:repeat-headers=1",
            "-f",
            "h264",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
        timeout=15,
    ).stdout
    # Access-unit delimiters are explicit in the real encoder output.
    import re

    starts = [m.start() for m in re.finditer(b"\x00\x00\x00\x01\x09", value)]
    assert len(starts) == 20
    return [value[s:e] for s, e in zip(starts, starts[1:] + [len(value)], strict=True)]


def test_real_h264_decoder_preserves_all_frames_and_reaps_process(encoded):
    frames = []
    event = threading.Event()

    def decoded(data, metadata):
        frames.append((data, metadata))
        if len(frames) == len(encoded):
            event.set()

    decoder = VideoDecoder(decoded)
    try:
        for i, packet in enumerate(encoded):
            while decoder.queue.full():
                time.sleep(0.01)
            decoder.feed(packet, {"width": 96, "height": 64, "sequence": i})
        decoder.finish()
        assert event.wait(5), (len(frames), decoder.error)
        assert [m["sequence"] for _, m in frames] == list(range(20))
        assert all(data.startswith(b"\x89PNG") for data, _ in frames)
        assert len({data for data, _ in frames}) > 1
    finally:
        decoder.close()
    assert decoder.process.poll() is not None
    assert not decoder.reader.is_alive() and not decoder.writer.is_alive()


def test_receiver_resets_at_generation_boundary_without_replaying_control(encoded):
    class Channel:
        closed = False
        index = 0

        def __init__(self):
            self.keys = []

        def request(self, op, arguments, timeout):
            assert op == "video_next"
            self.keys.append(arguments["request_keyframe"])
            if self.closed:
                raise ProtocolError("Closed")
            index = self.index
            self.index += 1
            time.sleep(0.04)
            i = index % 20
            return {
                "binary": encoded[i],
                "codec": "h264-annexb",
                "no_b_frames": True,
                "width": 96,
                "height": 64,
                "generation": "a" if index < 20 else "b",
                "key_frame": i % 5 == 0,
            }

        def close(self):
            self.closed = True

    channel = Channel()
    receiver = EncodedMediaReceiver(channel)
    try:
        assert receiver.frame()["continuous_video"]
        deadline = time.monotonic() + 5
        while receiver.generation != "b" and time.monotonic() < deadline:
            time.sleep(0.01)
        value = receiver.frame()
        assert value["generation"] == "b"
        assert receiver.latest[1] == value["binary"]
    finally:
        receiver.close()
    assert channel.closed and not receiver.thread.is_alive()


def test_encoded_receiver_rejects_non_annexb_before_decoding():
    class Bad:
        closed = False

        def request(self, *_args, **_kwargs):
            return {"codec": "h264-annexb", "no_b_frames": True, "binary": b"invalid"}

        def close(self):
            self.closed = True

    channel = Bad()
    receiver = EncodedMediaReceiver(channel)
    try:
        with pytest.raises(ProtocolError, match="Invalid bounded"):
            receiver.frame()
    finally:
        receiver.close()
    assert channel.closed


def test_idle_packet_does_not_establish_decoder_generation():
    class Channel:
        closed = False

        def __init__(self):
            self.keys = []

        packets = iter(
            [
                {"idle": True, "key_frame": False, "binary": b""},
                {"key_frame": False, "binary": b"\x00\x00\x01\x41"},
                {"key_frame": True, "binary": b"\x00\x00\x01\x65"},
            ]
        )

        def request(self, _op, arguments, **_kwargs):
            self.keys.append(arguments["request_keyframe"])
            return {
                "codec": "h264-annexb",
                "no_b_frames": True,
                "width": 96,
                "height": 64,
                "generation": "new",
                **next(self.packets),
            }

        def close(self):
            self.closed = True

    channel = Channel()
    source = EncodedPacketSource(channel)
    try:
        assert source.frame()["idle"]
        assert source.generation is None
        assert source.frame()["key_frame"]
        assert channel.keys == [True, True, True]
    finally:
        source.close()


def test_idle_packet_cannot_hide_encoded_payload():
    class Channel:
        closed = False

        def request(self, *_args, **_kwargs):
            return {
                "codec": "h264-annexb",
                "no_b_frames": True,
                "idle": True,
                "key_frame": False,
                "binary": b"\x00\x00\x01\x65",
            }

        def close(self):
            self.closed = True

    channel = Channel()
    with pytest.raises(ProtocolError, match="Invalid bounded"):
        EncodedPacketSource(channel).frame()
    assert channel.closed


def test_idle_keeps_last_real_frame_with_age_but_cannot_cross_generation(encoded):
    class Channel:
        closed = False
        index = 0
        generation = "a"

        def request(self, *_args, **_kwargs):
            time.sleep(0.03)
            if self.closed:
                raise ProtocolError("Closed")
            index = self.index
            self.index += 1
            base = {
                "codec": "h264-annexb",
                "no_b_frames": True,
                "width": 96,
                "height": 64,
                "generation": self.generation,
            }
            if index < len(encoded):
                return {**base, "binary": encoded[index], "key_frame": index % 5 == 0}
            return {**base, "binary": b"", "idle": True, "key_frame": False}

        def close(self):
            self.closed = True

    channel = Channel()
    receiver = EncodedMediaReceiver(channel, max_age=0.3)
    try:
        assert receiver.frame()["binary"].startswith(b"\x89PNG")
        deadline = time.monotonic() + 5
        while not receiver.source_idle and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(0.5)
        before = receiver.frame()
        assert before["source_idle"] and before["last_frame_age_ms"] > 300
        time.sleep(0.1)
        after = receiver.frame()
        assert before["binary"] == after["binary"]
        assert before["received_frames"] == after["received_frames"]
        assert after["last_frame_age_ms"] > before["last_frame_age_ms"]
        channel.generation = "b"
        deadline = time.monotonic() + 1
        while receiver.generation != "b" and time.monotonic() < deadline:
            time.sleep(0.01)
        with pytest.raises(ProtocolError, match="No fresh"):
            receiver.frame(timeout=0.1)
    finally:
        receiver.close()
    assert channel.closed and not receiver.thread.is_alive()
