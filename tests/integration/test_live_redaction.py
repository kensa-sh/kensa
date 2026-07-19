"""Live redaction integration path for en_core_web_sm-3.8.0 only.

Runs real spaCy/Presidio readiness and one redaction pass with the pinned small
model. Requires `--run-live` and the kensa[redaction] extra; downloads the ~12MB
en_core_web_sm wheel from the pinned release URL.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kensa import redact
from kensa.config import update_project_config
from kensa.traces import import_trace_source, load_trace_views

pytestmark = pytest.mark.live


@pytest.fixture(autouse=True)
def _require_redaction_extra() -> None:
    missing = redact.missing_redaction_dependencies()
    if missing:
        pytest.skip(f"kensa[redaction] extra not installed: {', '.join(missing)}")


def test_live_sm_readiness_and_redaction_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KENSA_MODELS_DIR", str(tmp_path / "models"))

    readiness = redact.ensure_redaction_ready()
    assert readiness.model == "en_core_web_sm"
    assert readiness.checksum_verified is True
    update_project_config({"redaction_model": "small"}, start=tmp_path)
    assert redact.assert_redaction_ready().model == "en_core_web_sm"

    source = tmp_path / "AKIAIOSFODNN7EXAMPLE" / "alice.smith@example.com"
    source.parent.mkdir()
    source.write_text(
        json.dumps(
            {
                "id": "tr_live",
                "trace_url": (
                    "https://10.0.0.1/%41KIAIOSFODNN7EXAMPLE/"
                    "eyJhbGciOiJIUzI1NiJ9%2EeyJzdWIiOiIxIn0%2Esig-part/"
                    "alice.smith%40example.com"
                ),
                "input": {
                    "message": (
                        "Customer Alice Smith asked for a refund. "
                        "Reach her at alice.smith@example.com or (212) 555-0182. "
                        "SSN 078-05-1120. api_token=AKIAIOSFODNN7EXAMPLE"
                    ),
                    "timestamp": "01/02/1990",
                },
                "attributes": {
                    "Alice Smith": "person key",
                    "AKIAIOSFODNN7EXAMPLE": "secret key",
                    "+1-202-555-0182": "phone key",
                },
                "api_key": "sk-live-super-secret-value",
            }
        )
        + "\n"
    )
    out = tmp_path / "imports" / "live.jsonl"
    result = import_trace_source(
        provider="jsonl",
        source=str(source),
        out=out,
        limit=1,
        max_payload_bytes=source.stat().st_size,
    )

    row = load_trace_views(out)[0]
    text = row["input"]["message"]
    assert "alice.smith@example.com" not in text
    assert "078-05-1120" not in text
    assert "(212) 555-0182" not in text
    assert "[EMAIL_ADDRESS_" in text
    assert "attributes" not in row
    assert "raw" not in row
    assert "trace_url" not in row["source"]
    rendered = out.read_text()
    assert "sk-live-super-secret-value" not in rendered
    assert "AKIAIOSFODNN7EXAMPLE" not in rendered
    assert "%41KIAIOSFODNN7EXAMPLE" not in rendered
    assert "%2EeyJzdWIiOiIxIn0%2E" not in rendered
    assert "10.0.0.1" not in rendered
    assert "alice.smith@example.com" not in rendered
    assert "01/02/1990" not in rendered
    manifest = result.redaction
    assert manifest["version"] == "kensa.redactor.v2"
    assert manifest["mandatory"] is True
    assert manifest["value_redaction_applied"] is True
    assert manifest["model"]["name"] == "en_core_web_sm"
    assert "PERSON" in manifest["detectors"]["presidio"]["entities"]
