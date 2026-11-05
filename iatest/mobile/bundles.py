"""Bounded split APK inspection and explicit existing-key AAB conversion."""

from __future__ import annotations
from iatest.core.env import environment_values

import hashlib
import os
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath

SIGNING_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["keystore_env", "store_password_env", "key_alias", "key_password_env"],
    "properties": {
        **{
            k: {"type": "string", "pattern": "^[A-Za-z_][A-Za-z0-9_]*$"}
            for k in ("keystore_env", "store_password_env", "key_password_env")
        },
        "key_alias": {"type": "string", "pattern": "^[A-Za-z0-9_.-]{1,100}$"},
    },
}


def extract_set(path, output):
    output = Path(output)
    total, files = 0, []
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) > 2048:
            raise ValueError("APK set entry quota exceeded")
        names = set()
        for member in entries:
            # ZIP names use POSIX syntax on every host. orig_filename retains
            # backslashes/NUL that ZipInfo may normalize on Windows.
            name = member.orig_filename
            relative = PurePosixPath(name)
            if (
                relative.is_absolute()
                or PureWindowsPath(name).anchor
                or ".." in relative.parts
                or any(c in name for c in ("\\", ":", "\x00"))
                or stat.S_ISLNK(member.external_attr >> 16)
                or name.casefold() in names
            ):
                raise ValueError("APK set contains unsafe/duplicate entries")
            names.add(name.casefold())
            total += member.file_size
            if total > 1073741824:
                raise ValueError("APK set decompression quota exceeded")
            if relative.suffix == ".apk":
                if len(files) >= 128:
                    raise ValueError("APK set exceeds 128 APKs")
                destination = output / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, destination.open("wb") as target:
                    copied = 0
                    while chunk := source.read(1048576):
                        copied += len(chunk)
                        if copied > member.file_size:
                            raise ValueError("APK entry size differs")
                        target.write(chunk)
                files.append(destination)
    if not files:
        raise ValueError("APK set contains no APKs")
    return files


def inspect_set(path, output, inspector):
    files = extract_set(path, output)
    metadata = [inspector(file, "android") for file in files]
    identity = lambda meta: tuple(
        meta.get(k) for k in ("app_id", "version_code", "certificate_sha256")
    ) + (tuple(meta.get("certificates", [])),)
    if len({identity(meta) for meta in metadata}) != 1:
        raise ValueError(
            "APK splits have different App identities, versions or signers"
        )
    bases = [meta for meta in metadata if not meta.get("split_name")]
    if not bases or len({meta.get("version", "") for meta in bases}) != 1:
        raise ValueError("APK set requires consistent base APK version metadata")
    base = dict(bases[0])
    # Bundletool config splits omit versionName. versionCode and signers remain
    # mandatory identity checks; take the display version from the base variants.
    if any(meta.get("version") and meta["version"] != base.get("version") for meta in metadata):
        raise ValueError("APK splits have inconsistent display versions")
    base["abis"] = sorted({abi for meta in metadata for abi in meta.get("abis", [])})
    base["minimum_os"] = str(
        min(int(meta.get("minimum_os", 0) or 0) for meta in bases)
    )
    base["apk_entries"] = [
        {
            "name": file.relative_to(output).as_posix(),
            "sha256": hashlib.sha256(file.read_bytes()).hexdigest(),
        }
        for file in files
    ]
    return base


def bundletool(*args, control=None):
    jar = environment_values().get("IATEST_BUNDLETOOL_JAR", "")
    java = environment_values().get("IATEST_JAVA") or (
        str(Path(environment_values()["JAVA_HOME"]) / "bin/java")
        if environment_values().get("JAVA_HOME")
        else shutil.which("java")
    )
    if not java or not Path(jar).is_file():
        raise ValueError("Configure existing Java and IATEST_BUNDLETOOL_JAR")
    from iatest.mobile.devices import command

    return command(
        [java, "-Xmx512m", "-jar", jar, *args],
        timeout=min(120, control.remaining() or 120) if control else 120,
        control=control,
    )


def convert_bundle(path, output, signing, control):
    required = [
        signing[key]
        for key in ("keystore_env", "store_password_env", "key_password_env")
    ]
    if any(not environment_values().get(key) for key in required):
        raise ValueError("Existing bundle signing environment references are missing")
    key = Path(environment_values()[signing["keystore_env"]]).resolve(strict=True)
    if not key.is_file():
        raise ValueError("Select an existing keystore; no signing keys are generated")
    # bundletool supports pass:/file:, not env:. Never put password values in argv.
    with tempfile.TemporaryDirectory(prefix="iatest-signing-") as private:
        passwords = []
        for name in ("store_password_env", "key_password_env"):
            value = environment_values()[signing[name]]
            if "\n" in value or "\r" in value or len(value.encode()) > 4096:
                raise ValueError("Signing password must be a bounded single line")
            destination = Path(private) / name
            descriptor = os.open(
                destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as target:
                target.write(value)
            passwords.append(destination)
        bundletool(
            "build-apks",
            "--bundle=" + str(path),
            "--output=" + str(output),
            "--mode=universal",
            "--ks=" + str(key),
            "--ks-key-alias=" + signing["key_alias"],
            "--ks-pass=file:" + str(passwords[0]),
            "--key-pass=file:" + str(passwords[1]),
            control=control,
        )
    if not Path(output).is_file() or Path(output).stat().st_size > 1073741824:
        raise ValueError("AAB conversion did not produce a bounded APK set")


def select_apks(path, output, device, control):
    # bundletool chooses ABI/density/language/module combinations for the exact device.
    spec = Path(output) / "device.json"
    selected = Path(output) / "selected"
    bundletool(
        "get-device-spec",
        "--device-id=" + device.profile["device_id"],
        "--output=" + str(spec),
        control=control,
    )
    bundletool(
        "extract-apks",
        "--apks=" + str(path),
        "--device-spec=" + str(spec),
        "--output-dir=" + str(selected),
        control=control,
    )
    files = sorted(selected.rglob("*.apk"))
    if not 1 <= len(files) <= 128 or any(p.is_symlink() for p in files):
        raise ValueError("No bounded device-compatible APK selection")
    return files
