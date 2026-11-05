from __future__ import annotations

import os
import tempfile
from pathlib import Path


def private_permissions(path: Path) -> None:
    """User-only POSIX permissions or an explicit current-user Windows ACL."""
    if os.name != "nt":
        path.chmod(0o700 if path.is_dir() else 0o600)
        return
    import csv
    import subprocess

    output = subprocess.check_output(["whoami", "/user", "/fo", "csv", "/nh"], encoding="utf-8", errors="replace", timeout=5)
    sid = next(csv.reader(output.splitlines()))[-1]
    if not sid.startswith("S-1-") or any(char not in "S-0123456789" for char in sid):
        raise PermissionError("Unable to determine the current Windows user SID")
    grant = f"*{sid}:" + ("(OI)(CI)(F)" if path.is_dir() else "(F)")
    subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", grant], check=True, capture_output=True, timeout=5)


def atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8", private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode if path.exists() else None
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding=encoding) as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        if private:
            private_permissions(temporary)
        elif mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode if path.exists() else None
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
