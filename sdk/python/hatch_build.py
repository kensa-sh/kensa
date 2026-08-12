from __future__ import annotations

import hashlib
import json
import os
import stat
import tomllib
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

_DESCRIPTOR_ENV = "KENSA_ENGINE_BUILD"
_DESCRIPTOR_KEYS = {
    "build_manifest",
    "build_manifest_sha256",
    "bun_target",
    "executable",
    "schema_version",
    "sha256",
    "target",
    "wheel_tag",
}
_CONTRACT_IDS = (
    "kensa.build_manifest.v1",
    "kensa.engine.v1",
    "kensa.result.v1",
)
_SCHEMA_IDS = ("evaluation", "evidence", "mining", "protection", "sync")
_CONFORMANCE_IDS = ("canonical-json", "evaluation", "redaction-proof", "trace-view")
_TARGETS = {
    "darwin-arm64": ("bun-darwin-arm64", "py3-none-macosx_11_0_arm64", "kensa-engine"),
    "darwin-x64": (
        "bun-darwin-x64-baseline",
        "py3-none-macosx_10_15_x86_64",
        "kensa-engine",
    ),
    "linux-arm64": (
        "bun-linux-arm64",
        "py3-none-manylinux_2_17_aarch64",
        "kensa-engine",
    ),
    "linux-x64": (
        "bun-linux-x64-baseline",
        "py3-none-manylinux_2_17_x86_64",
        "kensa-engine",
    ),
    "win32-x64": (
        "bun-windows-x64-baseline",
        "py3-none-win_amd64",
        "kensa-engine.exe",
    ),
}


class CustomBuildHook(BuildHookInterface):
    """Include a verified standalone engine in a platform wheel."""

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        if version == "editable":
            return
        descriptor = _load_descriptor(Path(self.root))
        root = Path(self.root)
        executable = _resolve_build_file(root, descriptor["executable"], "executable")
        expected = descriptor["sha256"]
        actual = hashlib.sha256(executable.read_bytes()).hexdigest()
        if actual != expected:
            raise RuntimeError("Kensa engine executable does not match its build descriptor")
        target = descriptor["target"]
        configuration = _TARGETS.get(target)
        if configuration is None or configuration[:2] != (
            descriptor["bun_target"],
            descriptor["wheel_tag"],
        ):
            raise RuntimeError("Kensa engine target contradicts its build descriptor")
        if executable.name != configuration[2]:
            raise RuntimeError("Kensa engine target has an invalid executable name")
        if target != "win32-x64" and not executable.stat().st_mode & stat.S_IXUSR:
            raise RuntimeError("Kensa engine executable is not executable")
        _validate_executable(executable, target)
        manifest = _resolve_build_file(root, descriptor["build_manifest"], "build manifest")
        if hashlib.sha256(manifest.read_bytes()).hexdigest() != descriptor["build_manifest_sha256"]:
            raise RuntimeError("Kensa build manifest does not match its build descriptor")
        _validate_manifest(manifest, _project_version(root))
        destination = f"kensa/bin/{configuration[2]}"
        build_data["force_include"][str(executable)] = destination
        build_data["force_include"][str(manifest)] = "kensa/build-manifest.json"
        build_data["pure_python"] = False
        build_data["tag"] = descriptor["wheel_tag"]


def _load_descriptor(root: Path) -> dict[str, str]:
    configured = os.environ.get(_DESCRIPTOR_ENV)
    if configured is None:
        raise RuntimeError(
            "KENSA_ENGINE_BUILD must identify a verified standalone engine before building a wheel"
        )
    path = Path(configured)
    if not path.is_absolute():
        path = root / path
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Could not read the Kensa engine build descriptor") from exc
    if not isinstance(value, dict) or set(value) != _DESCRIPTOR_KEYS:
        raise RuntimeError("Kensa engine build descriptor has an invalid shape")
    if not all(isinstance(item, str) and item for item in value.values()):
        raise RuntimeError("Kensa engine build descriptor values must be non-empty strings")
    if value["schema_version"] != "kensa.engine_build.v1":
        raise RuntimeError("Kensa engine build descriptor has an unsupported version")
    if not _valid_digest(value["sha256"]) or not _valid_digest(value["build_manifest_sha256"]):
        raise RuntimeError("Kensa engine build descriptor has an invalid digest")
    return value


