from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path
from typing import Any, cast

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "hatch_build", Path(__file__).parents[1] / "hatch_build.py"
)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("Could not load hatch_build.py")
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
hatch_build = cast(Any, _MODULE)
REPOSITORY = Path(__file__).parents[3]
PYTHON_PROJECT = REPOSITORY / "sdk" / "python"
PROJECT_FILES = ("README.md", "LICENSE")
GOVERNANCE_FILES = ("CODE_OF_CONDUCT.md", "CONTRIBUTING.md", "SECURITY.md")


def _descriptor(root: Path, target: str = "darwin-arm64") -> tuple[Path, dict[str, str]]:
    bun_target, wheel_tag, filename = hatch_build._TARGETS[target]
    executable = root / "build" / "engine" / target / filename
    executable.parent.mkdir(parents=True)
    executable.write_bytes(_header(target))
    executable.chmod(0o755)
    value = {
        "schema_version": "kensa.engine_build.v1",
        "target": target,
        "bun_target": bun_target,
        "executable": str(executable.relative_to(root)),
        "wheel_tag": wheel_tag,
        "sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
    }
    path = executable.with_name("engine-build.json")
    path.write_text(json.dumps(value))
    return path, value


def _hook(root: Path) -> hatch_build.CustomBuildHook:
    return hatch_build.CustomBuildHook(
        str(root), {}, cast(Any, None), cast(Any, None), str(root / "dist"), "wheel"
    )


def _header(target: str) -> bytes:
    if target == "darwin-arm64":
        return bytes.fromhex("cffaedfe0c000001")
    if target == "darwin-x64":
        return bytes.fromhex("cffaedfe07000001")
    if target in {"linux-arm64", "linux-x64"}:
        value = bytearray(20)
        value[:6] = b"\x7fELF\x02\x01"
        value[18:20] = (183 if target == "linux-arm64" else 62).to_bytes(2, "little")
        return bytes(value)
    value = bytearray(70)
    value[:2] = b"MZ"
    value[60:64] = (64).to_bytes(4, "little")
    value[64:68] = b"PE\0\0"
    value[68:70] = (0x8664).to_bytes(2, "little")
    return bytes(value)


def _native_target() -> str:
    machine = platform.machine().lower()
    architecture = "arm64" if machine in {"aarch64", "arm64"} else "x64"
    if sys.platform == "darwin":
        return f"darwin-{architecture}"
    if sys.platform.startswith("linux"):
        return f"linux-{architecture}"
    if sys.platform == "win32" and architecture == "x64":
        return "win32-x64"
    pytest.skip(f"unsupported distribution-test platform {sys.platform}-{machine}")


def test_editable_build_does_not_require_bundled_engine(tmp_path: Path) -> None:
    _hook(tmp_path).initialize("editable", {})


def test_distributions_preserve_root_project_files(tmp_path: Path) -> None:
    for filename in PROJECT_FILES:
        assert (PYTHON_PROJECT / filename).read_bytes() == (REPOSITORY / filename).read_bytes()

    repository = tmp_path / "repository"
    project = repository / "sdk" / "python"
    project.parent.mkdir(parents=True)
    shutil.copytree(
        PYTHON_PROJECT,
        project,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "build"),
    )
    for filename in GOVERNANCE_FILES:
        shutil.copyfile(REPOSITORY / filename, repository / filename)
    descriptor, _ = _descriptor(project, _native_target())
    output = tmp_path / "dist"
    environment = os.environ.copy()
    environment["KENSA_ENGINE_BUILD"] = str(descriptor)
    subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "from hatchling.build import build_sdist, build_wheel; "
                "build_sdist(sys.argv[1]); build_wheel(sys.argv[1])"
            ),
            str(output),
        ],
        cwd=project,
        env=environment,
        check=True,
    )

    sdist = next(output.glob("*.tar.gz"))
    with tarfile.open(sdist, "r:gz") as archive:
        members = {member.name: member for member in archive.getmembers()}
        version = tomllib.loads((project / "pyproject.toml").read_text())["project"]["version"]
        prefix = f"kensa-{version}/"
        for filename in (*PROJECT_FILES, *GOVERNANCE_FILES):
            member = members[f"{prefix}{filename}"]
            extracted = archive.extractfile(member)
            assert extracted is not None
            assert extracted.read() == (REPOSITORY / filename).read_bytes()

    wheel = next(output.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        license_name = next(name for name in names if name.endswith(".dist-info/licenses/LICENSE"))
        metadata = archive.read(metadata_name).decode()
        assert "License-Expression: Apache-2.0" in metadata
        assert "License-File: LICENSE" in metadata
        assert (REPOSITORY / "README.md").read_text() in metadata
        assert archive.read(license_name) == (REPOSITORY / "LICENSE").read_bytes()


@pytest.mark.parametrize("target", sorted(hatch_build._TARGETS))
def test_wheel_build_includes_verified_platform_engine(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    target: str,
) -> None:
    path, value = _descriptor(tmp_path, target)
    monkeypatch.setenv("KENSA_ENGINE_BUILD", str(path))
    build_data: dict[str, Any] = {"force_include": {}}

    _hook(tmp_path).initialize("standard", build_data)

    executable = tmp_path / value["executable"]
    assert build_data == {
        "force_include": {str(executable): f"kensa/bin/{executable.name}"},
        "pure_python": False,
        "tag": value["wheel_tag"],
    }


def test_descriptor_is_required(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("KENSA_ENGINE_BUILD", raising=False)
    with pytest.raises(RuntimeError, match="must identify"):
        hatch_build._load_descriptor(tmp_path)


@pytest.mark.parametrize("contents", ["{", "[]", '{"extra": "field"}'])
def test_descriptor_rejects_invalid_json_or_shape(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    contents: str,
) -> None:
    path = tmp_path / "descriptor.json"
    path.write_text(contents)
    monkeypatch.setenv("KENSA_ENGINE_BUILD", str(path))
    with pytest.raises(RuntimeError, match="descriptor"):
        hatch_build._load_descriptor(tmp_path)


def test_descriptor_rejects_missing_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("KENSA_ENGINE_BUILD", "missing.json")
    with pytest.raises(RuntimeError, match="Could not read"):
        hatch_build._load_descriptor(tmp_path)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("target", "", "non-empty"),
        ("schema_version", "other", "unsupported version"),
        ("sha256", "g" * 64, "invalid digest"),
    ],
)
def test_descriptor_rejects_invalid_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field: str,
    value: str,
    message: str,
) -> None:
    path, descriptor = _descriptor(tmp_path)
    descriptor[field] = value
    path.write_text(json.dumps(descriptor))
    monkeypatch.setenv("KENSA_ENGINE_BUILD", str(path.relative_to(tmp_path)))
    with pytest.raises(RuntimeError, match=message):
        hatch_build._load_descriptor(tmp_path)


