"""Content-addressed App delivery and explicit fresh/upgrade preparation."""

from __future__ import annotations
from iatest.core.env import environment_values

import hashlib
import json
import os
import plistlib
import re
import shutil
import stat
import tempfile
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from iatest.core.redaction import redact, secret_scope
from iatest.core.sandbox import origin
from iatest.engines.http import NetworkScope, resolve_secrets, validate
from iatest.engines.store import EngineStore
from iatest.execution.control import RunControl
from iatest.mobile.bundles import SIGNING_SCHEMA
from iatest.mobile.devices import NativeDevice, command
from iatest.mobile.lease import device_scope
from iatest.mobile.store import MobileStore

HASH = {"type": "string", "pattern": "^[a-fA-F0-9]{64}$"}
APP_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["platform", "app_id"],
    "properties": {
        "id": {"type": "string", "pattern": "^[A-Za-z0-9_.-]{1,80}$"},
        "platform": {"enum": ["android", "ios"]},
        "app_id": {
            "type": "string",
            "pattern": "^[A-Za-z][A-Za-z0-9_]*(?:\\.[A-Za-z0-9_-]+)+$",
        },
        "url": {"type": "string", "maxLength": 8192},
        "path": {"type": "string", "maxLength": 4096},
        "allowed_origins": {
            "type": "array",
            "minItems": 1,
            "maxItems": 32,
            "items": {"type": "string"},
        },
        "headers": {
            "type": "object",
            "maxProperties": 20,
            "additionalProperties": {
                "type": "object",
                "additionalProperties": False,
                "required": ["$env"],
                "properties": {"$env": {"type": "string"}},
            },
        },
        "sha256": HASH,
        "certificate_sha256": HASH,
        "version": {"type": "string", "maxLength": 80},
        "format": {"enum": ["apk", "apks", "aab", "ipa"]},
        "signing": SIGNING_SCHEMA,
        "max_bytes": {"type": "integer", "minimum": 1, "maximum": 1073741824},
    },
    "oneOf": [
        {"required": ["url"], "not": {"required": ["path"]}},
        {"required": ["path"], "not": {"required": ["url"]}},
    ],
}

PREPARATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["profile", "artifact_id", "mode"],
    "properties": {
        "profile": {"type": "string", "pattern": "^[A-Za-z0-9_.-]{1,80}$"},
        "artifact_id": {"type": "string", "pattern": "^[A-Za-z0-9_.-]{1,80}$"},
        "base_artifact_id": {"type": "string", "pattern": "^[A-Za-z0-9_.-]{1,80}$"},
        "seed_plan_id": {"type": "string", "pattern": "^[A-Za-z0-9_.-]{1,80}$"},
        "mode": {"enum": ["fresh", "upgrade"]},
    },
}


def sdk_tool(name):
    found = shutil.which(name)
    if found:
        return found
    for sdk in filter(
        None,
        (
            environment_values().get("ANDROID_SDK_ROOT"),
            environment_values().get("ANDROID_HOME"),
            str(Path.home() / "Library/Android/sdk"),
            str(Path.home() / "Android/Sdk"),
            "/opt/homebrew/share/android-commandlinetools",
        ),
    ):
        choices = sorted(
            (Path(sdk) / "build-tools").glob("*/" + name),
            key=lambda p: tuple(int(n) for n in re.findall(r"\d+", p.parent.name)),
            reverse=True,
        )
        if choices:
            return str(choices[0])
    raise ValueError(f"Installed Android SDK tool required: {name}")


