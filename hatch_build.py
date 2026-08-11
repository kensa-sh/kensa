from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

_DESCRIPTOR_ENV = "KENSA_ENGINE_BUILD"
_DESCRIPTOR_KEYS = {
    "bun_target",
    "executable",
    "schema_version",
    "sha256",
    "target",
    "wheel_tag",
}
_TARGETS = {
    "darwin-arm64": ("bun-darwin-arm64", "py3-none-macosx_11_0_arm64", "kensa-engine"),
    "darwin-x64": (
        "bun-darwin-x64-baseline",
        "py3-none-macosx_10_15_x86_64",
        "kensa-engine",
    ),
    "linux-arm64": (
        "bun-linux-arm64-musl",
        "py3-none-manylinux_2_17_aarch64",
        "kensa-engine",
    ),
    "linux-x64": (
        "bun-linux-x64-musl",
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
        executable = _resolve_executable(Path(self.root), descriptor)
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
        _validate_executable(executable, target)
        destination = f"kensa/bin/{configuration[2]}"
        build_data["force_include"][str(executable)] = destination
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
    if len(value["sha256"]) != 64 or any(
        character not in "0123456789abcdef" for character in value["sha256"]
    ):
        raise RuntimeError("Kensa engine build descriptor has an invalid digest")
    return value


def _resolve_executable(root: Path, descriptor: dict[str, str]) -> Path:
    executable = (root / descriptor["executable"]).resolve()
    build_root = (root / "build" / "engine").resolve()
    if not executable.is_relative_to(build_root) or not executable.is_file():
        raise RuntimeError("Kensa engine build descriptor names an invalid executable")
    return executable


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
