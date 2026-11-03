"""Session-owned reverse TLS for an iOS Runner whose USB port is occupied."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import select
import socket
import ssl
import subprocess
import time
from pathlib import Path

from iatest.execution.control import RunCancelled
from iatest.mobile.protocol import ProtocolError, read_frame

PREAMBLE = b"IATEST_REVERSE_TLS/1\n"


def validate_observation(value, *, simulator=False):
    if not isinstance(value, dict) or set(value) - {"transport", "host", "port"}:
        raise ValueError("Unknown iOS observation transport fields")
    if value.get("transport") != "reverse_tls":
        raise ValueError("Network observation requires reverse_tls")
    host = ipaddress.ip_address(value.get("host", ""))
    networks = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8")
    if host.version != 4 or not any(host in ipaddress.ip_network(n) for n in networks):
        raise ValueError("Observation host must be an explicit private IPv4 address")
    if host.is_loopback and not simulator:
        raise ValueError("Physical iPhone needs a reachable controller LAN address")
    port = value.get("port", 0)
    if type(port) is not int or port != 0 and not 1024 <= port <= 65535:
        raise ValueError("Observation port must be 0 or 1024–65535")


class ReverseTLSListener:
    def __init__(self, config, directory, *, session_id, token, app_id, control=None):
        self.control, self.session_id, self.token, self.app_id = control, session_id, token, app_id
        self.socket = None
        self.directory = Path(directory)
        cert, key = self.directory / "observation-cert.pem", self.directory / "observation-key.pem"
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
             "-subj", "/CN=iatest-session-observation", "-keyout", str(key), "-out", str(cert)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
        )
        key.chmod(0o600)
        der = ssl.PEM_cert_to_DER_cert(cert.read_text(encoding="ascii"))
        self.pin = hashlib.sha256(der).hexdigest()
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.context.load_cert_chain(cert, key)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.bind((config["host"], config.get("port", 0)))
            listener.listen(4)
        except BaseException:
            listener.close()
            raise
        self.socket = listener
        self.host, self.port = listener.getsockname()

    def environment(self):
        return {"IATEST_OBSERVATION_HOST": self.host, "IATEST_OBSERVATION_PORT": str(self.port),
                "IATEST_OBSERVATION_PIN": self.pin}

    def accept(self, role="control", *, timeout=90, setup_deadline=None):
        deadline = time.monotonic() + timeout
        failures = 0
        while time.monotonic() < deadline:
            if self.control:
                self.control.check()
            self.socket.settimeout(min(.25, max(.001, deadline-time.monotonic())))
            try:
                sock, _peer = self.socket.accept()
            except TimeoutError:
                continue
            accepted = None
            try:
                # The caller polls startup every two seconds. Once a peer
                # connects, keep its handshake within the overall startup
                # budget rather than the remaining idle-poll interval.
                authenticated_by = min(deadline if setup_deadline is None else setup_deadline, time.monotonic()+10)
                accepted = self.context.wrap_socket(sock, server_side=True, do_handshake_on_connect=False)
                accepted.setblocking(False)
                while True:
                    if self.control:
                        self.control.check()
                    remaining = authenticated_by-time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("TLS observation handshake deadline exceeded")
                    try:
                        accepted.do_handshake()
                        break
                    except ssl.SSLWantReadError:
                        select.select([accepted], [], [], min(.25, remaining))
                    except ssl.SSLWantWriteError:
                        select.select([], [accepted], [], min(.25, remaining))
                accepted.settimeout(min(3, max(.001, authenticated_by-time.monotonic())))
                accepted.sendall(PREAMBLE)
                value = read_frame(accepted, min(authenticated_by, time.monotonic()+3))
                if (set(value) != {"transport", "session_id", "token", "app_id", "role"}
                    or value["transport"] != "iatest-reverse-tls/1"
                    or value["session_id"] != self.session_id or value["app_id"] != self.app_id
                    or value["role"] != role or not isinstance(value["token"], str)
                    or not hmac.compare_digest(value["token"], self.token)):
                    raise ProtocolError("Network observation binding rejected")
                accepted.sendall(b"OK\n")
                accepted.settimeout(2)
                return accepted
            except RunCancelled:
                (accepted or sock).close()
                raise
            except (OSError, EOFError, RuntimeError, ValueError):
                (accepted or sock).close()
                failures += 1
                if failures >= 8:
                    raise ProtocolError("Network observation authentication/connection budget exhausted") from None
            except BaseException:
                (accepted or sock).close()
                raise
        raise TimeoutError("Paired iOS Runner did not connect to the TLS observation listener")

    def close(self):
        if self.socket:
            self.socket.close()
            self.socket = None
