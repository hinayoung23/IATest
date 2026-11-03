from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager

from iatest.application.engine_analysis import digest
from iatest.core.paths import project_runtime_dir
from iatest.core.redaction import redact
from iatest.engines.store import identifier

PROTOCOL = 1


def plan_tree(plan):
    yield plan
    if plan["kind"] == "workflow":
        for node in plan["spec"]["nodes"]:
            yield from plan_tree(node["plan"])
    elif plan["kind"] in {"client_performance", "remote"} and "plan" in plan["spec"]:
        yield from plan_tree(plan["spec"]["plan"])


def require_credential_references(value):
    from iatest.core.redaction import sensitive_key
    from iatest.engines.data import reference_text

    def visit(item, definitions=False):
        if isinstance(item, dict):
            if set(item) in ({"$env"}, {"$value"}):
                return
            for key, child in item.items():
                if key in {
                    "input_env",
                    "data_generation",
                    "schema",
                    "json_schema",
                    "extract",
                }:
                    continue
                if (
                    sensitive_key(key)
                    and isinstance(child, str)
                    and not reference_text(child)
                ):
                    raise ValueError(
                        "Remote credentials must use input_env/$value or extracted transaction variables"
                    )
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)


def engine_fingerprint():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    return digest(
        {
            p.relative_to(root).as_posix(): __import__("hashlib")
            .sha256(p.read_bytes())
            .hexdigest()
            for p in sorted(root.rglob("*"))
            if p.suffix
            in {
                ".py",
                ".js",
                ".java",
                ".m",
                ".h",
                ".html",
                ".css",
                ".plist",
                ".pbxproj",
                ".entitlements",
                ".xml",
                ".gradle",
                ".swift",
                ".ps1",
                ".json",
            }
        }
    )


def environment_fingerprint(root):
    from iatest.core.config import load_config

    config = load_config(root)
    return digest(
        {
            "environment": config.get("testing", {}).get("environment", {}),
            "name": config.get("default_environment", ""),
        }
    )


def input_proof(value):
    refs = {}

    def visit(item):
        if isinstance(item, dict):
            if set(item) == {"$env"}:
                refs[item["$env"]] = os.environ.get(item["$env"])
            for env in item.get("input_env", {}).values():
                refs[env] = os.environ.get(env)
            for key in ("path_env", "username_env", "password_env"):
                if key in item:
                    refs[item[key]] = os.environ.get(item[key])
            for v in item.values():
                visit(v)
        elif isinstance(item, list):
            for v in item:
                visit(v)

    visit(value)
    if any(v is None for v in refs.values()):
        raise ValueError("Worker input environment is missing a referenced value")
    return digest(refs)


