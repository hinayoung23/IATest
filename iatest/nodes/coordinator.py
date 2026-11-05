"""Shared resource authority for independent controllers, without automatic failover."""

from __future__ import annotations
from iatest.core.env import environment_values

import hmac
import json
import os
import sqlite3
import ssl
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit

import httpx

HELD = ContextVar("iatest_global_resource_leases", default=MappingProxyType({}))
DELEGATED = ContextVar("iatest_delegated_resource_leases", default=())


class LeaseStore:
    def __init__(self, path, *, clock=time.time):
        self.path, self.clock = Path(path), clock
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript("""
                create table if not exists leases(id text primary key,owner text,resources text,fences text,state text,expires real);
                create table if not exists claims(resource text primary key,lease_id text,fence integer);
                create table if not exists generations(resource text primary key,fence integer);
                create table if not exists audit(created real,owner text,lease_id text,action text,note text);
            """)
            conn.execute("begin immediate")
            conn.execute(
                "update leases set state='uncertain' where state='active' and expires<?",
                (self.clock(),),
            )
            yield conn
            conn.commit()
        finally:
            conn.close()

    def operate(self, owner, action, value, allowed):
        if not isinstance(value, dict):
            raise TypeError("Coordinator messages must be JSON objects")
        if action == "list":
            with self.connection() as c:
                return {
                    "leases": [
                        dict(r)
                        for r in c.execute(
                            "select * from leases where owner=? and state!='released'",
                            (owner,),
                        )
                    ]
                }
        lease_id = value.get("id", "")
        if not isinstance(lease_id, str) or not 1 <= len(lease_id) <= 100:
            raise ValueError("A bounded idempotency/lease ID is required")
        ttl = value.get("ttl", 10)
        if type(ttl) not in (float, int) or not 2 <= ttl <= 60:
            raise ValueError("Lease TTL must be 2–60 seconds")
        with self.connection() as c:
            row = c.execute("select * from leases where id=?", (lease_id,)).fetchone()
            if action == "acquire":
                resources = value.get("resources")
                if (
                    not isinstance(resources, list)
                    or not 1 <= len(resources) <= 100
                    or any(
                        not isinstance(r, str) or not 1 <= len(r) <= 200
                        for r in resources
                    )
                    or len(set(resources)) != len(resources)
                ):
                    raise ValueError("Invalid bounded resource set")
                if "*" not in allowed and not set(resources) <= set(allowed):
                    raise ValueError("Controller does not authorize these resources")
                resources = sorted(resources)
                if row:
                    if (
                        row["owner"] != owner
                        or json.loads(row["resources"]) != resources
                        or row["state"] != "active"
                    ):
                        raise ValueError("Lease ID cannot be rebound or resumed")
                    return dict(row)
                fences = {}
                for resource in resources:
                    if c.execute(
                        "select 1 from claims where resource=?", (resource,)
                    ).fetchone():
                        raise ValueError(
                            "Resource is occupied or awaits reconciliation: " + resource
                        )
                    old = c.execute(
                        "select fence from generations where resource=?", (resource,)
                    ).fetchone()
                    fence = old[0] + 1 if old else 1
                    c.execute(
                        "insert or replace into generations values(?,?)",
                        (resource, fence),
                    )
                    c.execute(
                        "insert into claims values(?,?,?)", (resource, lease_id, fence)
                    )
                    fences[resource] = fence
                c.execute(
                    "insert into leases values(?,?,?,?,?,?)",
                    (
                        lease_id,
                        owner,
                        json.dumps(resources),
                        json.dumps(fences),
                        "active",
                        self.clock() + ttl,
                    ),
                )
                c.execute(
                    "insert into audit values(?,?,?,?,?)",
                    (self.clock(), owner, lease_id, action, ""),
                )
            else:
                if not row or row["owner"] != owner:
                    raise ValueError("Lease belongs to another controller")
                if action == "reconcile":
                    note = value.get("note", "")
                    if (
                        row["state"] != "uncertain"
                        or value.get("stopped") is not True
                        or not isinstance(note, str)
                        or not 10 <= len(note) <= 2000
                    ):
                        raise ValueError(
                            "Reconciliation needs uncertain state, explicit stopped evidence and a 10–2000 character note"
                        )
                    c.execute("delete from claims where lease_id=?", (lease_id,))
                    c.execute(
                        "update leases set state='released' where id=?", (lease_id,)
                    )
                    c.execute(
                        "insert into audit values(?,?,?,?,?)",
                        (self.clock(), owner, lease_id, action, note),
                    )
                else:
                    if row["state"] != "active" or value.get("fences") != json.loads(
                        row["fences"]
                    ):
                        raise ValueError(
                            "Lease expired, uncertain or fencing generation differs"
                        )
                    if action == "renew":
                        c.execute(
                            "update leases set expires=? where id=?",
                            (self.clock() + ttl, lease_id),
                        )
                    elif action == "release":
                        c.execute("delete from claims where lease_id=?", (lease_id,))
                        c.execute(
                            "update leases set state='released' where id=?", (lease_id,)
                        )
                    elif action == "uncertain":
                        c.execute(
                            "update leases set state='uncertain' where id=?",
                            (lease_id,),
                        )
                    elif action != "validate":
                        raise ValueError("Unknown lease operation")
                    if action != "validate":
                        c.execute(
                            "insert into audit values(?,?,?,?,?)",
                            (self.clock(), owner, lease_id, action, ""),
                        )
            return dict(
                c.execute("select * from leases where id=?", (lease_id,)).fetchone()
            )


