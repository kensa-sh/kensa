"""Live redaction integration path for en_core_web_sm-3.8.0 only.

Runs real spaCy/Presidio readiness and one redaction pass with the pinned small
model. The large model stays covered by mocked bootstrap/checksum unit tests so
normal CI never downloads it. Requires `--run-live` and the kensa[redaction]
extra; downloads the ~12MB en_core_web_sm wheel from the pinned release URL.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kensa import redact
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

    # Simulate the lg model being unavailable so init falls back to the pinned
    # sm model; the sm wheel download, checksum, extraction, and meta.json
    # validation all run for real.
    original_download = redact._download_model_wheel

    def download_sm_only(spec: redact.SpacyModelSpec, destination: Path) -> None:
        if spec.tier == "lg":
            raise redact.RedactionBootstrapError("lg unavailable in live CI")
        original_download(spec, destination)

    monkeypatch.setattr(redact, "_download_model_wheel", download_sm_only)
    readiness = redact.ensure_redaction_ready()
    assert readiness.model == "en_core_web_sm"
    assert readiness.model_tier == "sm"
    assert readiness.fallback_used is True
    assert readiness.checksum_verified is True
    assert (tmp_path / ".kensa" / "redaction.json").exists()

    # Degraded readiness allows local and staging but blocks production.
    assert redact.assert_redaction_ready(environment="staging").model_tier == "sm"
    with pytest.raises(redact.RedactionGateError, match="production"):
        redact.assert_redaction_ready(environment="production")

    source = tmp_path / "AKIAIOSFODNN7EXAMPLE" / "alice.smith@example.com"
    source.parent.mkdir()
    source.write_text(
        json.dumps(
            {
                "id": "tr_live",
                "trace_url": (
                    "https://trace.example.com/%41KIAIOSFODNN7EXAMPLE/"
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
        environment="local",
    )

    row = load_trace_views(out, environment="local")[0]
    text = row["input"]["message"]
    assert "alice.smith@example.com" not in text
    assert "078-05-1120" not in text
    assert "(212) 555-0182" not in text
    assert "[EMAIL_ADDRESS_" in text
    attribute_keys = set(row["attributes"])
    assert any(key.startswith("[PERSON_") for key in attribute_keys)
    assert any(key.startswith("[SECRET_") for key in attribute_keys)
    assert any(key.startswith("[PHONE_NUMBER_") for key in attribute_keys)
    rendered = out.read_text()
    assert "sk-live-super-secret-value" not in rendered
    assert "AKIAIOSFODNN7EXAMPLE" not in rendered
    assert "%41KIAIOSFODNN7EXAMPLE" not in rendered
    assert "%2EeyJzdWIiOiIxIn0%2E" not in rendered
    assert "alice.smith@example.com" not in rendered
    assert "01/02/1990" not in rendered
    manifest = result.redaction
    assert manifest["version"] == "kensa.redactor.v2"
    assert manifest["mandatory"] is True
    assert manifest["value_redaction_applied"] is True
    assert manifest["model"]["tier"] == "sm"
    assert "PERSON" in manifest["detectors"]["presidio"]["entities"]
    # Production exposure of the sm-tier artifact is blocked at read time.
    with pytest.raises(redact.RedactionGateError, match="production"):
        load_trace_views(out, environment="production")
