"""Explicit preparation of the project's scoped GNOME Shell adapter."""

from __future__ import annotations

import json
from pathlib import Path

from iatest.core.files import atomic_write_bytes


def prepare_wayland_helper(output: Path, *, overwrite=False):
    source = Path(__file__).with_name("desktop_native") / "gnome"
    metadata = json.loads((source / "metadata.json").read_text())
    destination = output.expanduser().resolve() / metadata["uuid"]
    if destination.is_symlink():
        raise ValueError("GNOME adapter destination must not be a symbolic link")
    files = {
        name: (source / name).read_bytes() for name in ("metadata.json", "extension.js")
    }
    for name, data in files.items():
        target = destination / name
        if target.is_symlink():
            raise ValueError("GNOME adapter files must not be symbolic links")
        if target.exists() and target.read_bytes() != data and not overwrite:
            raise ValueError(
                "Existing adapter differs; review it and explicitly use --overwrite"
            )
    destination.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        atomic_write_bytes(destination / name, data)
    return {
        "uuid": metadata["uuid"],
        "directory": str(destination),
        "shell_versions": metadata["shell-version"],
        "enabled": False,
        "enable_command": ["gnome-extensions", "enable", metadata["uuid"]],
    }