def file_hash(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as source:
        while chunk := source.read(1048576):
            value.update(chunk)
    return value.hexdigest()


def artifact_suffix(value):
    platform = value.get("platform")
    fmt = value.get("format", "apk" if platform == "android" else "ipa")
    if fmt not in ({"apk", "apks"} if platform == "android" else {"ipa"} if platform == "ios" else set()):
        raise ValueError("Invalid frozen package format")
    return "." + fmt


def safe_ipa(path, output=None):
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) > 20000 or sum(e.file_size for e in entries) > 2147483648:
            raise ValueError("IPA archive exceeds extraction budget")
        infos = [
            e
            for e in entries
            if re.fullmatch(r"Payload/[^/]+\.app/Info\.plist", e.filename)
        ]
        if len(infos) != 1 or infos[0].file_size > 1048576:
            raise ValueError("IPA must contain one bounded application Info.plist")
        app_dir = str(Path(infos[0].filename).parent)
        for entry in entries:
            if (
                entry.filename.startswith("/")
                or ".." in Path(entry.filename).parts
                or stat.S_ISLNK(entry.external_attr >> 16)
            ):
                raise ValueError("IPA contains an unsafe path or symlink")
        info = plistlib.loads(archive.read(infos[0]))
        profile_path = app_dir + "/embedded.mobileprovision"
        if output is not None:
            for entry in entries:
                if entry.filename == app_dir or entry.filename.startswith(
                    app_dir + "/"
                ):
                    archive.extract(entry, output)
                    if not entry.is_dir():
                        mode = (entry.external_attr >> 16) & 0o777
                        if mode:
                            (Path(output) / entry.filename).chmod(mode)
            return info, Path(output) / app_dir
        return info, profile_path in archive.namelist()


def inspect_package(path, platform, *, runner=command):
    path = Path(path)
    if platform == "android":
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                is_set = "AndroidManifest.xml" not in archive.namelist() and any(n.endswith(".apk") for n in archive.namelist())
            if is_set:
                from iatest.mobile.bundles import inspect_set

                with tempfile.TemporaryDirectory(prefix="iatest-apks-") as output:
                    return inspect_set(path, output, lambda p, platform: inspect_package(p, platform, runner=runner))
        badging = runner([sdk_tool("aapt"), "dump", "badging", str(path)], timeout=30)
        package_line = re.search(r"^package:\s+(.+)$", badging, re.MULTILINE)
        package = dict(re.findall(r"([A-Za-z]+)='([^']*)'", package_line[1])) if package_line else {}
        if not {"name", "versionCode", "versionName"} <= package.keys():
            raise ValueError("APK identity metadata unavailable")
        signer = runner(
            [sdk_tool("apksigner"), "verify", "--print-certs", str(path)], timeout=30
        )
        certs = re.findall(
            r"Signer #[0-9]+ certificate SHA-256 digest: ([a-fA-F0-9]+)", signer
        )
        if not certs:
            raise ValueError("APK signature verification did not provide a signer")
        minimum = re.search(r"sdkVersion:'([0-9]+)'", badging)
        abi = re.search(r"native-code: (.+)", badging)
        return {
            "app_id": package["name"],
            "version_code": package["versionCode"] or "0",
            "version": package["versionName"],
            "split_name": package.get("split", ""),
            "certificate_sha256": certs[0].lower(),
            "certificates": sorted({c.lower() for c in certs}),
            "minimum_os": minimum[1] if minimum else "",
            "abis": re.findall(r"'([^']+)'", abi[1]) if abi else [],
            "debuggable": bool(
                re.search(
                    r"^application-debuggable(?:\s*:)?\s*$", badging, re.MULTILINE
                )
            ),
        }
    info, profile_present = safe_ipa(path)
    if not profile_present:
        raise ValueError(
            "Device IPA requires embedded provisioning; simulator packages are separate artifacts"
        )
    with tempfile.TemporaryDirectory(prefix="iatest-ipa-verify-") as temp:
        _, app = safe_ipa(path, temp)
        runner(["codesign", "--verify", "--deep", "--strict", str(app)], timeout=60)
        profile = plistlib.loads(
            runner(
                ["security", "cms", "-D", "-i", str(app / "embedded.mobileprovision")],
                timeout=15,
            ).encode()
        )
        prefix = str(Path(temp) / "certificate-")
        runner(
            ["codesign", "-d", "--extract-certificates=" + prefix, str(app)], timeout=15
        )
        cert_file = Path(prefix + "0")
        if not cert_file.is_file():
            raise ValueError("IPA signer certificate was not extracted")
        cert = file_hash(cert_file)
        if cert not in {
            hashlib.sha256(c).hexdigest()
            for c in profile.get("DeveloperCertificates", [])
        }:
            raise ValueError(
                "IPA signing certificate is not authorized by the provisioning profile"
            )
        application = profile.get("Entitlements", {}).get("application-identifier", "")
        bundle = info.get("CFBundleIdentifier", "")
        pattern = application.partition(".")[2]
        if not pattern or not (
            bundle == pattern
            or (pattern.endswith(".*") and bundle.startswith(pattern[:-1]))
            or pattern == "*"
        ):
            raise ValueError(
                "IPA bundle identity is not authorized by the provisioning profile"
            )
    expires = profile.get("ExpirationDate")
    if not isinstance(expires, datetime) or expires.replace(tzinfo=UTC) <= datetime.now(
        UTC
    ):
        raise ValueError("IPA provisioning profile is expired or invalid")
    return {
        "app_id": info.get("CFBundleIdentifier", ""),
        "version": str(info.get("CFBundleShortVersionString", "")),
        "version_code": str(info.get("CFBundleVersion", "")),
        "minimum_os": str(info.get("MinimumOSVersion", "")),
        "certificate_sha256": cert,
        "provisioned_devices": profile.get("ProvisionedDevices", []),
        "all_devices": bool(profile.get("ProvisionsAllDevices")),
        "profile_expires": expires.isoformat(),
        "platforms": info.get("CFBundleSupportedPlatforms", []),
        "development_provisioning": profile.get("Entitlements", {}).get(
            "get-task-allow"
        )
        is True,
    }


