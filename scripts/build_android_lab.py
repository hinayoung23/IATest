"""Build the owned Android fixture with installed SDK tools and an existing dev key."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--keystore", type=Path, required=True)
    parser.add_argument("--version-code", type=int, default=1)
    parser.add_argument("--companion", action="store_true")
    parser.add_argument("--split-fixture", action="store_true")
    parser.add_argument(
        "--sdk", type=Path, default=Path("/opt/homebrew/share/android-commandlinetools")
    )
    parser.add_argument(
        "--java-home",
        type=Path,
        default=Path("/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home"),
    )
    args = parser.parse_args()
    if not args.keystore.is_file() or not 1 <= args.version_code <= 100:
        parser.error(
            "An existing framework development key and version 1–100 are required"
        )
    root = Path(__file__).resolve().parents[1]
    native = root / "iatest/mobile/native"
    source = native / "android-lab"
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "JAVA_HOME": str(args.java_home),
        "PATH": str(args.java_home / "bin") + os.pathsep + os.environ.get("PATH", ""),
    }
    android = max(
        (args.sdk / "platforms").glob("android-*/android.jar"),
        key=lambda p: int(p.parent.name.split("-")[-1]),
    )
    build = max(
        (args.sdk / "build-tools").glob("*/aapt2"),
        key=lambda p: tuple(int(x) for x in p.parent.name.split(".")),
    ).parent

    def run(*command):
        subprocess.run([str(v) for v in command], check=True, env=env)

    classes, dex = output / "classes", output / "dex"
    classes.mkdir(exist_ok=True)
    dex.mkdir(exist_ok=True)
    run(
        args.java_home / "bin/javac",
        "--release",
        "8",
        "-encoding",
        "UTF-8",
        "-classpath",
        android,
        "-d",
        classes,
        source / "MainActivity.java",
        native / "bridge/android/IATestBridge.java",
    )
    compiled = output / "compiled.jar"
    with zipfile.ZipFile(compiled, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(classes.rglob("*.class")):
            archive.write(path, path.relative_to(classes).as_posix())
    run(build / "d8", "--min-api", "26", "--lib", android, "--output", dex, compiled)
    manifest = ET.parse(source / "AndroidManifest.xml")
    android_ns = "{http://schemas.android.com/apk/res/android}"
    package = "dev.iatest.mobile." + ("splitlab" if args.split_fixture else "companion" if args.companion else "lab")
    manifest.getroot().set("package", package)
    manifest.getroot().set(android_ns + "versionCode", str(args.version_code))
    manifest.getroot().set(android_ns + "versionName", str(args.version_code) + ".0")
    rendered = output / "AndroidManifest.xml"
    manifest.write(rendered, encoding="utf-8", xml_declaration=True)
    resources = output / "resources.zip"
    unsigned, aligned, apk = (
        output / name for name in ("unsigned.apk", "aligned.apk", "iatest-lab.apk")
    )
    run(build / "aapt2", "compile", "--dir", source / "res", "-o", resources)
    resource_inputs = [resources]
    if args.split_fixture:
        for suffix, text in (("values", "Split fixture"), ("values-zh", "分包测试")):
            folder = output / "split-res" / suffix
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "strings.xml").write_text('<resources><string name="split_probe">' + text + '</string></resources>')
        extra = output / "split-resources.zip"
        run(build / "aapt2", "compile", "--dir", output / "split-res", "-o", extra)
        resource_inputs.append(extra)
    run(
        build / "aapt2",
        "link",
        "-o",
        unsigned,
        "-I",
        android,
        "--manifest",
        rendered,
        *resource_inputs,
    )
    with zipfile.ZipFile(unsigned, "a", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(dex.glob("*.dex")):
            archive.write(path, path.name)
    run(build / "zipalign", "-f", "4", unsigned, aligned)
    run(
        build / "apksigner",
        "sign",
        "--ks",
        args.keystore,
        "--ks-pass",
        "pass:android",
        "--key-pass",
        "pass:android",
        "--out",
        apk,
        aligned,
    )
    run(build / "apksigner", "verify", apk)
    metadata = {
        "app_id": package,
        "version_code": args.version_code,
        "development_only": True,
        "apk_sha256": hashlib.sha256(apk.read_bytes()).hexdigest(),
    }
    (output / "manifest.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(json.dumps({**metadata, "apk": str(apk)}))


if __name__ == "__main__":
    main()
