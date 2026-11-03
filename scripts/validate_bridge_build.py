"""Compile development-only SDK examples; inspect release exclusion, no phone."""

import json
import subprocess
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
source = root / "iatest/mobile/native/bridge"
java = "/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home/bin/javac"
android = (
    "/opt/homebrew/share/android-commandlinetools/platforms/android-36/android.jar"
)
with tempfile.TemporaryDirectory(prefix="iatest-bridge-build-") as directory:
    work = Path(directory)
    subprocess.run(
        [
            java,
            "--release",
            "8",
            "-classpath",
            android,
            "-d",
            str(work),
            str(source / "android/IATestBridge.java"),
        ],
        check=True,
        capture_output=True,
    )
    sdk = subprocess.check_output(
        ["xcrun", "--sdk", "iphonesimulator", "--show-sdk-path"], text=True
    ).strip()
    for source_name, symbol in [
        ("IATestBridge.m", "_IATestBridgePublish"),
        ("IATestReplayStream.m", "_OBJC_CLASS_$_IATestReplayStream"),
    ]:
        for debug in (0, 1):
            target = work / (source_name + str(debug) + ".o")
            subprocess.run(
                [
                    "xcrun",
                    "--sdk",
                    "iphonesimulator",
                    "clang",
                    "-fobjc-arc",
                    "-DDEBUG=" + str(debug),
                    "-target",
                    "arm64-apple-ios15.0-simulator",
                    "-isysroot",
                    sdk,
                    "-c",
                    str(source / "ios" / source_name),
                    "-o",
                    str(target),
                ],
                capture_output=True,
                check=True,
            )
            names = subprocess.check_output(["nm", "-g", str(target)], text=True)
            assert (symbol in names) == bool(debug)
            if source_name == "IATestBridge.m":
                assert ("_IATestBridgeObserveMetalCommandBuffer" in names) == bool(debug)
    print(
        json.dumps(
            {
                "android_sdk_compile": True,
                "ios_debug_sdk_compile": True,
                "ios_release_bridge_symbol_absent": True,
                "phone_actions": 0,
            }
        )
    )
