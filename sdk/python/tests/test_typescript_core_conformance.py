from __future__ import annotations

import json
from pathlib import Path

VECTORS = Path(__file__).parents[3] / "packages" / "core" / "conformance" / "evaluation.json"


def test_python_can_consume_core_evaluation_vectors() -> None:
    vectors = json.loads(VECTORS.read_text())

    names = [vector["name"] for vector in vectors["valid"]]
    names.append(vectors["multi_check"]["name"])
    names.append(vectors["cancelled"]["name"])
    names.extend(vector["name"] for vector in vectors["invalid"])

    assert vectors["version"] == 1
    assert len(names) == len(set(names))
    assert json.loads(json.dumps(vectors, allow_nan=False)) == vectors
