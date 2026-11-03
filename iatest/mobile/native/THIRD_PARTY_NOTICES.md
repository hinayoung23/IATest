# Mobile implementation provenance

Android input/display mechanisms were studied in Genymobile scrcpy v5.0.1,
commit a60891aea193d92e7e5c3942700eca63f9d19a5f:

- `server/src/main/java/com/genymobile/scrcpy/control/Controller.java`
- `server/src/main/java/com/genymobile/scrcpy/wrappers/InputManager.java`
- `server/src/main/java/com/genymobile/scrcpy/wrappers/DisplayManager.java`

scrcpy is Copyright (C) Genymobile and contributors, licensed under Apache-2.0.
See https://github.com/Genymobile/scrcpy/blob/v5.0.1/LICENSE and this repository's LICENSE.
The IATest Java implementation narrows the action set, introduces a separate
bounded protocol and operation ledger, and separates instrumentation semantics.
It does not implement scrcpy wire compatibility or ship its desktop client.

The plist usbmuxd protocol was studied in libimobiledevice/libusbmuxd,
`include/usbmuxd-proto.h` and `src/libusbmuxd.c`; the Python transport is an
independent implementation of ListDevices and Connect, without pairing record access.

No commercial executable or decompiled commercial implementation is included.

The native video paths use Android MediaCodec/EGL and Apple VideoToolbox APIs.
The host optionally invokes an existing FFmpeg installation; its binary is not shipped.
The iOS optional gesture path checks XCTest event-record selectors at runtime.
Selector signatures were checked against the installed SDK and the upstream
Appium WebDriverAgent private headers/FBXCTestDaemonsProxy usage:
https://github.com/appium/WebDriverAgent/blob/master/WebDriverAgentLib/Utilities/FBXCTestDaemonsProxy.m
No WebDriverAgent execution backend is included or copied. These private interfaces
are explicitly opt-in and require compatibility testing for each Xcode release.

The optional Simulator HID helper independently implements bounded DTUHID/XPC
messages. Runtime symbol names and the wire dictionary were checked against the
installed Xcode SDK and Meta idb's HID/XPC source (MIT):
https://github.com/facebook/idb/tree/main/FBSimulatorControl/HID
https://github.com/facebook/idb/blob/main/FBSimulatorControl/XPC/SimulatorXPCConnection.swift
No idb execution backend or commercial iOS utility is bundled. This helper is
Simulator-only, requires a live DTUHID probe and never enables physical-device
global input or starts a Simulator.
