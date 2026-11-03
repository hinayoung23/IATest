"""Small, immutable declarative assets shared by local workers and remote nodes."""

from iatest.application.engine_analysis import digest
from iatest.engines.store import EngineStore


def document(kind, value):
    if kind == "security_library":
        return {
            **{
                k: v
                for k, v in value.items()
                if k not in {"id", "catalogue_id", "sha256"}
            },
            "id": value["catalogue_id"],
        }
    if kind == "accessibility_review":
        return {k: v for k, v in value.items() if k not in {"sha256", "trust"}}
    raise ValueError("Unsupported frozen declarative asset")


def verify(kind, value, expected):
    if value.get("sha256") != expected or digest(document(kind, value)) != expected:
        raise ValueError("Frozen declarative asset digest differs")


def selected(root, project, plan):
    from iatest.nodes.store import plan_tree

    found = {}
    for child in plan_tree(plan):
        spec = child["spec"]
        binding = None
        if child["kind"] == "security" and "scan" in spec:
            binding = (
                "security_library",
                spec["scan"]["library_id"],
                spec["scan"]["library_digest"],
            )
        if child["kind"] == "accessibility" and spec.get("audit", {}).get(
            "review_artifact"
        ):
            binding = (
                "accessibility_review",
                spec["audit"]["review_artifact"],
                spec["audit"]["review_digest"],
            )
        if binding:
            kind, name, expected = binding
            value = EngineStore(root, project).get_artifact(kind, name)
            verify(kind, value, expected)
            found[(kind, name)] = {"kind": kind, "id": name, "value": value}
    return list(found.values())


def install(root, project, entries):
    from iatest.engines.accessibility import import_review
    from iatest.engines.security_scan import import_library

    store = EngineStore(root, project)
    for entry in entries:
        kind, name, value = entry["kind"], entry["id"], entry["value"]
        verify(kind, value, value.get("sha256"))
        if value["id"] != name:
            raise ValueError("Frozen declarative asset identity differs")
        try:
            old = store.get_artifact(kind, name)
        except ValueError:
            result = (import_library if kind == "security_library" else import_review)(
                root, project, document(kind, value)
            )
            if result.get("artifact_id", result["id"]) != name:
                raise ValueError("Imported declarative asset identity differs")
        else:
            if old != value:
                raise ValueError("Asset identity already names different contents")
