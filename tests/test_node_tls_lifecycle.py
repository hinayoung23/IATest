"""Real TLS and controller authentication lifecycle; synthetic local credentials only."""

import secrets
import shutil
import subprocess
import threading

import httpx
import pytest

from iatest.core.config import init_project
from iatest.nodes.protocol import make_controller
from iatest.nodes.store import ControllerStore
from iatest.nodes.worker import NodeWorker


def test_controller_certificate_rotation_expiry_ca_retirement_and_identity_revocation(
    tmp_path, monkeypatch
):
    openssl = shutil.which("openssl")
    if not openssl:
        pytest.skip("Requires OpenSSL for real certificate lifecycle checks")

    def run(*args):
        subprocess.run(
            [openssl, *map(str, args)], check=True, capture_output=True, timeout=20
        )

    def authority(name):
        cert, key = tmp_path / (name + ".pem"), tmp_path / (name + ".key")
        run(
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "2",
            "-subj",
            "/CN=" + name,
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
            "-keyout",
            key,
            "-out",
            cert,
        )
        return cert, key

    first_ca = authority("first-ca")
    second_ca = authority("second-ca")
    extensions = tmp_path / "server.ext"
    leaf_extensions = "subjectAltName=IP:127.0.0.1\nextendedKeyUsage=serverAuth\nbasicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n"
    extensions.write_text(leaf_extensions)

    def leaf(name, ca, days=1):
        key, csr, cert = (
            tmp_path / (name + suffix) for suffix in (".key", ".csr", ".pem")
        )
        run(
            "req",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-subj",
            "/CN=127.0.0.1",
            "-keyout",
            key,
            "-out",
            csr,
        )
        if days < 0:
            database = tmp_path / "expired-index"
            database.touch()
            serial = tmp_path / "expired-serial"
            serial.write_text("2000\n")
            config = tmp_path / "expired-ca.cnf"
            config.write_text(
                "[ca]\ndefault_ca=local\n[local]\n"
                + f"database={database.as_posix()}\nserial={serial.as_posix()}\nnew_certs_dir={tmp_path.as_posix()}\n"
                + f"certificate={ca[0].as_posix()}\nprivate_key={ca[1].as_posix()}\n"
                + "default_md=sha256\npolicy=any_name\n[any_name]\ncommonName=supplied\n[server]\n"
                + leaf_extensions
            )
            run(
                "ca",
                "-batch",
                "-notext",
                "-config",
                config,
                "-in",
                csr,
                "-startdate",
                "20200101000000Z",
                "-enddate",
                "20200102000000Z",
                "-extensions",
                "server",
                "-out",
                cert,
            )
        else:
            run(
                "x509",
                "-req",
                "-in",
                csr,
                "-CA",
                ca[0],
                "-CAkey",
                ca[1],
                "-CAcreateserial",
                "-days",
                str(days),
                "-extfile",
                extensions,
                "-out",
                cert,
            )
        return cert, key

    original = leaf("original", first_ca)
    renewed = leaf("renewed", first_ca)
    expired = leaf("expired", first_ca, -1)
    new_authority = leaf("new-authority", second_ca)
    root = tmp_path / "controller"
    init_project(root, "suite")
    token_env = "IATEST_SYNTHETIC_TLS_ROTATION_NODE"
    monkeypatch.setenv(token_env, secrets.token_urlsafe(24))
    store = ControllerStore(root, "suite")
    store.enroll(
        {
            "id": "node",
            "token_env": token_env,
            "allowed_origins": ["http://127.0.0.1"],
            "allowed_kinds": ["api"],
        }
    )
    server = thread = None
    workers = []
    port = 0

    def start(pair):
        nonlocal server, thread, port
        if server:
            server.shutdown()
            server.server_close()
            thread.join(3)
        server = make_controller(
            root, "suite", port=port, certfile=str(pair[0]), keyfile=str(pair[1])
        )
        port = server.server_port
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

    def worker(ca, node="node"):
        instance = NodeWorker(
            tmp_path / ("worker-" + str(len(workers))),
            "suite",
            f"https://127.0.0.1:{port}",
            node,
            token_env,
            ["http://127.0.0.1"],
            allowed_kinds=("api",),
            ca_file=str(ca),
        )
        workers.append(instance)
        return instance

    try:
        start(original)
        old = worker(first_ca[0])
        old.register()
        assert old.post("poll", {})["job"] is None
        start(
            renewed
        )  # Same CA and address; the existing worker reconnects after restart.
        assert old.post("poll", {})["job"] is None
        monkeypatch.setenv(token_env, secrets.token_urlsafe(24))
        with pytest.raises(RuntimeError, match="authentication failed"):
            old.post("poll", {})
        current = worker(first_ca[0])
        current.register()
        with pytest.raises(RuntimeError, match="authentication failed"):
            worker(first_ca[0], "other").register()
        assert store.nodes()[0]["instance"] == current.instance
        start(expired)
        with pytest.raises(
            httpx.ConnectError, match="(?i)expired|certificate_verify_failed"
        ):
            current.post("poll", {})
        start(renewed)
        assert current.post("poll", {})["job"] is None
        start(new_authority)
        with pytest.raises(httpx.ConnectError):
            current.post("poll", {})
        overlap = tmp_path / "overlap.pem"
        overlap.write_bytes(first_ca[0].read_bytes() + second_ca[0].read_bytes())
        worker(overlap).register()
        retired = worker(second_ca[0])
        retired.register()
        assert retired.post("poll", {})["job"] is None
        start(original)
        with pytest.raises(httpx.ConnectError):
            retired.post("poll", {})
    finally:
        for instance in workers:
            instance.close()
        if server:
            server.shutdown()
            server.server_close()
            thread.join(3)
