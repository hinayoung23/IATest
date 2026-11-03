import json
import os
import shutil
import socket
import ssl
import subprocess
import threading
import time
import uuid

import httpx
import pytest

from iatest.nodes.coordinator import CoordinatorClient, make_coordinator
from iatest.nodes.etcd import EtcdDocument, EtcdLeaseStore


def test_real_tls_auth_and_no_space_alarm_preserve_fencing(tmp_path, monkeypatch):
    """Owned TLS backend: bad identities and rejected writes cannot allocate claims."""
    executable = os.environ.get("IATEST_TEST_ETCD_BIN") or shutil.which("etcd")
    openssl = shutil.which("openssl")
    if not executable or not openssl:
        pytest.skip("Requires installed etcd and OpenSSL for real TLS/auth acceptance")
    cert, key = tmp_path / "ca.pem", tmp_path / "key.pem"
    subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-days", "1", "-subj", "/CN=owned-etcd-test", "-addext",
                    "subjectAltName=IP:127.0.0.1", "-keyout", str(key), "-out", str(cert)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    ports = []
    for _ in range(2):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            ports.append(sock.getsockname()[1])
    endpoint, peer = f"https://127.0.0.1:{ports[0]}", f"http://127.0.0.1:{ports[1]}"
    command = [executable, "--name", "owned", "--data-dir", str(tmp_path / "data"),
               "--listen-client-urls", endpoint, "--advertise-client-urls", endpoint,
               "--cert-file", str(cert), "--key-file", str(key),
               "--listen-peer-urls", peer, "--initial-advertise-peer-urls", peer,
               "--initial-cluster", "owned=" + peer, "--log-level", "error"]
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    http = httpx.Client(verify=ssl.create_default_context(cafile=cert), trust_env=False, timeout=2)
    documents = []
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                status = http.post(endpoint + "/v3/maintenance/status", json={}).json()
                if status.get("leader"):
                    break
            except (httpx.HTTPError, ValueError):
                pass
            if time.monotonic() >= deadline:
                pytest.fail("Owned TLS etcd did not become ready")
            time.sleep(.05)

        def admin(path, value):
            response = http.post(endpoint + path, json=value)
            assert response.status_code == 200, response.text
            return response.json()

        password = uuid.uuid4().hex
        admin("/v3/auth/user/add", {"name": "root", "password": password})
        admin("/v3/auth/user/grant", {"user": "root", "role": "root"})
        admin("/v3/auth/enable", {})
        token = admin("/v3/auth/authenticate", {"name": "root", "password": password})["token"]
        http.headers["Authorization"] = token
        monkeypatch.setenv("IATEST_OWNED_ETCD_TEST_TOKEN", token)
        spec = {"endpoints": [endpoint], "cluster_id": str(status["header"]["cluster_id"]),
                "ca": str(cert), "token_env": "IATEST_OWNED_ETCD_TEST_TOKEN"}

        def document(settings):
            value = EtcdDocument(settings)
            documents.append(value)
            return value

        with pytest.raises(ValueError, match="rejected"):
            document({k: v for k, v in spec.items() if k != "token_env"}).read()
        monkeypatch.setenv("IATEST_WRONG_ETCD_TEST_TOKEN", "wrong-owned-token")
        with pytest.raises(ValueError, match="rejected"):
            document({**spec, "token_env": "IATEST_WRONG_ETCD_TEST_TOKEN"}).read()
        with pytest.raises(RuntimeError, match="reachable"):
            document({k: v for k, v in spec.items() if k != "ca"}).read()
        with pytest.raises(ValueError, match="identity mismatch"):
            document({**spec, "cluster_id": "1"}).read()

        store = EtcdLeaseStore(spec, "owned-policy")
        documents.append(store.document)
        lease = store.operate("a", "acquire", {"id": "one", "resources": ["lab"], "ttl": 60}, ["lab"])
        claim = {"id": "one", "fences": json.loads(lease["fences"])}
        admin("/v3/maintenance/alarm", {"action": "ACTIVATE", "memberID": status["header"]["member_id"], "alarm": "NOSPACE"})
        with pytest.raises(ValueError, match="rejected"):
            store.operate("b", "acquire", {"id": "two", "resources": ["other"]}, ["other"])
        admin("/v3/maintenance/alarm", {"action": "DEACTIVATE", "memberID": status["header"]["member_id"], "alarm": "NOSPACE"})
        assert not admin("/v3/maintenance/alarm", {"action": "GET"}).get("alarms")
        state, _ = store.document.read()
        assert [row[0] for row in state["tables"]["leases"]] == ["one"]
        assert store.operate("a", "validate", claim, ["lab"])["state"] == "active"
        with pytest.raises(ValueError, match="occupied"):
            store.operate("b", "acquire", {"id": "three", "resources": ["lab"]}, ["lab"])
    finally:
        for value in documents:
            value.http.close()
        http.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)


def test_read_failover_keeps_reachable_member_for_following_compare_and_swap():
    endpoints = ["http://127.0.0.1:19001", "http://127.0.0.1:19002"]
    document = EtcdDocument({"endpoints": endpoints, "cluster_id": "123"})
    document.http.close()
    calls = []

    def handle(request):
        calls.append((request.url.port, request.url.path))
        if request.url.port == 19001:
            raise httpx.ConnectTimeout("Owned peer stopped", request=request)
        return httpx.Response(200, json={"header": {"cluster_id": "123"}, "succeeded": True})

    document.http = httpx.Client(transport=httpx.MockTransport(handle))
    try:
        assert document.read() == (None, 0)
        document.write({"tables": {}}, 0)
        assert calls == [(19001, "/v3/kv/range"), (19002, "/v3/kv/range"), (19002, "/v3/kv/txn")]
        assert endpoints == ["http://127.0.0.1:19001", "http://127.0.0.1:19002"]
    finally:
        document.http.close()


