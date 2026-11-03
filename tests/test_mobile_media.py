import base64
import time

import pytest

from iatest.mobile.media import MediaReceiver
from iatest.mobile.protocol import ProtocolError

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/a9sAAAAASUVORK5CYII="
)


class Channel:
    def __init__(self, delay=0, bad=False):
        self.delay, self.bad, self.closed = delay, bad, False
        self.calls = []

    def request(self, op, arguments, timeout):
        self.calls.append(op)
        time.sleep(self.delay)
        if self.closed:
            raise ProtocolError("Media channel closed")
        return {"binary": PNG, "width": 2 if self.bad else 1, "height": 1}

    def close(self):
        self.closed = True


def test_media_receiver_uses_one_latest_frame_and_never_the_control_channel():
    channel = Channel()
    receiver = MediaReceiver(channel)
    try:
        value = receiver.frame()
        assert value["binary"] == PNG and value["transport"] == "separate-png"
        assert not value["continuous_video"]
        assert channel.calls == ["screenshot"]
        assert receiver.frames == 1 and receiver.total == len(PNG)
        assert isinstance(receiver.latest, tuple)
    finally:
        receiver.close()
    assert not receiver.thread.is_alive() and channel.closed


def test_media_rejects_stale_corrupt_or_over_budget_frames():
    for channel, options in [
        (Channel(delay=0.1), {"max_age": 0.01}),
        (Channel(bad=True), {}),
        (Channel(), {"byte_limit": 1}),
    ]:
        receiver = MediaReceiver(channel, **options)
        try:
            with pytest.raises(ProtocolError):
                receiver.frame(timeout=0.5)
        finally:
            receiver.close()
        assert channel.closed