class ControllerStore:
    def __init__(self, root, project):
        self.root, self.project = root, project
        self.path = project_runtime_dir(root, project) / "nodes.sqlite3"

    @contextmanager
    def connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("pragma journal_mode=WAL")
            conn.executescript("""
              create table if not exists nodes(id text primary key,profile text not null,capabilities text not null default '{}',instance text not null default '',seen real not null default 0);
              create table if not exists jobs(id text primary key,run_id text,node_id text,plan text not null,state text not null,instance text not null,created real not null,updated real not null,start real,result text,error text not null default '',proof text not null);
              create table if not exists sample_chunks(job_id text,seq integer,content text not null,hash text not null,primary key(job_id,seq));
              create table if not exists task_assets(job_id text,kind text,id text,manifest text not null,path text not null,content blob,primary key(job_id,kind,id));
              create table if not exists resource_claims(resource text primary key,job_id text not null,node_id text not null,instance text not null,state text not null,updated real not null);
              create table if not exists reconciliations(job_id text primary key,note text not null,created real not null);
              create table if not exists job_global_leases(job_id text primary key,descriptors text not null);
            """)
            yield conn
            conn.commit()
        finally:
            conn.close()

    def enroll(self, profile):
        from iatest.engines.http import validate

        validate(
            profile,
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "token_env", "allowed_origins"],
                "properties": {
                    "id": {"type": "string", "pattern": "^[A-Za-z0-9_.-]{1,80}$"},
                    "token_env": {
                        "type": "string",
                        "pattern": "^[A-Za-z_][A-Za-z0-9_]*$",
                    },
                    "allowed_origins": {
                        "type": "array",
                        "maxItems": 32,
                        "items": {"type": "string"},
                    },
                    "allowed_kinds": {
                        "type": "array",
                        "minItems": 1,
                        "uniqueItems": True,
                        "items": {
                            "enum": [
                                "performance",
                                "security",
                                "api",
                                "web",
                                "android",
                                "ios",
                                "workflow",
                                "client_performance",
                                "data",
                                "protocol",
                                "accessibility",
                                "desktop",
                            ]
                        },
                    },
                    "resources": {
                        "type": "array",
                        "maxItems": 50,
                        "uniqueItems": True,
                        "items": {"type": "string", "minLength": 1, "maxLength": 200},
                    },
                },
            },
        )
        with self.connection() as conn:
            if conn.execute(
                "select 1 from jobs where node_id=? and state in ('pending','preparing','ready','running')",
                (profile["id"],),
            ).fetchone():
                raise ValueError("Cannot change enrollment while a node owns a task")
            conn.execute(
                "insert into nodes(id,profile) values(?,?) on conflict(id) do update set profile=excluded.profile,seen=0,capabilities='{}',instance=''",
                (identifier(profile["id"]), json.dumps(profile)),
            )

    def nodes(self):
        with self.connection() as conn:
            rows = conn.execute("select * from nodes order by id").fetchall()
        return [
            {
                "id": r["id"],
                "profile": json.loads(r["profile"]),
                "capabilities": json.loads(r["capabilities"]),
                "instance": r["instance"],
                "last_seen": r["seen"],
                "online": time.time() - r["seen"] < 8,
            }
            for r in rows
        ]

    def authenticate(self, node_id, token):
        node = next((n for n in self.nodes() if n["id"] == node_id), None)
        expected = os.environ.get(node["profile"]["token_env"], "") if node else ""
        return (
            node
            if expected
            and len(expected) >= 16
            and secrets.compare_digest(expected, token)
            else None
        )

    def register(self, node_id, instance, capabilities):
        if capabilities.get("protocol") != PROTOCOL:
            raise ValueError("Node protocol mismatch")
        identifier(instance)
        with self.connection() as conn:
            node = conn.execute(
                "select instance from nodes where id=?", (node_id,)
            ).fetchone()
            if not node:
                raise ValueError("Enroll the node before registration")
            if node["instance"] and node["instance"] != instance:
                conn.execute(
                    "update resource_claims set state='uncertain',updated=? where node_id=?",
                    (time.time(), node_id),
                )
                conn.execute(
                    "update jobs set state='error',error='Worker restarted; prior side effects are not replayed',updated=? where node_id=? and state in ('pending','preparing','ready','running')",
                    (time.time(), node_id),
                )
            conn.execute(
                "update nodes set instance=?,capabilities=?,seen=? where id=?",
                (
                    instance,
                    json.dumps(capabilities, allow_nan=False),
                    time.time(),
                    node_id,
                ),
            )

    def touch(self, node_id, instance):
        with self.connection() as conn:
            cursor = conn.execute(
                "update nodes set seen=? where id=? and instance=?",
                (time.time(), node_id, instance),
            )
            if not cursor.rowcount:
                raise ValueError("Stale worker instance")
            conn.execute(
                "update resource_claims set updated=? where node_id=? and instance=? and state='held'",
                (time.time(), node_id, instance),
            )

    def enqueue(self, run_id, node_id, plan):
        import copy

        from iatest.engines.contracts import ExecutionPlan
        from iatest.engines.store import EngineStore
        from iatest.engines.validation import validate_plan

        validate_plan(ExecutionPlan.from_dict(plan))
        plan = copy.deepcopy(plan)
        for child in plan_tree(plan):
            if child["kind"] == "client_performance" and "measurement" in child["spec"]:
                binding = child["spec"]["measurement"]
                asset = EngineStore(self.root, self.project).get_artifact(
                    "ios_measurement", binding["artifact_id"]
                )
                binding.setdefault("source_digest", asset["source_digest"])
        require_credential_references(plan)
        node = next((n for n in self.nodes() if n["id"] == node_id), None)
        if (
            not node
            or not node["online"]
            or node["capabilities"].get("engine_fingerprint") != engine_fingerprint()
        ):
            raise ValueError("Node is offline or its engine fingerprint differs")
        if node["capabilities"].get(
            "environment_fingerprint"
        ) != environment_fingerprint(self.root):
            raise ValueError("Node declared environment differs from the controller")
        kinds = {p["kind"] for p in plan_tree(plan)}
        if not kinds <= set(
            node["profile"].get("allowed_kinds", ["performance"])
        ) or not kinds <= set(node["capabilities"].get("kinds", [])):
            raise ValueError("Node does not authorize/support this engine kind")
        identifier(run_id)
        job_id = "job-" + uuid.uuid4().hex[:24]
        proof = input_proof(plan)
        from iatest.nodes.assets import freeze_assets

        assets = freeze_assets(self.root, self.project, plan)
        from iatest.nodes.coordinator import held_descriptors

        descriptors = held_descriptors()
        from iatest.core.config import load_config

        if load_config(self.root).get("resource_coordinator"):
            if not descriptors:
                from iatest.nodes.coordinator import CoordinatorClient

                client = CoordinatorClient.configured(self.root)
                descriptors = [
                    {
                        "url": client.url,
                        "controller_id": client.spec["id"],
                        "id": "policy",
                        "fences": {},
                        "ttl": client.ttl,
                    }
                ]
                client.close()
            required = set(node["profile"].get("resources", [])) | {
                r for p in plan_tree(plan) for r in p.get("resources", [])
            }
            if not required <= {r for d in descriptors for r in d["fences"]}:
                raise ValueError(
                    "Remote resources must be acquired by the controller before enqueue"
                )
        with self.connection() as conn:
            conn.execute("begin immediate")
            if conn.execute(
                "select 1 from jobs where node_id=? and state in ('pending','preparing','ready','running')",
                (node_id,),
            ).fetchone():
                raise ValueError("Node already owns a task")
            conn.execute(
                "insert into jobs(id,run_id,node_id,plan,state,instance,created,updated,proof) values(?,?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    run_id,
                    node_id,
                    json.dumps(plan, allow_nan=False),
                    "pending",
                    node["instance"],
                    time.time(),
                    time.time(),
                    proof,
                ),
            )
            for resource in sorted(node["profile"].get("resources", [])):
                if conn.execute(
                    "select 1 from resource_claims where resource=?", (resource,)
                ).fetchone():
                    raise ValueError(
                        "Controller resource is held or awaits reconciliation: "
                        + resource
                    )
                conn.execute(
                    "insert into resource_claims values(?,?,?,?,?,?)",
                    (resource, job_id, node_id, node["instance"], "held", time.time()),
                )
            conn.execute(
                "insert into job_global_leases values(?,?)",
                (job_id, json.dumps(descriptors)),
            )
            for asset in assets:
                manifest = {
                    k: v for k, v in asset.items() if k not in {"path", "content"}
                }
                conn.execute(
                    "insert into task_assets values(?,?,?,?,?,?)",
                    (
                        job_id,
                        asset["kind"],
                        asset["id"],
                        json.dumps(manifest, allow_nan=False),
                        asset["path"],
                        asset["content"],
                    ),
                )
        return job_id

    def job(self, job_id):
        identifier(job_id)
        with self.connection() as conn:
            row = conn.execute("select * from jobs where id=?", (job_id,)).fetchone()
        if not row:
            raise ValueError("Node task not found")
        value = dict(row)
        value["plan"] = json.loads(value["plan"])
        value["result"] = json.loads(value["result"]) if value["result"] else None
        with self.connection() as conn:
            binding = conn.execute(
                "select descriptors from job_global_leases where job_id=?", (job_id,)
            ).fetchone()
            value["global_leases"] = json.loads(binding[0]) if binding else []
            value["assets"] = [
                json.loads(r[0])
                for r in conn.execute(
                    "select manifest from task_assets where job_id=? order by kind,id",
                    (job_id,),
                )
            ]
        return value

    def global_authority_alive(self, job):
        from iatest.nodes.coordinator import CoordinatorClient

        if not job.get("global_leases"):
            return True
        client = CoordinatorClient.configured(self.root)
        if client is None:
            return False
        try:
            for binding in job["global_leases"]:
                if (
                    binding["url"] != client.url
                    or binding["controller_id"] != client.spec["id"]
                ):
                    return False
                if binding["fences"]:
                    client.call(
                        "validate", {k: binding[k] for k in ("id", "fences", "ttl")}
                    )
            return True
        except (ValueError, RuntimeError, __import__("httpx").HTTPError):
            return False
        finally:
            client.close()

    def asset(self, job_id, kind, asset_id, offset):
        from iatest.nodes.assets import asset_chunk

        with self.connection() as conn:
            row = conn.execute(
                "select manifest,path,content from task_assets where job_id=? and kind=? and id=?",
                (job_id, kind, asset_id),
            ).fetchone()
        if not row:
            raise ValueError("Task did not authorize this asset")
        return asset_chunk(
            {
                "manifest": json.loads(row["manifest"]),
                "path": row["path"],
                "content": row["content"],
            },
            offset,
        )

    def claims(self):
        with self.connection() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "select * from resource_claims order by resource"
                )
            ]

    def reconcile(self, job_id, note, stopped=False):
        if (
            stopped is not True
            or not isinstance(note, str)
            or not 10 <= len(note.strip()) <= 2000
        ):
            raise ValueError(
                "Reconciliation requires explicit stopped acknowledgement and a 10–2000 character evidence note"
            )
        with self.connection() as conn:
            conn.execute("begin immediate")
            row = conn.execute(
                "select state from jobs where id=?", (job_id,)
            ).fetchone()
            if not row or row["state"] not in {"cancelled", "error"}:
                raise ValueError("Only a terminal uncertain task may be reconciled")
            conn.execute(
                "insert into reconciliations values(?,?,?)",
                (job_id, str(redact(note)), time.time()),
            )
            conn.execute("delete from resource_claims where job_id=?", (job_id,))

    def poll(self, node_id, instance):
        self.touch(node_id, instance)
        with self.connection() as conn:
            conn.execute("begin immediate")
            row = conn.execute(
                "select id from jobs where node_id=? and instance=? and state in ('pending','preparing','ready','running') order by created limit 1",
                (node_id, instance),
            ).fetchone()
            if row:
                conn.execute(
                    "update jobs set state='preparing',updated=? where id=? and state='pending'",
                    (time.time(), row["id"]),
                )
        return self.job(row["id"]) if row else None

    def ready(self, job_id, proof):
        with self.connection() as conn:
            row = conn.execute(
                "select proof,state from jobs where id=?", (job_id,)
            ).fetchone()
            if not row or row["proof"] != proof:
                raise ValueError(
                    "Worker credentials differ from controller task inputs"
                )
            if row["state"] not in {"preparing", "ready"}:
                raise ValueError("Task is not in preparation")
            conn.execute(
                "update jobs set state='ready',updated=? where id=?",
                (time.time(), job_id),
            )

    def start(self, job_ids, *, delay=1):
        epoch = time.time() + delay
        with self.connection() as conn:
            conn.execute("begin immediate")
            for job_id in job_ids:
                if (
                    conn.execute(
                        "select state from jobs where id=?", (job_id,)
                    ).fetchone()[0]
                    != "ready"
                ):
                    raise ValueError("Every selected node must be ready before start")
            for job_id in job_ids:
                conn.execute(
                    "update jobs set state='running',start=?,updated=? where id=?",
                    (epoch, time.time(), job_id),
                )
        return epoch

    def cancel(self, job_ids, reason="Controller cancelled this task"):
        with self.connection() as conn:
            for job_id in job_ids:
                row = conn.execute(
                    "select state from jobs where id=?", (job_id,)
                ).fetchone()
                if row and row["state"] in {"pending", "preparing", "ready"}:
                    conn.execute(
                        "delete from resource_claims where job_id=?", (job_id,)
                    )
                else:
                    conn.execute(
                        "update resource_claims set state='uncertain',updated=? where job_id=?",
                        (time.time(), job_id),
                    )
                conn.execute(
                    "update jobs set state='cancelled',error=?,updated=? where id=? and state not in ('completed','error')",
                    (reason, time.time(), job_id),
                )

    def fail(self, job_id, error):
        with self.connection() as conn:
            row = conn.execute(
                "select state from jobs where id=?", (job_id,)
            ).fetchone()
            if row and row["state"] in {"pending", "preparing", "ready"}:
                conn.execute("delete from resource_claims where job_id=?", (job_id,))
            else:
                conn.execute(
                    "update resource_claims set state='uncertain',updated=? where job_id=?",
                    (time.time(), job_id),
                )
            conn.execute(
                "update jobs set state='error',error=?,updated=? where id=? and state not in ('cancelled','completed')",
                (str(redact(error))[:2000], time.time(), job_id),
            )

    def samples(self, job_id):
        with self.connection() as conn:
            rows = conn.execute(
                "select seq,content from sample_chunks where job_id=? order by seq",
                (job_id,),
            ).fetchall()
        if [r["seq"] for r in rows] != list(range(len(rows))):
            raise ValueError("Node sample chunks are incomplete")
        return [v for row in rows for v in json.loads(row["content"])]

    def put_samples(self, job_id, sequence, rows):
        import math

        if (
            type(sequence) is not int
            or not 0 <= sequence < 200
            or not isinstance(rows, list)
            or not 1 <= len(rows) <= 1000
        ):
            raise ValueError("Invalid sample chunk bounds")
        for row in rows:
            if (
                row.get("kind") not in {"request", "transaction"}
                or type(row.get("duration_ms")) not in (int, float)
                or not math.isfinite(row["duration_ms"])
                or row["duration_ms"] < 0
                or type(row.get("ok")) is not bool
            ):
                raise ValueError("Invalid raw performance sample")
        encoded, fingerprint = json.dumps(rows, allow_nan=False), digest(rows)
        with self.connection() as conn:
            old = conn.execute(
                "select hash from sample_chunks where job_id=? and seq=?",
                (job_id, sequence),
            ).fetchone()
            if old:
                if old["hash"] != fingerprint:
                    raise ValueError(
                        "Sample sequence already contains different evidence"
                    )
            else:
                state = conn.execute(
                    "select state from jobs where id=?", (job_id,)
                ).fetchone()
                if not state or state[0] not in {"running", "cancelled"}:
                    raise ValueError("Samples require an executing or cancelling task")
                conn.execute(
                    "insert into sample_chunks values(?,?,?,?)",
                    (job_id, sequence, encoded, fingerprint),
                )

    def complete(self, job_id, result, samples_digest):
        from iatest.engines.contracts import EngineResult

        if len(json.dumps(result, allow_nan=False).encode()) > 4194304:
            raise ValueError("Node result exceeds 4 MiB")
        parsed = EngineResult(**result)
        if parsed.verdict not in {
            "passed",
            "failed",
            "inconclusive",
            "not_evaluated",
        } or not isinstance(parsed.metrics, dict):
            raise ValueError("Invalid node verdict/metrics")
        if digest(self.samples(job_id)) != samples_digest:
            raise ValueError("Node sample digest mismatch")
        encoded = json.dumps(redact(result), allow_nan=False)
        with self.connection() as conn:
            row = conn.execute(
                "select state,result,plan from jobs where id=?", (job_id,)
            ).fetchone()
            if row["state"] == "completed":
                if row["result"] != encoded:
                    raise ValueError(
                        "Completed task result differs; execution is never replayed"
                    )
                return
            if row["state"] not in {"running", "cancelled"}:
                raise ValueError("Task is not executing")
            plan = json.loads(row["plan"])
            if result["kind"] != plan["kind"] or result["plan_id"] != plan["id"]:
                raise ValueError("Node result names a different plan")
            conn.execute(
                "update jobs set state=?,result=?,updated=? where id=?",
                (
                    "completed" if row["state"] != "cancelled" else "cancelled",
                    encoded,
                    time.time(),
                    job_id,
                ),
            )
            if (
                row["state"] == "running"
                and parsed.lifecycle == "completed"
                and parsed.verdict in {"passed", "failed"}
            ):
                conn.execute("delete from resource_claims where job_id=?", (job_id,))
            else:
                conn.execute(
                    "update resource_claims set state='uncertain',updated=? where job_id=?",
                    (time.time(), job_id),
                )
