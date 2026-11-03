import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from iatest.mobile.devices import NativeDevice
from iatest.mobile.protocol import ProtocolError
from iatest.mobile.runtime import MobileSession
from iatest.mobile.store import validate_profile
from iatest.mobile.video import EncodedPacketSource


@pytest.mark.parametrize(
    "limits",
    [
        True,
        {"duration_seconds": True},
        {"duration_seconds": 0},
        {"duration_seconds": 7201},
        {"byte_limit": "1024"},
        {"byte_limit": 2147483649},
        {"unlimited": True},
    ],
)
def test_profile_rejects_invalid_or_unlimited_media_budgets(limits):
    with pytest.raises(ValueError, match="[Mm]edia"):
        validate_profile(
            {
                "id": "phone",
                "platform": "android",
                "device_id": "fixture",
                "app_id": "dev.iatest.mobile.lab",
                "media_limits": limits,
            }
        )


class Channel:
    closed = False
    calls = 0

    def request(self, op, arguments, timeout):
        assert op == "video_next"
        self.calls += 1
        return {
            "codec": "h264-annexb",
            "no_b_frames": True,
            "width": 2,
            "height": 2,
            "generation": "same-stream",
            "binary": b"\x00\x00\x01\x65",
            "key_frame": True,
        }

    def close(self):
        self.closed = True


def test_explicit_duration_preserves_stream_beyond_default_without_hiding_exhaustion():
    channel = Channel()
    source = EncodedPacketSource(channel, duration=3600, byte_limit=8)
    source.started -= 121
    assert source.frame()["generation"] == "same-stream"
    assert source.frame()["received_bytes"] == 8
    with pytest.raises(ProtocolError, match="Invalid bounded"):
        source.frame()
    assert channel.closed and channel.calls == 3
    expired = Channel()
    default = EncodedPacketSource(expired)
    default.started -= 121
    with pytest.raises(ProtocolError, match="duration"):
        default.frame()
    assert expired.closed and expired.calls == 0


def test_runtime_passes_explicit_media_limits_to_the_same_receiver():
    session = object.__new__(MobileSession)
    session.closed, session.media_receiver = False, None
    session.profile = {
        "media_limits": {"duration_seconds": 3600, "byte_limit": 1073741824}
    }
    channel = Channel()
    session.device = SimpleNamespace(
        semantic=SimpleNamespace(capabilities={"continuous_video": True}),
        open_media=lambda codec: channel,
    )
    first = session.preview(separate=True, codec="h264-native")
    second = session.preview(separate=True, codec="h264-native")
    assert first["receiver_id"] == second["receiver_id"]
    assert (
        session.media_receiver.duration == 3600
        and session.media_receiver.byte_limit == 1073741824
    )
    session.stop_preview(first["receiver_id"])
    assert channel.closed


def test_native_budget_mismatch_closes_channel_before_stream_consumption():
    device = object.__new__(NativeDevice)
    device.profile = {"media_limits": {"duration_seconds": 3600}}
    device.media = SimpleNamespace(
        capabilities={
            "media_limits": {"duration_seconds": 120, "byte_limit": 67108864}
        },
        close=lambda: setattr(device, "budget_channel_closed", True),
    )
    with pytest.raises(ProtocolError, match="did not confirm"):
        device._verify_media_limits()
    assert device.budget_channel_closed


def test_exhausted_explicit_budget_cannot_silently_open_a_new_stream():
    session = object.__new__(MobileSession)
    session.closed, session.media_receiver = False, None
    session.profile = {"media_limits": {"duration_seconds": 3600, "byte_limit": 1}}
    channels = []

    def open_media(codec):
        channel = Channel()
        channels.append(channel)
        return channel

    session.device = SimpleNamespace(
        semantic=SimpleNamespace(capabilities={"continuous_video": True}),
        open_media=open_media,
    )
    with pytest.raises(ProtocolError, match="Invalid bounded"):
        session.preview(separate=True, codec="h264-native")
    receiver_id = session.media_receiver.id
    with pytest.raises(ProtocolError, match="stop the preview"):
        session.preview(separate=True, codec="h264-native")
    assert len(channels) == 1 and channels[0].closed
    session.stop_preview(receiver_id)
    with pytest.raises(ProtocolError, match="Invalid bounded"):
        session.preview(separate=True, codec="h264-native")
    assert len(channels) == 2
    session.stop_preview(session.media_receiver.id)


def test_actual_native_budget_counts_before_publish_and_does_not_overflow(
    tmp_path, java_bin
):
    harness = tmp_path / "BudgetAcceptance.java"
    harness.write_text("""package dev.iatest.mobile;public class BudgetAcceptance {
        static void check(boolean b){if(!b)throw new AssertionError();}
        static void fails(Runnable f){try{f.run();}catch(IllegalArgumentException|IllegalStateException ok){return;}throw new AssertionError();}
        public static void main(String[] args){
            MediaBudget b=new MediaBudget(3600,2147483648L,100);
            b.check(121100);b.charge(2147483647L,122100);
            fails(()->b.charge(Long.MAX_VALUE,122100));
            b.charge(1,122100);fails(()->b.check(122100));
            MediaBudget small=new MediaBudget(1,100,0);
            fails(()->small.charge(101,0));small.charge(100,0);
            fails(()->new MediaBudget(0,1,0));fails(()->new MediaBudget(7201,1,0));
            fails(()->new MediaBudget(1,2147483649L,0));
            fails(()->new MediaBudget(1,1,100).check(99));
            fails(()->new MediaBudget(1,1,0).check(1000));
            System.out.println("native budgets passed");}}""")
    source = Path(
        "iatest/mobile/native/android/src/dev/iatest/mobile/MediaBudget.java"
    ).resolve()
    subprocess.run(
        [str(java_bin / "javac"), "-d", str(tmp_path), str(source), str(harness)],
        check=True,
        capture_output=True,
        timeout=15,
    )
    result = subprocess.run(
        [
            str(java_bin / "java"),
            "-cp",
            str(tmp_path),
            "dev.iatest.mobile.BudgetAcceptance",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert "native budgets passed" in result.stdout
