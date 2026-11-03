"""Frozen descriptors and dynamic messages; never import generated Python code."""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

DESCRIPTOR_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["descriptor_set_base64", "sha256"],
    "properties": {
        "descriptor_set_base64": {"type": "string", "maxLength": 1398104},
        "sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
    },
}


def compile_sources(files):
    if not isinstance(files, dict) or not 1 <= len(files) <= 64:
        raise ValueError("Provide 1–64 named proto sources/imports")
    total = 0
    with tempfile.TemporaryDirectory(prefix="iatest-proto-input-") as tmp:
        for name, content in files.items():
            relative = Path(name)
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or name.startswith("-")
                or relative.suffix != ".proto"
                or not isinstance(content, str)
            ):
                raise ValueError("Invalid proto filename/content")
            total += len(content.encode())
            if total > 1048576:
                raise ValueError("Proto sources exceed 1 MiB")
            path = Path(tmp) / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return compile_proto(tmp, list(files))


def compile_proto(directory, files, *, executable=None):
    """Compile an explicitly selected, bounded source tree to a frozen asset."""
    root = Path(directory).resolve(strict=True)
    if not root.is_dir() or not 1 <= len(files) <= 64:
        raise ValueError("Select a proto directory and 1–64 source files")
    compiler = executable or os.environ.get("IATEST_PROTOC") or shutil.which("protoc")
    if not compiler:
        raise ValueError("Install protoc or configure IATEST_PROTOC")
    with tempfile.TemporaryDirectory(prefix="iatest-proto-") as tmp:
        stage = Path(tmp)
        total = 0
        names = []
        # Only declared files are visible to the compiler, including imports.
        for name in files:
            relative = Path(name)
            source = root / relative
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or name.startswith("-")
                or source.is_symlink()
            ):
                raise ValueError("Proto source escaped the selected directory")
            if (
                not source.resolve(strict=True).is_relative_to(root)
                or source.suffix != ".proto"
            ):
                raise ValueError("Select regular .proto sources")
            content = source.read_bytes()
            total += len(content)
            if total > 1048576:
                raise ValueError("Proto sources exceed 1 MiB")
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            names.append(relative.as_posix())
        output = stage / "descriptor.pb"
        result = subprocess.run(
            [
                str(compiler),
                "--proto_path=" + str(stage),
                "--include_imports",
                "--descriptor_set_out=" + str(output),
                *names,
            ],
            cwd=stage,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if result.returncode or not output.is_file() or output.stat().st_size > 1048576:
            raise ValueError("Proto compilation failed or descriptor quota exceeded")
        raw = output.read_bytes()
    return {
        "descriptor_set_base64": base64.b64encode(raw).decode(),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


class Messages:
    def __init__(self, spec, path):
        from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

        raw = base64.b64decode(spec["descriptor_set_base64"], validate=True)
        if (
            not 0 < len(raw) <= 1048576
            or hashlib.sha256(raw).hexdigest() != spec["sha256"]
        ):
            raise ValueError("Descriptor content/hash differs")
        try:
            definitions = descriptor_pb2.FileDescriptorSet.FromString(raw)
        except Exception as exc:
            raise ValueError("Malformed protobuf descriptor set") from exc
        if not 1 <= len(definitions.file) <= 128:
            raise ValueError("Descriptor file quota exceeded")
        self.pool = descriptor_pool.DescriptorPool()
        pending = list(definitions.file)
        while pending:
            rest = []
            for file in pending:
                try:
                    self.pool.Add(file)
                except (TypeError, ValueError):
                    rest.append(file)
            if len(rest) == len(pending):
                raise ValueError("Descriptor dependencies are missing or conflicting")
            pending = rest
        try:
            _, service, method = path.split("/")
            self.method = self.pool.FindServiceByName(service).methods_by_name[method]
        except (KeyError, ValueError) as exc:
            raise ValueError("RPC method absent from descriptor") from exc
        self.input = message_factory.GetMessageClass(self.method.input_type)
        self.output = message_factory.GetMessageClass(self.method.output_type)

    def encode(self, value):
        from google.protobuf.json_format import ParseDict

        try:
            message = ParseDict(value, self.input(), descriptor_pool=self.pool)
            if not message.IsInitialized():
                raise ValueError("Required protobuf fields are missing")
            data = message.SerializeToString(deterministic=True)
        except Exception as exc:  # descriptor errors must not leak payloads
            raise ValueError("Invalid protobuf request message") from exc
        if len(data) > 1048576:
            raise ValueError("Protobuf message exceeds 1 MiB")
        return data

    def decode(self, data):
        from google.protobuf.json_format import MessageToDict

        if len(data) > 1048576:
            raise ValueError("Protobuf message exceeds 1 MiB")
        try:
            message = self.output.FromString(data)
            if not message.IsInitialized():
                raise ValueError("Required protobuf fields are missing")
            return MessageToDict(
                message, preserving_proto_field_name=True, descriptor_pool=self.pool
            )
        except Exception as exc:
            raise ValueError("Invalid protobuf response message") from exc

    def expected(self, value):
        from google.protobuf.json_format import ParseDict

        try:
            message = ParseDict(value, self.output(), descriptor_pool=self.pool)
            return self.decode(message.SerializeToString(deterministic=True))
        except Exception as exc:
            raise ValueError("Invalid protobuf expected message") from exc
