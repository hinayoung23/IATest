from __future__ import annotations

import hashlib
import os
import shutil
import socket
import ssl
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from iatest.execution.control import RunCancelled, RunControl
from iatest.mobile.network_observation import (
    PREAMBLE,
    ReverseTLSListener,
    validate_observation,
)
from iatest.mobile.protocol import receive_exact, write_frame
from iatest.mobile.store import validate_profile


@pytest.fixture
def listener(tmp_path):
    if not shutil.which("openssl"):
        pytest.skip("openssl is unavailable")
    result = ReverseTLSListener(
        {"host": "127.0.0.1"}, tmp_path,
        session_id="owned-session", token="s" * 40, app_id="dev.iatest.lab",
    )
    try:
        yield result
    finally:
        result.close()


def connect(listener, **binding):
    # Exact leaf pin is the trust anchor; authenticate before sending the token.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    connection = context.wrap_socket(socket.create_connection((listener.host, listener.port)))
    assert hashlib.sha256(connection.getpeercert(binary_form=True)).hexdigest() == listener.pin
    assert receive_exact(connection, len(PREAMBLE)) == PREAMBLE
    write_frame(connection, {
        "transport": "iatest-reverse-tls/1", "session_id": listener.session_id,
        "token": listener.token, "app_id": listener.app_id, "role": "control", **binding,
    })
    return connection


@pytest.mark.parametrize("wrong", [{"token": "wrong"}, {"session_id": "other"}, {"role": "media"}])
def test_invalid_binding_is_closed_then_correct_peer_can_connect(listener, wrong):
    with ThreadPoolExecutor() as pool:
        accepted = pool.submit(listener.accept, timeout=5)
        with connect(listener, **wrong) as rejected:
            assert rejected.recv(3) == b""
        with connect(listener) as valid:
            assert receive_exact(valid, 3) == b"OK\n"
            server = accepted.result(timeout=5)
            try:
                valid.sendall(b"actual tls payload")
                assert receive_exact(server, 18) == b"actual tls payload"
            finally:
                server.close()


@pytest.mark.parametrize("host", ["0.0.0.0", "8.8.8.8", "224.0.0.1", "::1", "192.0.2.1", "127.0.0.1"])
def test_physical_observation_rejects_unreachable_or_public_hosts(host):
    with pytest.raises(ValueError):
        validate_observation({"transport": "reverse_tls", "host": host})


def test_hid_requires_independent_observation():
    profile = {
        "id": "hid", "platform": "ios", "device_id": "phone", "app_id": "dev.iatest.lab",
        "ios_input_backend": "usb_hid",
        "usb_hid": {"endpoint": "http://127.0.0.1:19430", "binding_id": "cable", "token_env": "HID_TOKEN"},
    }
    with pytest.raises(ValueError, match="independent TLS"):
        validate_profile(profile)
    validate_profile({**profile, "ios_observation": {"transport": "reverse_tls", "host": "192.168.0.2"}})


@pytest.fixture(scope="module")
def native_client(tmp_path_factory):
    if sys.platform != "darwin" or not shutil.which("clang"):
        pytest.skip("Apple Foundation/SecureTransport compiler requires macOS")
    root = Path(__file__).resolve().parents[1]
    native = root / "iatest/mobile/native/ios"
    target = tmp_path_factory.mktemp("observation-native") / "client"
    subprocess.run([
        "clang", "-fobjc-arc", "-Wno-deprecated-declarations", "-I", str(native),
        str(root / "tests/fixtures/observation_client.m"), str(native / "NetworkTransport.m"),
        "-framework", "Foundation", "-framework", "Security", "-o", str(target),
    ], check=True, capture_output=True)
    return target


@pytest.mark.parametrize("correct_pin", [True, False])
def test_actual_native_securetransport_pin_and_encrypted_bytes(listener, native_client, correct_pin):
    env = {**os.environ, **listener.environment(), "IATEST_SESSION": listener.session_id,
           "IATEST_TOKEN": listener.token, "IATEST_APP": listener.app_id}
    if not correct_pin:
        env["IATEST_OBSERVATION_PIN"] = "0" * 64
    process = subprocess.Popen([str(native_client)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        if correct_pin:
            with listener.accept(timeout=5) as server:
                server.sendall(b"ping")
                assert receive_exact(server, 4) == b"pong"
        else:
            with pytest.raises(TimeoutError):
                listener.accept(timeout=1)
        assert process.wait(timeout=5) == (0 if correct_pin else 2)
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


def test_native_connection_can_wait_for_listener_without_sending_unauthenticated_data(listener, native_client):
    # Retain the real TLS context and pin while delaying the actual TCP listener.
    listener.socket.close()
    listener.socket = None
    env = {**os.environ, **listener.environment(), "IATEST_SESSION": listener.session_id,
           "IATEST_TOKEN": listener.token, "IATEST_APP": listener.app_id}
    process = subprocess.Popen([str(native_client)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        time.sleep(.3)
        assert process.poll() is None, "First refused connection must not terminate startup"
        listener.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.socket.bind((listener.host, listener.port))
        listener.socket.listen(1)
        with listener.accept(timeout=5) as server:
            server.sendall(b"ping")
            assert receive_exact(server, 4) == b"pong"
        assert process.wait(timeout=5) == 0
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


def test_native_tls_can_receive_after_socket_read_timeout(listener, native_client):
    env = {**os.environ, **listener.environment(), "IATEST_SESSION": listener.session_id,
           "IATEST_TOKEN": listener.token, "IATEST_APP": listener.app_id}
    process = subprocess.Popen([str(native_client)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        with listener.accept(timeout=5) as server:
            # The console waits for a new HTTP request after opening a session.
            # Exercise the native one-second recv timeout before sending data.
            time.sleep(1.3)
            server.sendall(b"ping")
            assert receive_exact(server, 4) == b"pong"
        assert process.wait(timeout=5) == 0
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


def test_connected_peer_gets_setup_budget_beyond_idle_poll_interval(listener):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    raw = socket.create_connection((listener.host, listener.port))
    with ThreadPoolExecutor() as pool:
        accepted = pool.submit(listener.accept, timeout=.1, setup_deadline=time.monotonic()+4)
        time.sleep(.25)  # ClientHello arrives after the startup poll has expired.
        with context.wrap_socket(raw) as peer:
            assert hashlib.sha256(peer.getpeercert(binary_form=True)).hexdigest() == listener.pin
            assert receive_exact(peer, len(PREAMBLE)) == PREAMBLE
            write_frame(peer, {"transport":"iatest-reverse-tls/1", "session_id":listener.session_id,
                "token":listener.token, "app_id":listener.app_id, "role":"control"})
            assert receive_exact(peer, 3) == b"OK\n"
            server = accepted.result(timeout=3)
            server.close()


def test_stalled_tls_setup_still_obeys_cancellation(listener):
    listener.control = RunControl(timeout_seconds=.3)
    peer = socket.create_connection((listener.host, listener.port))
    started = time.monotonic()
    try:
        with pytest.raises(RunCancelled):
            listener.accept(timeout=.1, setup_deadline=started+10)
        assert time.monotonic()-started < 2
    finally:
        peer.close()
