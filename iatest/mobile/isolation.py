"""Explicit target-UID network isolation for authorized rooted Android test devices."""

from __future__ import annotations

import ipaddress
import re
import uuid

from iatest.execution.control import RunControl

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["backend", "allowed_endpoints"],
    "properties": {
        "backend": {"const": "android_root_firewall"},
        "root_backend": {"enum": ["su", "adb_root"]},
        "allowed_endpoints": {
            "type": "array",
            "maxItems": 64,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["ip", "port"],
                "properties": {
                    "ip": {"type": "string", "maxLength": 45},
                    "port": {"type": "integer", "minimum": 1, "maximum": 65535},
                },
            },
        },
    },
}


class DeviceNetworkIsolation:
    def __init__(self, device, profile=None):
        self.device = device
        self.profile = profile if profile is not None else device.profile
        self.settings = self.profile.get("isolation")
        self.chain = "IAT" + uuid.uuid4().hex[:16]
        self.created, self.jumps, self.expected = [], [], {}
        self.uid = None

    def run(self, *args):
        # Only generated chain names, numeric UID/ports and parsed literal IPs reach su.
        command = " ".join(str(arg) for arg in args)
        if not re.fullmatch(r"[A-Za-z0-9_.:/ =-]+", command):
            raise ValueError("Invalid generated device firewall command")
        if self.settings.get("root_backend", "su") == "adb_root":
            from iatest.mobile.emulator import require_owned

            if not self.profile.get("managed_emulator"):
                raise ValueError("ADB-root firewall backend requires an owned Android VM")
            require_owned(self.profile)
            return self.device.adb("shell", command, timeout=5).strip()
        return self.device.adb("shell", "su", "-c", command, timeout=5).strip()

    def open(self):
        if not self.settings:
            return
        if self.profile["platform"] != "android":
            raise ValueError(
                "Target UID firewall requires an explicitly authorized rooted Android test device"
            )
        endpoints = [
            (ipaddress.ip_address(p["ip"]), p["port"])
            for p in self.settings["allowed_endpoints"]
        ]
        if self.run("id", "-u") != "0":
            raise ValueError("Device network isolation requires root; no fallback")
        packages = self.device.adb("shell", "cmd", "package", "list", "packages", "-U")
        values = re.findall(r"package:([^\s]+)\s+uid:([0-9]+)", packages)
        matches = [int(uid) for name, uid in values if name == self.profile["app_id"]]
        if (
            len(matches) != 1
            or matches[0] < 10000
            or sum(int(uid) == matches[0] for _, uid in values) != 1
        ):
            raise ValueError("Firewall requires a unique non-system App UID")
        self.uid = matches[0]
        try:
            for tool, version in (("iptables", 4), ("ip6tables", 6)):
                self.run(tool, "-w", "2", "-N", self.chain)
                self.created.append(tool)
                for ip, port in endpoints:
                    if ip.version == version:
                        self.run(
                            tool,
                            "-w",
                            "2",
                            "-A",
                            self.chain,
                            "-p",
                            "tcp",
                            "-d",
                            str(ip),
                            "--dport",
                            port,
                            "-j",
                            "ACCEPT",
                        )
                self.run(tool, "-w", "2", "-A", self.chain, "-j", "REJECT")
                self.expected[tool] = self.run(tool, "-w", "2", "-S", self.chain)
                self.run(
                    tool,
                    "-w",
                    "2",
                    "-I",
                    "OUTPUT",
                    "1",
                    "-m",
                    "owner",
                    "--uid-owner",
                    self.uid,
                    "-j",
                    self.chain,
                )
                self.jumps.append(tool)
                if (
                    not self.run(
                        tool,
                        "-w",
                        "2",
                        "-C",
                        "OUTPUT",
                        "-m",
                        "owner",
                        "--uid-owner",
                        self.uid,
                        "-j",
                        self.chain,
                    )
                    == ""
                ):
                    raise ValueError("Firewall attachment could not be verified")
        except BaseException:
            self.close()
            raise

    def close(self):
        if not self.created:
            return
        previous = self.device.control
        self.device.control = RunControl(timeout_seconds=15)
        errors = []
        try:
            for tool in reversed(self.created):
                try:
                    if tool in self.jumps:
                        self.run(
                            tool,
                            "-w",
                            "2",
                            "-D",
                            "OUTPUT",
                            "-m",
                            "owner",
                            "--uid-owner",
                            self.uid,
                            "-j",
                            self.chain,
                        )
                        self.jumps.remove(tool)
                    if (
                        tool in self.expected
                        and self.run(tool, "-w", "2", "-S", self.chain)
                        != self.expected[tool]
                    ):
                        raise ValueError(
                            "Owned firewall chain was externally changed; reconciliation required"
                        )
                    self.run(tool, "-w", "2", "-F", self.chain)
                    self.run(tool, "-w", "2", "-X", self.chain)
                except (OSError, ValueError, RuntimeError) as exc:
                    errors.append(str(exc))
            self.created.clear()
        finally:
            self.device.control = previous
        if errors:
            raise RuntimeError(
                "Device firewall cleanup requires reconciliation: " + "; ".join(errors)
            )
