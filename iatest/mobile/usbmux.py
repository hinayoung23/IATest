"""Small plist usbmuxd client; no pairing records or device secrets are read.

Protocol reference: libimobiledevice/libusbmuxd, include/usbmuxd-proto.h
and src/libusbmuxd.c (ListDevices and Connect). Independently implemented.
"""

from __future__ import annotations

import errno
import os
import plistlib
import socket
import struct

from iatest.mobile.protocol import ProtocolError, receive_exact


def _daemon() -> socket.socket:
    if os.name == "nt":
        return socket.create_connection(("127.0.0.1", 27015), timeout=5)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5)
    try:
        sock.connect("/var/run/usbmuxd")
    except BaseException:
        sock.close()
        raise
    return sock


def _exchange(sock: socket.socket, message: dict) -> dict:
    data = plistlib.dumps(
        {
            "ClientVersionString": "iatest-mobile-1",
            "ProgName": "iatest",
            "kLibUSBMuxVersion": 3,
            **message,
        }
    )
    sock.sendall(struct.pack("<IIII", len(data) + 16, 1, 8, 1) + data)
    length, version, kind, tag = struct.unpack("<IIII", receive_exact(sock, 16))
    if not 16 <= length <= 1024 * 1024 or (version, kind, tag) != (1, 8, 1):
        raise ProtocolError("Invalid usbmuxd response header")
    value = plistlib.loads(receive_exact(sock, length - 16))
    if not isinstance(value, dict):
        raise ProtocolError("Invalid usbmuxd response")
    return value


def list_devices() -> list[dict]:
    with _daemon() as sock:
        response = _exchange(sock, {"MessageType": "ListDevices"})
    devices = response.get("DeviceList")
    if not isinstance(devices, list) or len(devices) > 256:
        raise ProtocolError("usbmuxd did not return a bounded device list")
    found = []
    for device in devices:
        properties = device.get("Properties") if isinstance(device, dict) else None
        if (
            not isinstance(properties, dict)
            or type(device.get("DeviceID")) is not int
            or device["DeviceID"] <= 0
            or not isinstance(properties.get("SerialNumber"), str)
            or not 1 <= len(properties["SerialNumber"]) <= 200
            or not isinstance(properties.get("ConnectionType", "USB"), str)
            or properties.get("ConnectionType", "USB") not in {"USB", "Network"}
        ):
            raise ProtocolError("usbmuxd returned an invalid device identity")
        found.append({
            "device_id": properties["SerialNumber"],
            "mux_id": device["DeviceID"],
            "connection": properties.get("ConnectionType", "USB"),
            "platform": "ios",
        })
    return found


def connect(device_id: str, port: int) -> socket.socket:
    devices = [
        d
        for d in list_devices()
        if d["device_id"] == device_id and d["connection"] == "USB"
    ]
    if len(devices) != 1:
        raise ProtocolError("Exactly one matching USB iOS device is required")
    sock = _daemon()
    try:
        response = _exchange(
            sock,
            {
                "MessageType": "Connect",
                "DeviceID": devices[0]["mux_id"],
                "PortNumber": socket.htons(port),
            },
        )
        code = response.get("Number")
        if type(code) is int and code == 3:
            # RESULT_CONNREFUSED: the Runner may still be starting. NativeDevice
            # already bounds connection waiting and never replays App input here.
            raise ConnectionRefusedError(
                errno.ECONNREFUSED, "iOS Runner is not listening"
            )
        if type(code) is not int or code != 0:
            raise ProtocolError(f"iOS Runner port unavailable: {code}")
        return sock
    except BaseException:
        sock.close()
        raise