def fetch_app(root, project, spec, *, control=None, inspector=inspect_package):
    validate(spec, APP_SCHEMA)
    package_format = spec.get("format", "apk" if spec["platform"] == "android" else "ipa")
    if (package_format == "ipa") != (spec["platform"] == "ios"):
        raise ValueError("Package format differs from selected platform")
    if package_format == "aab" and "signing" not in spec:
        raise ValueError("AAB conversion requires explicit existing signing references")
    store = EngineStore(root, project)
    cache = store.directory / "app-cache"
    cache.mkdir(parents=True, exist_ok=True)
    control = control or RunControl(timeout_seconds=300)
    maximum = spec.get("max_bytes", 536870912)
    headers = resolve_secrets(spec.get("headers", {}))
    with (
        secret_scope(headers.values()),
        tempfile.NamedTemporaryFile(
            dir=cache, prefix="download-", delete=False
        ) as target,
    ):
        temporary = Path(target.name)
        sha, size = hashlib.sha256(), 0

        def consume(chunks):
            nonlocal size
            for chunk in chunks:
                control.check()
                size += len(chunk)
                if size > maximum:
                    raise ValueError("App exceeds declared download size")
                sha.update(chunk)
                target.write(chunk)

        try:
            if "path" in spec:
                with Path(spec["path"]).expanduser().open("rb") as source:
                    consume(iter(lambda: source.read(1048576), b""))
            else:
                scope = NetworkScope(
                    root, project, spec.get("allowed_origins") or [origin(spec["url"])]
                )
                url = spec["url"]
                with httpx.Client(trust_env=False, follow_redirects=False) as client:
                    for redirect in range(6):
                        scope.check(url)
                        control.check()
                        with client.stream(
                            "GET",
                            url,
                            headers=headers,
                            timeout=min(30, control.remaining() or 30),
                        ) as response:
                            if response.status_code in {
                                301,
                                302,
                                303,
                                307,
                                308,
                            } and response.headers.get("location"):
                                next_url = urljoin(url, response.headers["location"])
                                scope.check(next_url)
                                if origin(next_url) != origin(url):
                                    headers = {}
                                    client.cookies.clear()
                                url = next_url
                                continue
                            response.raise_for_status()
                            if int(response.headers.get("content-length", 0)) > maximum:
                                raise ValueError("App exceeds declared download size")
                            consume(response.iter_bytes(1048576))
                            break
                    else:
                        raise ValueError("App download redirect limit exceeded")
            target.flush()
            if (
                not size
                or spec.get("sha256", sha.hexdigest()).lower() != sha.hexdigest()
            ):
                raise ValueError("App is empty or SHA-256 does not match")
            source_sha256 = sha.hexdigest()
            if package_format == "aab":
                from iatest.mobile.bundles import convert_bundle

                converted = Path(str(temporary) + ".apks")
                with secret_scope([environment_values().get(spec["signing"][k], "") for k in ("store_password_env", "key_password_env")]):
                    convert_bundle(temporary, converted, spec["signing"], control)
                converted.replace(temporary)
                package_format = "apks"
                sha = hashlib.sha256()
                with temporary.open("rb") as generated:
                    while chunk := generated.read(1048576):
                        control.check()
                        sha.update(chunk)
                size = temporary.stat().st_size
                if size > maximum:
                    raise ValueError("Generated APK set exceeds declared size quota")
            metadata = inspector(temporary, spec["platform"])
            if metadata["app_id"] != spec["app_id"] or (
                spec.get("version") and metadata["version"] != spec["version"]
            ):
                raise ValueError(
                    "Downloaded App identity/version differs from the declared build"
                )
            if (
                spec.get("certificate_sha256")
                and metadata["certificate_sha256"] != spec["certificate_sha256"].lower()
            ):
                raise ValueError("Downloaded App signing certificate differs")
            suffix = "." + package_format
            destination = cache / (sha.hexdigest() + suffix)
            target.close()
            if destination.exists() and file_hash(destination) != sha.hexdigest():
                raise ValueError("Existing App cache is corrupted")
            temporary.replace(destination)
            source = {k: v for k, v in spec.items() if k not in {"headers", "path"}}
            if "url" in source:
                parsed = urlsplit(source["url"])
                source["url"] = urlunsplit(
                    (parsed.scheme, parsed.netloc, parsed.path, "", "")
                )
            value = {
                "id": spec.get("id", "app-" + sha.hexdigest()[:24]),
                "platform": spec["platform"],
                "format": package_format,
                "source_sha256": source_sha256,
                "sha256": sha.hexdigest(),
                "bytes": size,
                "metadata": metadata,
                "source": source,
                "created_at": datetime.now(UTC).isoformat(),
            }
            try:
                existing = store.get_artifact("app", value["id"])
            except ValueError:
                store.save_artifact("app", value, artifact_id=value["id"])
            else:
                if existing["sha256"] != value["sha256"]:
                    raise ValueError(
                        "App asset ID already names a different build; choose a new ID"
                    )
                return existing
            return redact(value)
        except httpx.HTTPError as exc:
            status = (
                exc.response.status_code
                if isinstance(exc, httpx.HTTPStatusError)
                else type(exc).__name__
            )
            raise ValueError(
                f"App download failed ({status}); verify the source and its credential references"
            ) from None
        finally:
            target.close()
            temporary.unlink(missing_ok=True)
            Path(str(temporary) + ".apks").unlink(missing_ok=True)