def make_coordinator(
    database, identities, host="127.0.0.1", port=8769, *, cert="", key="", etcd=None
):
    store = LeaseStore(database)
    entries = {item["id"]: item for item in identities}
    if not entries or any(
        set(item) - {"id", "token_env", "resources"}
        or not environment_values().get(item.get("token_env", ""))
        or not isinstance(item.get("resources"), list)
        for item in entries.values()
    ):
        raise ValueError(
            "Coordinator identities require existing token env references and resource allowlists"
        )
    if etcd:
        from iatest.application.engine_analysis import digest
        from iatest.nodes.etcd import EtcdLeaseStore

        store = EtcdLeaseStore(
            etcd, digest(sorted(identities, key=lambda item: item["id"]))
        )
    authority_id = (
        "etcd:" + store.document.cluster + ":" + store.document.key + ":" + store.policy
        if etcd
        else "local:" + uuid.uuid4().hex
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            identity = self.headers.get("X-IATest-Controller", "")
            entry = entries.get(identity)
            token = self.headers.get("Authorization", "").removeprefix("Bearer ")
            status, result = 200, {}
            try:
                if not entry or not hmac.compare_digest(
                    token.encode(), environment_values()[entry["token_env"]].encode()
                ):
                    status = 401
                    raise ValueError("Controller authentication failed")
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536:
                    raise ValueError("Coordinator payload exceeds bounds")
                action = self.path.removeprefix("/leases/")
                if not self.path.startswith("/leases/"):
                    raise ValueError("Unknown coordinator endpoint")
                result = store.operate(
                    identity,
                    action,
                    json.loads(self.rfile.read(length)),
                    entry["resources"],
                )
                result["authority_id"] = authority_id
            except (
                ValueError,
                RuntimeError,
                TypeError,
                KeyError,
                sqlite3.Error,
            ) as exc:
                status = status if status != 200 else 409
                result = {"error": str(exc)[:500]}
            data = json.dumps(result, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    if host not in {"127.0.0.1", "localhost", "::1"} and not cert:
        raise ValueError("LAN resource coordinators require TLS")
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    if etcd:
        close_server = server.server_close

        def close():
            close_server()
            store.document.http.close()

        server.server_close = close
    if cert:
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            server.socket = context.wrap_socket(server.socket, server_side=True)
        except BaseException:
            server.server_close()
            raise
    return server


class CoordinatorClient:
    def __init__(self, spec):
        if set(spec) - {
            "url",
            "urls",
            "authority_id",
            "id",
            "token_env",
            "ca",
            "ttl",
            "wait_seconds",
        }:
            raise ValueError("Unknown resource coordinator setting")
        self.spec = spec
        self.url = spec["url"].rstrip("/")
        self.endpoints = spec.get("urls", [self.url])
        if len(self.endpoints) > 1 and not str(spec.get("authority_id", "")).startswith(
            "etcd:"
        ):
            raise ValueError(
                "Replica failover requires the pinned consensus-backed authority ID"
            )
        if (
            not isinstance(self.endpoints, list)
            or not 1 <= len(self.endpoints) <= 7
            or self.url not in self.endpoints
        ):
            raise ValueError(
                "Declare 1–7 authority replicas including the canonical URL"
            )
        for endpoint in self.endpoints:
            parsed_endpoint = urlsplit(endpoint)
            if (
                parsed_endpoint.scheme not in {"http", "https"}
                or not parsed_endpoint.hostname
                or parsed_endpoint.username
                or parsed_endpoint.password
                or parsed_endpoint.query
                or parsed_endpoint.fragment
            ):
                raise ValueError("Invalid authority replica URL")
            if parsed_endpoint.scheme == "http" and parsed_endpoint.hostname not in {
                "localhost",
                "127.0.0.1",
                "::1",
            }:
                raise ValueError("LAN authority replicas require TLS")
        parsed = urlsplit(self.url)
        if (
            parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.scheme not in {"http", "https"}
            or parsed.scheme == "http"
            and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        ):
            raise ValueError(
                "Coordinator URL must use TLS outside loopback and contain no credentials"
            )
        token = environment_values().get(spec["token_env"])
        if not token:
            raise ValueError("Existing coordinator credential environment is missing")
        self.ttl = spec.get("ttl", 10)
        if type(self.ttl) not in (int, float) or not 2 <= self.ttl <= 60:
            raise ValueError("Coordinator TTL must be 2–60 seconds")
        self.http = httpx.Client(
            timeout=2,
            verify=ssl.create_default_context(cafile=spec["ca"])
            if spec.get("ca")
            else True,
            trust_env=False,
            follow_redirects=False,
            headers={
                "X-IATest-Controller": spec["id"],
                "Authorization": "Bearer " + token,
            },
        )

    @classmethod
    def configured(cls, root):
        if root is None:
            return None
        from iatest.core.config import load_config

        spec = load_config(root).get("resource_coordinator", {})
        return cls(spec) if spec else None

    def call(self, action, value):
        response = None
        for endpoint in self.endpoints:
            try:
                response = self.http.post(
                    endpoint.rstrip("/") + "/leases/" + action, json=value
                )
                break
            except httpx.HTTPError:
                if endpoint == self.endpoints[-1]:
                    raise
        if response.status_code != 200:
            raise ValueError(
                "Resource authority rejected operation: " + response.text[:500]
            )
        value = response.json()
        if (
            self.spec.get("authority_id")
            and value.get("authority_id") != self.spec["authority_id"]
        ):
            raise ValueError("Resource authority identity mismatch; failover rejected")
        return value

    def close(self):
        self.http.close()


class LeaseGuard:
    def __init__(self, client, resources, control=None):
        self.client, self.resources = client, sorted(set(resources))
        message = {
            "id": "lease-" + uuid.uuid4().hex,
            "resources": self.resources,
            "ttl": client.ttl,
        }
        wait = client.spec.get("wait_seconds", 0)
        if type(wait) not in (int, float) or not 0 <= wait <= 60:
            raise ValueError("Resource wait must be 0–60 seconds")
        deadline = time.monotonic() + wait
        while True:
            began = time.monotonic()
            if control:
                control.check()
            try:
                value = client.call("acquire", message)
                break
            except ValueError as exc:
                if (
                    "Resource is occupied" not in str(exc)
                    or time.monotonic() >= deadline
                ):
                    raise
                if control:
                    control.wait(min(0.05, deadline - time.monotonic()))
                else:
                    time.sleep(0.05)
        self.expires_at = began + client.ttl
        self.value = {
            "id": value["id"],
            "fences": json.loads(value["fences"]),
            "ttl": client.ttl,
        }
        self.healthy, self.safe, self.closed, self.uncertain = True, False, False, False
        self.stop = threading.Event()
        self.thread = threading.Thread(
            target=self.renew, daemon=True, name="iatest-resource-lease"
        )
        self.thread.start()

    def renew(self):
        while not self.stop.wait(min(0.5, self.client.ttl / 3)):
            try:
                began = time.monotonic()
                self.client.call("renew", self.value)
                self.expires_at = began + self.client.ttl
            except (ValueError, httpx.HTTPError):
                self.healthy = False
                return

    def alive(self):
        return self.healthy and not self.closed and time.monotonic() < self.expires_at

    def descriptor(self):
        return {
            **self.value,
            "url": self.client.url,
            "controller_id": self.client.spec["id"],
        }

    def close(self):
        if self.closed:
            return "already_closed"
        self.closed = True
        self.stop.set()
        self.thread.join(timeout=3)
        try:
            release = self.safe and self.healthy and not self.uncertain
            self.client.call("release" if release else "uncertain", self.value)
            return "released" if release else "uncertain"
        except (ValueError, httpx.HTTPError):
            return "unconfirmed"
        finally:
            self.client.close()


def declared_resources(root, project, plan):
    from dataclasses import asdict

    from iatest.mobile.store import MobileStore

    resources = set()

    def visit(value):
        # Only follow plan structure; payload/data records may contain these same keys.
        resources.update(value.get("resources", []))
        spec, kind = value["spec"], value["kind"]
        if kind == "desktop":
            resources.add(f"desktop:{spec['platform']}:{spec['process_id']}")
        profile_id = value.get("preparation", {}).get("app", {}).get("profile")
        if kind in {"android", "ios"}:
            profile_id = spec["profile"]
        if profile_id:
            try:
                profile = MobileStore(root, project).get("profile", profile_id)
                resources.add(
                    "device:" + profile["platform"] + ":" + profile["device_id"]
                )
            except ValueError:
                pass  # Worker-only profiles must be covered by its enrolled device resource.
        node_ids = (
            [spec["node_id"]]
            if kind == "remote"
            else spec.get("distribution", {}).get("nodes", [])
            if kind == "performance"
            else []
        )
        if node_ids:
            from iatest.nodes.store import ControllerStore

            for node in ControllerStore(root, project).nodes():
                if node["id"] in node_ids:
                    resources.update(node["profile"].get("resources", []))
        if kind == "workflow":
            for node in spec["nodes"]:
                resources.update(node.get("resources", []))
                visit(node["plan"])
        elif kind in {"remote", "client_performance"} and "plan" in spec:
            visit(spec["plan"])

    visit(asdict(plan))
    return sorted(resources)


@contextmanager
def global_scope(root, project, plan, control):
    client = CoordinatorClient.configured(root)

    class Ticket:
        result = None

        def finish(self, result):
            self.result = result
            if (
                result.verdict not in {"passed", "failed"}
                or result.lifecycle != "completed"
            ):
                for guard in HELD.get().values():
                    guard.uncertain = True

    ticket = Ticket()
    if client is None:
        delegated_resources = {r for d in DELEGATED.get() for r in d["fences"]}
        if not set(plan.resources) <= delegated_resources:
            raise ValueError(
                "Explicit shared resources require a configured authority or a verified parent delegation"
            )
        yield ticket
        return
    required = set(declared_resources(root, project, plan))
    delegated = {
        r for d in DELEGATED.get() if d["url"] == client.url for r in d["fences"]
    }
    missing = required - set(HELD.get()) - delegated
    guard = None
    prior = control.cancellation_check
    token = None
    try:
        if missing:
            guard = LeaseGuard(client, missing, control)
            token = HELD.set({**HELD.get(), **dict.fromkeys(missing, guard)})
            control.cancellation_check = lambda: (
                not guard.alive() or bool(prior and prior())
            )
        else:
            client.close()
        yield ticket
        if (
            guard
            and ticket.result
            and ticket.result.verdict in {"passed", "failed"}
            and ticket.result.lifecycle == "completed"
        ):
            guard.safe = True
    finally:
        if guard:
            state = guard.close()
            if ticket.result:
                ticket.result.metrics["global_resources"] = {
                    "lease": guard.descriptor(),
                    "state": state,
                }
                if state != "released" and ticket.result.verdict == "passed":
                    ticket.result.verdict = "inconclusive"
                    ticket.result.limitations.append(
                        "Global resource release/authority could not be confirmed"
                    )
            control.cancellation_check = prior
        else:
            client.close()
        if token is not None:
            HELD.reset(token)


def held_descriptors():
    return [g.descriptor() for g in {id(v): v for v in HELD.get().values()}.values()]


def device_guard(root, profile):
    client = CoordinatorClient.configured(root)
    if client is None:
        return None
    resource = "device:" + profile["platform"] + ":" + profile["device_id"]
    if resource in HELD.get():
        client.close()
        return None
    try:
        return LeaseGuard(client, [resource])
    except BaseException:
        client.close()
        raise
