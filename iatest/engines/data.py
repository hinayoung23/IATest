"""Variable references for native plans, using the existing bounded generator."""

from __future__ import annotations

import copy
import os
import re
import secrets
import uuid
from dataclasses import replace

from iatest.execution.data_generation import generate_values

DATA_PROPERTIES = {
    "data_generation": {
        "type": "object",
        "maxProperties": 32,
        "additionalProperties": {"type": "object"},
    },
    "input_env": {
        "type": "object",
        "maxProperties": 32,
        "additionalProperties": {
            "type": "string",
            "pattern": "^[A-Za-z_][A-Za-z0-9_]*$",
        },
    },
    "data_seed": {"type": ["integer", "null"]},
}


def reference_text(value):
    variable = r"\{\{[A-Za-z_][A-Za-z0-9_]*\}\}"
    return isinstance(value, str) and bool(
        re.fullmatch(r"(?:(?:Bearer|Basic) )?" + variable, value)
        or re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_-]*="
            + variable
            + r"(?:;\s*[A-Za-z_][A-Za-z0-9_-]*="
            + variable
            + r")*",
            value,
        )
    )


def redact_asset(value):
    """Preserve validated variable definitions; these contain references, not env values."""
    from iatest.core.redaction import redact
    from iatest.core.schema import schema_errors

    definitions = []

    def protect(item, path=()):
        if isinstance(item, dict):
            item = {k: protect(v, (*path, k)) for k, v in item.items()}
            for key, child in list(item.items()):
                reference = isinstance(child, dict) and (
                    (
                        set(child) in ({"$env"}, {"$value"})
                        and isinstance(next(iter(child.values())), str)
                    )
                    or (
                        set(child) == {"$from", "type", "sensitive"}
                        and isinstance(child["$from"], str)
                        and type(child["sensitive"]) is bool
                    )
                )
                template = reference_text(child)
                extraction = (
                    key == "extract"
                    and isinstance(child, dict)
                    and all(
                        isinstance(v, str) and v.startswith("$") for v in child.values()
                    )
                )
                exports = (
                    key == "exports"
                    and isinstance(child, dict)
                    and all(
                        isinstance(v, dict)
                        and set(v) <= {"path", "type", "sensitive"}
                        and isinstance(v.get("path"), str)
                        and v["path"].startswith("$")
                        for v in child.values()
                    )
                )
                if reference or template or extraction or exports:
                    definitions.append(((*path, key), item.pop(key)))
            if "kind" in item and "spec" in item or "input_env" in item:
                for key in (
                    ("input_env", "data_generation")
                    if "kind" in item and "spec" in item
                    else ("input_env",)
                ):
                    if key in item and not schema_errors(
                        item[key], DATA_PROPERTIES[key]
                    ):
                        definitions.append(((*path, key), item.pop(key)))
            return item
        if isinstance(item, list):
            return [protect(v, (*path, i)) for i, v in enumerate(item)]
        return item

    clean = redact(protect(copy.deepcopy(value)))
    for path, definition in sorted(definitions, key=lambda item: len(item[0])):
        destination = clean
        for key in path[:-1]:
            destination = destination[key]
        destination[path[-1]] = definition
    return clean


def declared_secrets(value):
    if isinstance(value, dict):
        if (
            set(value) == {"$env"}
            and isinstance(value["$env"], str)
            and value["$env"] in os.environ
        ):
            yield os.environ[value["$env"]]
        bindings = value.get("input_env", {})
        for env_name in bindings.values() if isinstance(bindings, dict) else ():
            if isinstance(env_name, str) and env_name in os.environ:
                yield os.environ[env_name]
        for child in value.values():
            yield from declared_secrets(child)
    elif isinstance(value, list):
        for child in value:
            yield from declared_secrets(child)


def bind_values(value, variables, deferred=frozenset()):
    if isinstance(value, dict):
        if "$value" in value:
            if not isinstance(value["$value"], str):
                raise ValueError("Data variable names must be strings")
            if set(value) == {"$value"} and value["$value"] in deferred:
                return dict(value)
            if set(value) != {"$value"} or value["$value"] not in variables:
                raise ValueError("Unknown or malformed data variable reference")
            return variables[value["$value"]]
        if "kind" in value and "spec" in value:
            deferred = (
                deferred
                | set(value.get("data_generation", {}))
                | set(value.get("input_env", {}))
            )
        return {k: bind_values(v, variables, deferred) for k, v in value.items()}
    if isinstance(value, list):
        return [bind_values(v, variables, deferred) for v in value]
    return value


def plan_values(plan, *, preview=False, namespace=""):
    namespace = namespace or uuid.uuid4().hex
    seed = plan.data_seed if plan.data_seed is not None else secrets.randbits(64)
    names = set(plan.data_generation) | set(plan.input_env)
    if set(plan.data_generation) & set(plan.input_env) or any(
        not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", n) for n in names
    ):
        raise ValueError("Invalid or duplicate data binding name")
    variables = generate_values(plan.data_generation, seed, plan.id, namespace)
    hidden = []
    for name, env_name in plan.input_env.items():
        if not preview and env_name not in os.environ:
            raise ValueError(
                "Referenced input environment variable is missing: " + env_name
            )
        variables[name] = "credential-preview" if preview else os.environ[env_name]
        hidden.append(variables[name])
    metadata = {
        "namespace": namespace,
        "seed": seed,
        "variables": {
            k: "[REDACTED]" if k in plan.input_env else v for k, v in variables.items()
        },
    }
    return variables, metadata, hidden


def materialize(plan, *, preview=False, namespace=""):
    values, metadata, hidden = plan_values(plan, preview=preview, namespace=namespace)
    return (
        replace(
            plan,
            spec=bind_values(plan.spec, values),
            data_generation={},
            input_env={},
            data_seed=None,
        ),
        metadata,
        hidden,
    )
