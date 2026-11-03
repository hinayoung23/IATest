"""Explicit, byte-preserving launch isolation for the installed Xcode recorder."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from iatest.mobile.devices import command

MAX_TOOL_BYTES = 64 * 1024 * 1024


def _digest(path, control):
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            control.check()
            size += len(chunk)
            if size > MAX_TOOL_BYTES:
                raise ValueError("Installed xctrace exceeds the tool size budget")
            digest.update(chunk)
    if not size:
        raise ValueError("Installed xctrace is empty")
    return digest.hexdigest()


def installed_xctrace(control, *, runner=None):
    runner = runner or command
    source = Path(
        runner(["xcrun", "--find", "xctrace"], timeout=10, control=control).strip()
    ).resolve(strict=True)
    if (
        source.name != "xctrace"
        or tuple(source.parts[-4:-1]) != ("Developer", "usr", "bin")
        or not source.is_file()
        or source.stat().st_size > MAX_TOOL_BYTES
    ):
        raise ValueError("Recording requires the installed Xcode xctrace")
    return source


def prepare_frame_template(control, *, runner=None):
    """Use the installed empty template with only the two frame instruments."""
    source = installed_xctrace(control, runner=runner)
    developer = source.parents[2]
    template = (
        developer.parent
        / "Applications/Instruments.app/Contents/Packages/Base.instrdst"
        / "Contents/Templates/Blank.tracetemplate"
    )
    if not template.is_file() or not 0 < template.stat().st_size <= 1048576:
        raise ValueError(
            "The selected Xcode empty frame template is unavailable or oversized"
        )
    return template, developer, {
        "capture_template": "frame_lifetimes",
        "frame_capture_instruments": ["Hitches", "Frame Lifetimes"],
        "template_sha256": _digest(template, control),
    }


def prepare_isolated_xctrace(directory, control, *, runner=None):
    """Copy only an existing signed tool; never patch, re-sign, or download it."""
    runner = runner or command
    source = installed_xctrace(control, runner=runner)
    developer = source.parents[2]
    frameworks = [
        developer.parent / "Applications/Instruments.app/Contents/Frameworks",
        developer.parent / "SharedFrameworks",
    ]
    if not all(path.is_dir() for path in frameworks):
        raise ValueError("The selected Xcode framework directories are unavailable")
    runner(
        ["/usr/bin/codesign", "--verify", "--strict", str(source)],
        timeout=10,
        control=control,
    )
    expected = _digest(source, control)
    copied = Path(directory) / "xctrace-original-copy"
    created = False
    try:
        # Exclusive creation inside the recorder's private temporary directory.
        with source.open("rb") as incoming, copied.open("xb") as outgoing:
            created = True
            size = 0
            while chunk := incoming.read(1024 * 1024):
                control.check()
                size += len(chunk)
                if size > MAX_TOOL_BYTES:
                    raise ValueError("Installed xctrace changed beyond its size budget")
                outgoing.write(chunk)
        copied.chmod(0o500)
        if _digest(copied, control) != expected or _digest(source, control) != expected:
            raise ValueError("Installed xctrace changed while preparing the recorder")
        runner(
            ["/usr/bin/codesign", "--verify", "--strict", str(copied)],
            timeout=10,
            control=control,
        )
    except BaseException:
        # The recorder owns this directory and also cleans it on cancellation.
        if created:
            copied.chmod(0o600)
            copied.unlink(missing_ok=True)
        raise
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("DYLD_")
    }
    environment.update(
        DEVELOPER_DIR=str(developer),
        DYLD_FRAMEWORK_PATH=":".join(map(str, frameworks)),
        LC_ALL="C",
        LANG="en_US.UTF-8",
    )
    return (
        [str(copied)],
        environment,
        {
            "tool_launch": "isolated_copy",
            "tool_sha256": expected,
            "tool_copy_byte_identical": True,
            "tool_signature_verified": True,
        },
    )
