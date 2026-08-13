from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_EVALUATION = """
from kensa.engine import EngineClient

engine = EngineClient()
engine.start_case("clean-install", {"id": "case", "input": None, "metadata": {}})
result = engine.complete_case(
    "clean-install",
    observation={
        "output": "ok",
        "output_recorded": True,
        "trace": {
            "spans": [],
            "agent_runs": [],
            "tools": [],
            "tool_calls": [],
            "incomplete": False,
            "incomplete_reason": None,
            "duration_ms": 0,
            "cost_usd": None,
            "known_cost_usd": None,
            "cost_available": False,
            "llm_turns": 0,
        },
        "failure": None,
    },
    runtime_outcome={"kind": "passed"},
)
engine.close()
print(result.verdict)
"""


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: verify-engine-wheel.py <wheel-or-directory>")
    candidate = Path(sys.argv[1]).resolve()
    wheels = [candidate] if candidate.is_file() else list(candidate.glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"expected one wheel at {candidate}, found {len(wheels)}")
    with tempfile.TemporaryDirectory(prefix="kensa-wheel-") as temporary:
        environment = Path(temporary) / "venv"
        _run(["uv", "venv", "--python", sys.executable, str(environment)])
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        _run(["uv", "pip", "install", "--python", str(python), str(wheels[0])])
        clean_environment = os.environ.copy()
        clean_environment.pop("KENSA_ENGINE_COMMAND", None)
        clean_environment["PATH"] = _runtime_path(environment)
        if shutil.which("node", path=clean_environment["PATH"]) is not None:
            raise RuntimeError("clean wheel environment still exposes Node")
        if shutil.which("bun", path=clean_environment["PATH"]) is not None:
            raise RuntimeError("clean wheel environment still exposes Bun")
        result = subprocess.run(
            [str(python), "-c", _EVALUATION],
            cwd=temporary,
            env=clean_environment,
            check=True,
            capture_output=True,
            text=True,
        )
        if result.stdout.strip() != "pass" or result.stderr:
            raise RuntimeError("bundled engine clean-install evaluation failed")
    print(f"verified {wheels[0].name} without an external JavaScript runtime")
    return 0


def _runtime_path(environment: Path) -> str:
    if os.name == "nt":
        system_root = Path(os.environ["SYSTEMROOT"])
        paths = [environment / "Scripts", system_root / "System32", system_root]
    else:
        paths = [environment / "bin", Path("/usr/bin"), Path("/bin")]
    return os.pathsep.join(str(path) for path in paths)


def _run(command: list[str]) -> None:
    subprocess.run(command, check=True)


if __name__ == "__main__":
    raise SystemExit(main())
