"""Build framework-owned mobile artifacts using installed platform toolchains.

No Gradle/Maven, pip dependencies, SDK downloads, or global configuration changes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

ROOT = Path.cwd()
SOURCE = Path(__file__).with_name("native")


def run(args: list[str], env: dict | None = None) -> None:
    subprocess.run([str(a) for a in args], check=True, env=env)


def android(args) -> None:
    hid = getattr(args, "hid", False)
    if hid and not getattr(args, "keystore", None):
        raise ValueError("Bluetooth HID build requires an existing development keystore")
    sdk = Path(
        args.sdk
        or os.environ.get(
            "ANDROID_HOME", "/opt/homebrew/share/android-commandlinetools"
        )
    )
    java = Path(args.java_home or os.environ.get("JAVA_HOME", ""))
    env = dict(os.environ)
    if args.java_home:
        env["JAVA_HOME"] = str(java)
        env["PATH"] = str(java / "bin") + os.pathsep + env.get("PATH", "")

    def tool(name):
        found = java / "bin" / name
        return str(found) if found.is_file() else shutil.which(name) or name

    platforms = sorted(
        (sdk / "platforms").glob("android-*/android.jar"),
        key=lambda p: int(p.parent.name.split("-")[-1]),
    )
    builds = sorted(
        (sdk / "build-tools").glob("*/aapt2"),
        key=lambda p: tuple(int(x) for x in p.parent.name.split(".")),
    )
    if not platforms or not builds:
        raise SystemExit(
            "Installed Android platform and build-tools are required; nothing was downloaded"
        )
    android_jar, build = platforms[-1], builds[-1].parent
    if hid and int(android_jar.parent.name.split("-")[-1]) < 36:
        raise SystemExit(
            "Bluetooth HID build requires installed Android platform 36+ for target identity checks; nothing was downloaded"
        )
    output = Path(args.output or ROOT / ("native/mobile/build/android-hid" if hid else "native/mobile/build/android")).resolve()
    classes, dex = output / "classes", output / "dex"
    classes.mkdir(parents=True, exist_ok=True)
    dex.mkdir(exist_ok=True)
    source = SOURCE / ("android-hid" if hid else "android")
    minimum = 28 if hid else 26
    run(
        [
            tool("javac"),
            "--release",
            "8",
            "-encoding",
            "UTF-8",
            "-classpath",
            android_jar,
            "-d",
            classes,
            *sorted((source / "src").rglob("*.java")),
        ],
        env,
    )
    compiled = output / "compiled.jar"
    with zipfile.ZipFile(compiled, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(classes.rglob("*.class")):
            archive.write(path, path.relative_to(classes).as_posix())
    run(
        [
            build / "d8",
            "--min-api",
            str(minimum),
            "--lib",
            android_jar,
            "--output",
            dex,
            compiled,
        ],
        env,
    )
    helper = output / "iatest-input.jar"
    with zipfile.ZipFile(helper, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(dex.glob("*.dex")):
            archive.write(path, path.name)
    unsigned = output / "probe-unsigned.apk"
    resources = output / "resources.zip"
    run([build / "aapt2", "compile", "--dir", source / "res", "-o", resources], env)
    run(
        [
            build / "aapt2",
            "link",
            "-o",
            unsigned,
            "-I",
            android_jar,
            "--manifest",
            source / "AndroidManifest.xml",
            resources,
        ],
        env,
    )
    with zipfile.ZipFile(unsigned, "a", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(dex.glob("*.dex")):
            archive.write(path, path.name)
    aligned = output / "probe-aligned.apk"
    run([build / "zipalign", "-f", "4", unsigned, aligned], env)
    # Framework-owned development signing only; no existing user keychain/key is used.
    key = Path(args.keystore).resolve(strict=True) if getattr(args, "keystore", None) else output / "development.keystore"
    if not key.exists():
        run(
            [
                tool("keytool"),
                "-genkeypair",
                "-keystore",
                key,
                "-storepass",
                "android",
                "-keypass",
                "android",
                "-alias",
                "androiddebugkey",
                "-dname",
                "CN=IATest Development",
                "-keyalg",
                "RSA",
                "-validity",
                "3650",
            ],
            env,
        )
    apk = output / ("iatest-bluetooth-hid.apk" if hid else "iatest-probe.apk")
    run(
        [
            build / "apksigner",
            "sign",
            "--ks",
            key,
            "--ks-pass",
            "pass:android",
            "--key-pass",
            "pass:android",
            "--out",
            apk,
            aligned,
        ],
        env,
    )
    run([build / "apksigner", "verify", apk], env)
    manifest = {
        "protocol": "iatest-bluetooth-hid/1" if hid else "iatest-mobile/1",
        "platform": "android",
        "min_sdk": minimum,
        "compile_sdk": android_jar.parent.name,
        "build_tools": build.name,
        "signing": "local-development-only",
        "continuous_video": not hid,
        "sha256": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (helper, apk)
        },
        "source_sha256": {
            p.relative_to(SOURCE).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(source.rglob("*"))
            if p.is_file()
        },
    }
    if hid:
        manifest["managed_ble_protocol"] = "iatest-managed-ble/1"
        manifest["managed_pairing_protocol"] = "iatest-managed-ble-pairing/1"
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"artifacts": str(output), "verified_apk_signature": True}))


def ios(args) -> None:
    output = Path(args.output or ROOT / "native/mobile/build/ios").resolve()
    command = [
        "xcodebuild",
        "build-for-testing",
        "-project",
        SOURCE / "ios/IATestMobile.xcodeproj",
        "-scheme",
        "IATestPerformance" if args.performance else "IATestMobile",
        "-destination",
        "generic/platform=" + ("iOS" if args.device else "iOS Simulator"),
        "-derivedDataPath",
        output,
        "CODE_SIGNING_ALLOWED=" + ("YES" if args.team else "NO"),
    ]
    if args.team:
        command.append("DEVELOPMENT_TEAM=" + args.team)
    run(command)
    print(
        json.dumps(
            {
                "build_products": str(output / "Build/Products"),
                "signed_for_device": bool(args.device and args.team),
                "scheme": "IATestPerformance" if args.performance else "IATestMobile",
            }
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="platform", required=True)
    a = sub.add_parser("android")
    a.add_argument("--sdk")
    a.add_argument("--java-home")
    a.add_argument("--output")
    a.add_argument("--hid", action="store_true", help="Build the foreground Android Bluetooth HID bridge")
    a.add_argument("--keystore", help="Existing framework development keystore")
    i = sub.add_parser("ios")
    i.add_argument("--device", action="store_true")
    i.add_argument(
        "--performance",
        action="store_true",
        help="Release Runner without debugger/coverage/checkers",
    )
    i.add_argument("--team")
    i.add_argument("--output")
    args = parser.parse_args()
    (android if args.platform == "android" else ios)(args)


if __name__ == "__main__":
    main()