def _resolve_build_file(root: Path, configured: str, label: str) -> Path:
    path = (root / configured).resolve()
    build_root = (root / "build" / "engine").resolve()
    if not path.is_relative_to(build_root) or not path.is_file():
        raise RuntimeError(f"Kensa engine build descriptor names an invalid {label}")
    return path


def _project_version(root: Path) -> str:
    try:
        value = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    except (OSError, tomllib.TOMLDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError("Could not read the Kensa Python project version") from exc
    if not isinstance(value, str) or not value:
        raise RuntimeError("Kensa Python project version must be a non-empty string")
    return value


def _validate_manifest(path: Path, release: str) -> None:
    try:
        value = json.loads(path.read_text())
        components = value["components"]
        sdks = components["sdks"]
        component_identities = (
            (components["core"], "@kensa/core"),
            (components["engine"], "kensa-engine"),
            (sdks["python"], "kensa"),
            (sdks["typescript"], "@kensa/sdk"),
        )
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError("Kensa build manifest has an invalid shape") from exc
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "schema_version",
            "release",
            "components",
            "contracts",
            "schemas",
            "conformance",
            "contract_digest",
            "digest",
        }
        or value["schema_version"] != "kensa.build_manifest.v1"
        or value["release"] != release
        or not isinstance(components, dict)
        or set(components) != {"core", "engine", "sdks"}
        or not isinstance(sdks, dict)
        or set(sdks) != {"python", "typescript"}
        or any(
            not _valid_component(component, name, release)
            for component, name in component_identities
        )
        or not _valid_identities(value["contracts"], _CONTRACT_IDS)
        or not _valid_identities(value["schemas"], _SCHEMA_IDS)
        or not _valid_identities(value["conformance"], _CONFORMANCE_IDS)
    ):
        raise RuntimeError("Kensa build manifest has an invalid shape")
    expected_contract_digest = _digest_json(
        {"contracts": value["contracts"], "schemas": value["schemas"]}
    )
    if value["contract_digest"] != expected_contract_digest:
        raise RuntimeError("Kensa build manifest has an invalid contract digest")
    expected_digest = _digest_json({key: item for key, item in value.items() if key != "digest"})
    if value["digest"] != expected_digest:
        raise RuntimeError("Kensa build manifest has an invalid digest")


def _valid_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _valid_component(value: object, name: str, release: str) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"name", "version", "digest"}
        and value["name"] == name
        and value["version"] == release
        and _valid_digest(value["digest"])
    )


def _valid_identities(value: object, expected_ids: tuple[str, ...]) -> bool:
    return (
        isinstance(value, list)
        and [identity.get("id") for identity in value if isinstance(identity, dict)]
        == list(expected_ids)
        and all(
            isinstance(identity, dict)
            and set(identity) == {"id", "digest"}
            and isinstance(identity["id"], str)
            and bool(identity["id"])
            and _valid_digest(identity["digest"])
            for identity in value
        )
    )


def _digest_json(value: object) -> str:
    canonical = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _validate_executable(executable: Path, target: str) -> None:
    header = executable.read_bytes()[:512]
    valid = False
    if target == "darwin-arm64":
        valid = header.startswith(bytes.fromhex("cffaedfe0c000001"))
    elif target == "darwin-x64":
        valid = header.startswith(bytes.fromhex("cffaedfe07000001"))
    elif target in {"linux-arm64", "linux-x64"}:
        machine = 183 if target == "linux-arm64" else 62
        valid = (
            len(header) >= 20
            and header[:6] == b"\x7fELF\x02\x01"
            and int.from_bytes(header[18:20], "little") == machine
        )
    elif target == "win32-x64" and len(header) >= 64 and header.startswith(b"MZ"):
        pe_offset = int.from_bytes(header[60:64], "little")
        valid = (
            pe_offset + 6 <= len(header)
            and header[pe_offset : pe_offset + 4] == b"PE\0\0"
            and int.from_bytes(header[pe_offset + 4 : pe_offset + 6], "little") == 0x8664
        )
    if not valid:
        raise RuntimeError(f"Kensa engine executable does not match target {target}")