def test_hook_rejects_tampered_executable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path, value = _descriptor(tmp_path)
    (tmp_path / value["executable"]).write_bytes(b"changed")
    monkeypatch.setenv("KENSA_ENGINE_BUILD", str(path))
    with pytest.raises(RuntimeError, match="does not match its build descriptor"):
        _hook(tmp_path).initialize("standard", {"force_include": {}})


@pytest.mark.parametrize("field", ["bun_target", "wheel_tag"])
def test_hook_rejects_contradictory_target_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field: str,
) -> None:
    path, descriptor = _descriptor(tmp_path)
    descriptor[field] = "other"
    path.write_text(json.dumps(descriptor))
    monkeypatch.setenv("KENSA_ENGINE_BUILD", str(path))
    with pytest.raises(RuntimeError, match="contradicts"):
        _hook(tmp_path).initialize("standard", {"force_include": {}})


def test_hook_rejects_unknown_target(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path, descriptor = _descriptor(tmp_path)
    descriptor["target"] = "other"
    path.write_text(json.dumps(descriptor))
    monkeypatch.setenv("KENSA_ENGINE_BUILD", str(path))
    with pytest.raises(RuntimeError, match="contradicts"):
        _hook(tmp_path).initialize("standard", {"force_include": {}})


def test_hook_rejects_wrong_executable_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path, descriptor = _descriptor(tmp_path)
    executable = tmp_path / descriptor["executable"]
    renamed = executable.with_name("other")
    executable.rename(renamed)
    descriptor["executable"] = str(renamed.relative_to(tmp_path))
    descriptor["sha256"] = hashlib.sha256(renamed.read_bytes()).hexdigest()
    path.write_text(json.dumps(descriptor))
    monkeypatch.setenv("KENSA_ENGINE_BUILD", str(path))
    with pytest.raises(RuntimeError, match="invalid executable name"):
        _hook(tmp_path).initialize("standard", {"force_include": {}})


def test_hook_rejects_non_executable_binary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path, descriptor = _descriptor(tmp_path)
    executable = tmp_path / descriptor["executable"]
    executable.chmod(0o644)
    monkeypatch.setenv("KENSA_ENGINE_BUILD", str(path))
    with pytest.raises(RuntimeError, match="is not executable"):
        _hook(tmp_path).initialize("standard", {"force_include": {}})


def test_hook_rejects_executable_outside_build_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path, descriptor = _descriptor(tmp_path)
    outside = tmp_path / "outside"
    outside.write_bytes(_header("darwin-arm64"))
    descriptor["executable"] = "outside"
    descriptor["sha256"] = hashlib.sha256(outside.read_bytes()).hexdigest()
    path.write_text(json.dumps(descriptor))
    monkeypatch.setenv("KENSA_ENGINE_BUILD", str(path))
    with pytest.raises(RuntimeError, match="invalid executable"):
        _hook(tmp_path).initialize("standard", {"force_include": {}})


@pytest.mark.parametrize("target", sorted(hatch_build._TARGETS))
def test_hook_rejects_wrong_executable_format(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    target: str,
) -> None:
    path, descriptor = _descriptor(tmp_path, target)
    executable = tmp_path / descriptor["executable"]
    executable.write_bytes(b"not an executable")
    descriptor["sha256"] = hashlib.sha256(executable.read_bytes()).hexdigest()
    path.write_text(json.dumps(descriptor))
    monkeypatch.setenv("KENSA_ENGINE_BUILD", str(path))
    with pytest.raises(RuntimeError, match=f"does not match target {target}"):
        _hook(tmp_path).initialize("standard", {"force_include": {}})