def cached_app(root, project, artifact_id):
    store = EngineStore(root, project)
    value = store.get_artifact("app", artifact_id)
    path = (
        store.directory
        / "app-cache"
        / (value["sha256"] + artifact_suffix(value))
    )
    if not path.is_file() or file_hash(path) != value["sha256"]:
        raise ValueError("App content is missing or corrupted; fetch the build again")
    return value, path


class AppInstaller:
    def __init__(self, profile, control):
        self.profile, self.control = profile, control
        self.device = NativeDevice(profile, "delivery-" + uuid.uuid4().hex[:16])
        self.device.control = control

    def adb(self, *args, **kwargs):
        return self.device.adb(*args, **kwargs)

    def ios(self, *args):
        return json.loads(
            command(
                [
                    "xcrun",
                    "devicectl",
                    "device",
                    *args,
                    "--device",
                    self.profile["device_id"],
                    "--json-output",
                    "-",
                ],
                timeout=min(90, self.control.remaining() or 90),
                control=self.control,
            )
        )

    def installed(self):
        app_id = self.profile["app_id"]
        if self.profile["platform"] == "android":
            # pm path exits nonzero for an absent package on current Android.
            packages = self.adb("shell", "pm", "list", "packages", app_id).splitlines()
            if any(not line.startswith("package:") for line in packages if line.strip()):
                raise ValueError("Installed App list cannot be verified")
            if "package:" + app_id not in packages:
                return None
            value = self.adb("shell", "dumpsys", "package", app_id)
            code, version = (
                re.search(r"versionCode=([0-9]+)", value),
                re.search(r"versionName=([^\s]+)", value),
            )
            if not code:
                raise ValueError("Installed App version cannot be verified")
            return {
                "app_id": app_id,
                "version_code": code[1],
                "version": version[1] if version else "",
            }
        if self.profile.get("simulator"):
            raise ValueError(
                "Device IPA deployment is unavailable for simulator profiles"
            )
        value = self.ios("info", "apps", "--bundle-id", app_id)
        apps = value.get("result", {}).get("apps", [])
        match = next((a for a in apps if a.get("bundleIdentifier") == app_id), None)
        return (
            {
                "app_id": app_id,
                "version": str(match.get("version", "")),
                "version_code": str(match.get("bundleVersion", "")),
            }
            if match
            else None
        )

    def preflight(self, artifact, path):
        meta = artifact["metadata"]
        if (
            artifact["platform"] != self.profile["platform"]
            or meta["app_id"] != self.profile["app_id"]
        ):
            raise ValueError("Build and device profile target different Apps/platforms")
        # Verify integrity/signature again before any uninstall; cached metadata is not authority.
        actual = inspect_package(path, artifact["platform"])
        if any(
            actual.get(k) != meta.get(k)
            for k in ("app_id", "version", "version_code", "certificate_sha256")
        ):
            raise ValueError("App inspection changed after registration")
        meta = actual
        if self.profile["platform"] == "android":
            sdk = int(self.adb("shell", "getprop", "ro.build.version.sdk").strip())
            if meta.get("minimum_os") and sdk < int(meta["minimum_os"]):
                raise ValueError("App requires a newer Android SDK")
            device_abis = (
                self.adb("shell", "getprop", "ro.product.cpu.abilist")
                .strip()
                .split(",")
            )
            if meta.get("abis") and not set(device_abis) & set(meta["abis"]):
                raise ValueError("App and device ABIs are incompatible")
        elif not meta.get("all_devices") and self.profile["device_id"].lower() not in {
            d.lower() for d in meta.get("provisioned_devices", [])
        }:
            raise ValueError("IPA profile does not authorize this device")
        if self.profile["platform"] == "ios":
            details = self.ios("info", "details")

            def versions(value):
                if isinstance(value, dict):
                    for key, child in value.items():
                        if key == "osVersionNumber" and isinstance(child, str):
                            yield child
                        yield from versions(child)
                elif isinstance(value, list):
                    for child in value:
                        yield from versions(child)

            version = next(versions(details), "")
            if not version or "iPhoneOS" not in meta.get("platforms", []):
                raise ValueError(
                    "Physical iOS device OS/platform could not be verified"
                )
            numeric = lambda v: (
                tuple(int(n) for n in re.findall(r"\d+", v)[:3])
                + (0,) * max(0, 3 - len(re.findall(r"\d+", v)[:3]))
            )
            if meta.get("minimum_os") and numeric(version) < numeric(
                meta["minimum_os"]
            ):
                raise ValueError("App requires a newer iOS version")

    def install(self, artifact, path, mode):
        self.preflight(artifact, path)
        current = self.installed()
        if mode == "upgrade" and not current:
            raise ValueError(
                "Cover installation requires an existing App or a base build"
            )
        if (
            current
            and mode == "upgrade"
            and current.get("version_code", "").isdigit()
            and artifact["metadata"]["version_code"].isdigit()
            and int(artifact["metadata"]["version_code"]) < int(current["version_code"])
        ):
            raise ValueError("Downgrade is not an upgrade")
        app_id = self.profile["app_id"]
        if self.profile["platform"] == "android":
            with tempfile.TemporaryDirectory(prefix="iatest-selected-apks-") as output:
                if artifact.get("format") == "apks":
                    from iatest.mobile.bundles import extract_set, select_apks

                    entries = extract_set(path, output)
                    files = entries if len(entries) == 1 and entries[0].name == "universal.apk" else select_apks(path, output, self.device, self.control)
                    split_names = []
                    sdk = int(self.adb("shell", "getprop", "ro.build.version.sdk").strip())
                    abis = set(self.adb("shell", "getprop", "ro.product.cpu.abilist").strip().split(","))
                    for file in files:
                        actual = inspect_package(file, "android")
                        split_names.append(actual.get("split_name", ""))
                        if int(actual.get("minimum_os", 0) or 0) > sdk or actual.get("abis") and not abis.intersection(actual["abis"]):
                            raise ValueError("Selected APK split is incompatible with the device")
                        if any(actual.get(k) != artifact["metadata"].get(k) for k in ("app_id", "version_code", "certificate_sha256")):
                            raise ValueError("Selected APK identity/signature differs before installation")
                    if split_names.count("") != 1 or len(set(split_names)) != len(split_names):
                        raise ValueError("Selected APKs require one base and unique split names")
                else:
                    files = [path]
                if current and mode == "fresh":
                    self.adb("uninstall", app_id, timeout=60)
                self.adb("install-multiple" if len(files) > 1 else "install",
                         *(["-r"] if mode == "upgrade" else []),
                         *(str(file) for file in files), timeout=120)
        else:
            with tempfile.TemporaryDirectory(prefix="iatest-install-") as temp:
                _, app = safe_ipa(path, temp)
                if current and mode == "fresh":
                    self.ios("uninstall", "app", app_id)
                self.ios("install", "app", str(app))
        installed = self.installed()
        if (
            not installed
            or installed.get("version_code") != artifact["metadata"]["version_code"]
        ):
            raise RuntimeError(
                "Installed App build could not be confirmed; do not retry automatically"
            )
        return {
            "mode": mode,
            "before": current,
            "after": installed,
            "artifact_id": artifact["id"],
            "sha256": artifact["sha256"],
        }

    def launch(self):
        app_id = self.profile["app_id"]
        if self.profile["platform"] == "android":
            component = (
                self.adb(
                    "shell", "cmd", "package", "resolve-activity", "--brief", app_id
                )
                .strip()
                .splitlines()[-1]
            )
            if not re.fullmatch(r"[A-Za-z0-9_.$]+/[A-Za-z0-9_.$]+", component):
                raise ValueError("App has no resolvable launch activity")
            value = self.adb("shell", "am", "start", "-W", "-n", component, timeout=30)
            if "Status: ok" not in value:
                raise RuntimeError("App launch was not acknowledged")
        else:
            self.ios("process", "launch", app_id)
        return {
            "launch": "acknowledged",
            "business_readiness": "requires UI postcondition",
        }