@pytest.fixture
def quorum(tmp_path):
    executable = os.environ.get("IATEST_TEST_ETCD_BIN") or shutil.which("etcd")
    if not executable:
        pytest.skip(
            "Requires an explicitly installed etcd binary for real consensus acceptance"
        )
    ports = []
    for _ in range(6):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            ports.append(sock.getsockname()[1])
    cluster = ",".join(f"n{i}=http://127.0.0.1:{ports[3 + i]}" for i in range(3))
    processes = []

    def start(i):
        command = [
            executable,
            "--name",
            f"n{i}",
            "--data-dir",
            str(tmp_path / f"n{i}"),
            "--listen-client-urls",
            f"http://127.0.0.1:{ports[i]}",
            "--advertise-client-urls",
            f"http://127.0.0.1:{ports[i]}",
            "--listen-peer-urls",
            f"http://127.0.0.1:{ports[3 + i]}",
            "--initial-advertise-peer-urls",
            f"http://127.0.0.1:{ports[3 + i]}",
            "--initial-cluster",
            cluster,
            "--initial-cluster-token",
            "iatest-owned-test",
            "--log-level",
            "error",
        ]
        return subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

    client = httpx.Client(trust_env=False, timeout=0.5)
    try:
        processes.extend(start(i) for i in range(3))
        status = None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                status = client.post(
                    f"http://127.0.0.1:{ports[0]}/v3/maintenance/status", json={}
                ).json()
                if status.get("leader"):
                    break
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(0.1)
        assert status and status.get("leader"), (
            "Owned etcd cluster did not elect a leader"
        )
        yield (
            {
                "endpoints": [f"http://127.0.0.1:{p}" for p in ports[:3]],
                "cluster_id": str(status["header"]["cluster_id"]),
            },
            processes,
            client,
            start,
        )
    finally:
        client.close()
        for process in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)


def test_real_quorum_authority_failover_and_no_minority_reassignment(
    quorum, tmp_path, monkeypatch
):
    spec, processes, http, start = quorum
    monkeypatch.setenv("IATEST_ETCD_AUTHORITY", "synthetic-authority-token")
    identities = [
        {"id": owner, "token_env": "IATEST_ETCD_AUTHORITY", "resources": ["lab"]}
        for owner in ("a", "b")
    ]
    replicas = [
        make_coordinator(tmp_path / f"unused{i}.sqlite", identities, port=0, etcd=spec)
        for i in range(2)
    ]
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in replicas]
    for thread in threads:
        thread.start()
    urls = [f"http://127.0.0.1:{s.server_port}" for s in replicas]
    first = CoordinatorClient(
        {"url": urls[0], "id": "a", "token_env": "IATEST_ETCD_AUTHORITY"}
    )
    second = None
    try:
        authority_id = first.call("list", {})["authority_id"]
        first.close()
        first = CoordinatorClient(
            {
                "url": urls[0],
                "urls": urls,
                "authority_id": authority_id,
                "id": "a",
                "token_env": "IATEST_ETCD_AUTHORITY",
            }
        )
        # The bounded election + minority outage can exceed ten seconds on
        # Windows. Keep this recovery test's lease alive; expiry stays fail-closed.
        lease = first.call("acquire", {"id": "one", "resources": ["lab"], "ttl": 30})
        import json

        claim = {"id": "one", "fences": json.loads(lease["fences"]), "ttl": 30}
        replicas[0].shutdown()
        replicas[0].server_close()
        assert first.call("renew", claim)["state"] == "active"
        second = CoordinatorClient(
            {"url": urls[1], "id": "b", "token_env": "IATEST_ETCD_AUTHORITY"}
        )
        with pytest.raises(ValueError, match="occupied"):
            second.call("acquire", {"id": "two", "resources": ["lab"]})
        # Remove the elected etcd leader; the remaining quorum must preserve the fence.
        statuses = [
            http.post(url + "/v3/maintenance/status", json={}).json()
            for url in spec["endpoints"]
        ]
        leader = next(
            i
            for i, s in enumerate(statuses)
            if str(s["header"]["member_id"]) == str(s["leader"])
        )
        processes[leader].terminate()
        processes[leader].wait(timeout=3)
        deadline = time.monotonic() + 8
        while True:
            try:
                assert first.call("validate", claim)["state"] == "active"
                break
            except (httpx.HTTPError, ValueError):
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.1)
        remaining = next(i for i in range(3) if i != leader)
        processes[remaining].terminate()
        processes[remaining].wait(timeout=3)
        with pytest.raises((RuntimeError, ValueError)):
            EtcdLeaseStore(spec, "unused").operate(
                "b", "acquire", {"id": "minority", "resources": ["lab"]}, ["lab"]
            )
        processes[leader] = start(leader)
        deadline = time.monotonic() + 8
        while True:
            try:
                assert first.call("validate", claim)["state"] == "active"
                break
            except (httpx.HTTPError, ValueError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
        first.call("uncertain", claim)
        with pytest.raises(ValueError, match="occupied"):
            second.call("acquire", {"id": "three", "resources": ["lab"]})
        first.call(
            "reconcile",
            {
                "id": "one",
                "stopped": True,
                "note": "Owned input workers stopped and all side effects reviewed",
            },
        )
        next_lease = second.call("acquire", {"id": "four", "resources": ["lab"]})
        assert json.loads(next_lease["fences"])["lab"] == 2
    finally:
        first.close()
        if second:
            second.close()
        for server, thread in zip(replicas, threads, strict=True):
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
