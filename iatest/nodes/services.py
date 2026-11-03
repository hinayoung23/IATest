"""Web-owned controller lifecycle; node secrets always remain environment references."""

from __future__ import annotations

import threading

_LOCK = threading.RLock()
_SERVERS = {}
_COORDINATORS = {}


def status(root, project):
    with _LOCK:
        item = _SERVERS.get((str(root.resolve()), project))
        return {"running": bool(item), **(item[2] if item else {})}


def start(root, project, spec):
    from iatest.nodes.protocol import make_controller

    with _LOCK:
        key = (str(root.resolve()), project)
        if key in _SERVERS:
            raise ValueError("Controller is already running in this Web process")
        if set(spec) - {"host", "port", "cert", "key"}:
            raise ValueError("Unknown controller setting")
        host, port = spec.get("host", "127.0.0.1"), spec.get("port", 8768)
        if not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("Controller port must be an integer from 0 to 65535")
        server = make_controller(
            root,
            project,
            host,
            port,
            certfile=spec.get("cert", ""),
            keyfile=spec.get("key", ""),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        value = {
            "host": host,
            "port": server.server_port,
            "tls": bool(spec.get("cert")),
        }
        _SERVERS[key] = server, thread, value
        return value


def stop(root, project):
    with _LOCK:
        item = _SERVERS.pop((str(root.resolve()), project), None)
    if item:
        item[0].shutdown()
        item[0].server_close()
        item[1].join(timeout=2)


def close_all():
    with _LOCK:
        keys = list(_SERVERS)
    from pathlib import Path

    for root, project in keys:
        stop(Path(root), project)
    with _LOCK:
        keys = list(_COORDINATORS)
    for root, project in keys:
        coordinator_stop(Path(root), project)


def coordinator_status(root, project):
    with _LOCK:
        item = _COORDINATORS.get((str(root.resolve()), project))
        return {"running": bool(item), **(item[2] if item else {})}


def coordinator_start(root, project, spec):
    from iatest.core.paths import project_runtime_dir
    from iatest.nodes.coordinator import make_coordinator

    if set(spec) - {"host", "port", "cert", "key", "identities", "etcd"}:
        raise ValueError("Unknown coordinator setting")
    with _LOCK:
        index = (str(root.resolve()), project)
        if index in _COORDINATORS:
            raise ValueError("Resource authority is already running")
        server = make_coordinator(
            project_runtime_dir(root, project) / "shared-resources.sqlite3",
            spec["identities"],
            spec.get("host", "127.0.0.1"),
            spec.get("port", 8769),
            cert=spec.get("cert", ""),
            key=spec.get("key", ""),
            etcd=spec.get("etcd"),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        status = {
            "host": spec.get("host", "127.0.0.1"),
            "port": server.server_port,
            "tls": bool(spec.get("cert")),
        }
        _COORDINATORS[index] = server, thread, status
        return status


def coordinator_stop(root, project):
    with _LOCK:
        item = _COORDINATORS.pop((str(root.resolve()), project), None)
    if item:
        item[0].shutdown()
        item[0].server_close()
        item[1].join(timeout=2)