def prepare_app(
    root, project, spec, control, *, installer_factory=AppInstaller, executor=None
):
    validate(spec, PREPARATION_SCHEMA)
    if spec.get("seed_plan_id") and not spec.get("base_artifact_id"):
        raise ValueError("An upgrade data seed requires a base build")
    if spec.get("base_artifact_id") and spec["mode"] != "upgrade":
        raise ValueError("A base build is only used for upgrade verification")
    profile = MobileStore(root, project).get("profile", spec["profile"])
    artifact, path = cached_app(root, project, spec["artifact_id"])
    installer = installer_factory(profile, control)
    result = {
        "id": "deployment-" + uuid.uuid4().hex[:20],
        "profile": spec["profile"],
        "platform": profile["platform"],
        "app_id": profile["app_id"],
        "steps": [],
        "state": "preparing",
    }
    with device_scope(profile):
        try:
            installer.preflight(artifact, path)
            if spec.get("base_artifact_id"):
                base, base_path = cached_app(root, project, spec["base_artifact_id"])
                result["steps"].append(installer.install(base, base_path, "fresh"))
                installer.launch()
                if spec.get("seed_plan_id"):
                    from iatest.application.testing_service import execute_test_plan

                    seed = EngineStore(root, project).get_plan(spec["seed_plan_id"])
                    if (
                        seed.get("preparation")
                        or seed["kind"] not in {"api", profile["platform"]}
                        or (
                            seed["kind"] != "api"
                            and seed["spec"]["profile"] != profile["id"]
                        )
                    ):
                        raise ValueError(
                            "Seed must be a local API or same-device UI plan without preparation"
                        )
                    seeded = (executor or execute_test_plan)(
                        root, project, seed, control=control
                    )
                    result["seed_run_id"] = seeded["run_id"]
                    if seeded["verdict"] != "passed":
                        raise RuntimeError("Old-version data preparation did not pass")
            result["steps"].append(installer.install(artifact, path, spec["mode"]))
            result["launch"] = installer.launch()
            result["state"] = "prepared"
        except Exception as exc:  # noqa: BLE001 -- persist environment failure; never fall back to uninstall
            result.update(state="environment_error", error=str(exc))
        EngineStore(root, project).save_artifact(
            "deployment", result, artifact_id=result["id"]
        )
    return redact(result)
