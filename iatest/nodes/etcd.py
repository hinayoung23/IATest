"""Linearizable etcd transactions for replicated resource authorities."""

from __future__ import annotations
from iatest.core.env import environment_values

import base64
import json
import os
import sqlite3
import ssl
import time
from contextlib import contextmanager
from urllib.parse import urlsplit

import httpx

from iatest.nodes.coordinator import LeaseStore


class Conflict(RuntimeError):
    pass


class EtcdDocument:
    def __init__(self, spec):
        if set(spec) - {"endpoints", "cluster_id", "key", "token_env", "ca"}:
            raise ValueError("Unknown etcd setting")
        self.endpoints = spec["endpoints"]
        if not isinstance(self.endpoints, list) or not 1 <= len(self.endpoints) <= 7:
            raise ValueError("Declare 1–7 etcd endpoints")
        for endpoint in tuple(self.endpoints):
            p = urlsplit(endpoint)
            if (
                p.scheme not in {"http", "https"}
                or not p.hostname
                or p.username
                or p.password
                or p.query
                or p.fragment
                or p.path not in {"", "/"}
            ):
                raise ValueError("Invalid etcd endpoint")
            if p.scheme == "http" and p.hostname not in {
                "127.0.0.1",
                "localhost",
                "::1",
            }:
                raise ValueError("LAN etcd requires TLS")
        self.cluster = str(spec["cluster_id"])
        if not self.cluster.isdigit() or self.cluster == "0":
            raise ValueError("Pin the etcd cluster ID")
        key = spec.get("key", "/iatest/resource-authority")
        if not isinstance(key, str) or not key.startswith("/iatest/") or len(key) > 500:
            raise ValueError("Use a bounded /iatest/ namespace key")
        self.key = base64.b64encode(key.encode()).decode()
        headers = {}
        if spec.get("token_env"):
            token = environment_values().get(spec["token_env"])
            if not token:
                raise ValueError("Existing etcd authentication env is missing")
            headers["Authorization"] = token
        self.http = httpx.Client(
            timeout=1.5,
            trust_env=False,
            follow_redirects=False,
            headers=headers,
            verify=ssl.create_default_context(cafile=spec["ca"])
            if spec.get("ca")
            else True,
        )

    def call(self, path, value):
        error = None
        for endpoint in self.endpoints:
            try:
                with self.http.stream(
                    "POST", endpoint.rstrip("/") + path, json=value
                ) as response:
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 8388608:
                            raise ValueError("etcd response exceeds 8 MiB")
                    if response.status_code != 200:
                        raise ValueError("etcd rejected the transaction")
                data = json.loads(raw)
                if str(data.get("header", {}).get("cluster_id")) != self.cluster:
                    raise ValueError("etcd cluster identity mismatch")
                # Keep the reachable member first for the following CAS write.
                # Reprobing a dead first peer on both read and write can exceed
                # the controller's lease deadline even after quorum recovers.
                self.endpoints = [endpoint, *(p for p in self.endpoints if p != endpoint)]
                return data
            except httpx.HTTPError as exc:
                error = exc
        raise RuntimeError("No etcd quorum endpoint is reachable") from error

    def read(self):
        result = self.call("/v3/kv/range", {"key": self.key, "serializable": False})
        values = result.get("kvs", [])
        if not values:
            return None, 0
        return json.loads(base64.b64decode(values[0]["value"], validate=True)), int(
            values[0]["mod_revision"]
        )

    def write(self, value, revision):
        raw = json.dumps(value, separators=(",", ":"), allow_nan=False).encode()
        if len(raw) > 4194304:
            raise ValueError("Resource authority state exceeds 4 MiB")
        compare = {
            "key": self.key,
            "target": "MOD" if revision else "VERSION",
            "result": "EQUAL",
            "mod_revision" if revision else "version": str(revision),
        }
        result = self.call(
            "/v3/kv/txn",
            {
                "compare": [compare],
                "success": [
                    {
                        "request_put": {
                            "key": self.key,
                            "value": base64.b64encode(raw).decode(),
                        }
                    }
                ],
                "failure": [],
            },
        )
        if not result.get("succeeded"):
            raise Conflict("Concurrent authority state changed")


class EtcdLeaseStore(LeaseStore):
    def __init__(self, spec, policy_sha256, *, clock=time.time):
        self.document = EtcdDocument(spec)
        self.policy = policy_sha256
        self.clock = clock

    @contextmanager
    def connection(self):
        value, revision = self.document.read()
        if value and value.get("policy_sha256") != self.policy:
            raise ValueError(
                "Authority replicas use different identity/resource policies"
            )
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript("""
                create table leases(id text primary key,owner text,resources text,fences text,state text,expires real);
                create table claims(resource text primary key,lease_id text,fence integer);
                create table generations(resource text primary key,fence integer);
                create table audit(created real,owner text,lease_id text,action text,note text);
            """)
            for table, width in (
                ("leases", 6),
                ("claims", 3),
                ("generations", 2),
                ("audit", 5),
            ):
                for row in (value or {}).get("tables", {}).get(table, []):
                    if len(row) != width:
                        raise ValueError("Corrupt resource state")
                    conn.execute(
                        "insert into "
                        + table
                        + " values("
                        + ",".join("?" for _ in range(width))
                        + ")",
                        row,
                    )
            conn.execute(
                "update leases set state='uncertain' where state='active' and expires<?",
                (self.clock(),),
            )
            yield conn
            tables = {
                table: [list(row) for row in conn.execute("select * from " + table)]
                for table in ("leases", "claims", "generations", "audit")
            }
            tables["audit"] = tables["audit"][-10000:]
            self.document.write(
                {"policy_sha256": self.policy, "tables": tables}, revision
            )
        finally:
            conn.close()

    def operate(self, owner, action, value, allowed):
        for attempt in range(16):
            try:
                return super().operate(owner, action, value, allowed)
            except Conflict:
                if attempt == 15:
                    raise ValueError(
                        "Resource authority contention budget exhausted"
                    ) from None
                time.sleep(min(0.005 * (attempt + 1), 0.05))
