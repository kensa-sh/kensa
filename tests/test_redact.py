from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import FakeRecognizerResult, FakeRedactionEnv, write_fake_model_dir

from kensa import redact
from kensa.redact import (
    DEFAULT_SPACY_MODEL,
    FALLBACK_SPACY_MODEL,
    DetectorKind,
    EvidenceEnvironment,
    RedactionBootstrapError,
    RedactionError,
    RedactionGateError,
    RedactionNotReadyError,
    RedactionSpan,
    Redactor,
    assert_redaction_ready,
    assert_safe_manifest,
    ensure_redaction_ready,
    missing_redaction_dependencies,
    models_root,
    read_redaction_readiness,
    readiness_path,
    redact_trace_view,
    redact_value,
    safe_manifest,
)


def _safe_manifest_dict(tier: str = "lg", **overrides: Any) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "version": "kensa.redactor.v2",
        "mandatory": True,
        "language": "en",
        "value_redaction_applied": True,
        "redaction_available": True,
        "ruleset_hash": redact.RULESET_HASH,
        "pseudonymization": "instance-counter",
        "model": {
            "name": "en_core_web_lg" if tier == "lg" else "en_core_web_sm",
            "version": "3.8.0",
            "tier": tier,
            "fallback_used": tier == "sm",
            "checksum_verified": True,
        },
    }
    manifest.update(overrides)
    return manifest


# --- helpers, environments, and readiness plumbing -----------------------------------


def test_module_availability_and_missing_dependencies() -> None:
    assert redact._module_available("json") is True
    assert redact._module_available("kensa_definitely_missing_module") is False
    assert redact._module_available("") is False
    # Reports exactly the extras absent from this environment (all four in
    # eval-only CI; none in a redaction-live environment).
    expected = tuple(
        name for name in redact.REDACTION_EXTRA_MODULES if not redact._module_available(name)
    )
    assert missing_redaction_dependencies() == expected


def test_models_root_and_readiness_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KENSA_MODELS_DIR", str(tmp_path / "models"))
    assert models_root() == tmp_path / "models"
    monkeypatch.delenv("KENSA_MODELS_DIR")
    assert models_root() == Path.home() / ".kensa" / "models"
    assert readiness_path(tmp_path) == tmp_path / ".kensa" / "redaction.json"
    monkeypatch.chdir(tmp_path)
    assert readiness_path() == tmp_path / ".kensa" / "redaction.json"


def test_normalize_environment() -> None:
    assert redact._normalize_environment(None) is EvidenceEnvironment.LOCAL
    assert redact._normalize_environment("staging") is EvidenceEnvironment.STAGING
    assert (
        redact._normalize_environment(EvidenceEnvironment.PRODUCTION)
        is EvidenceEnvironment.PRODUCTION
    )
    with pytest.raises(RedactionError, match="unknown evidence environment"):
        redact._normalize_environment("prod")


def test_package_version_lookup() -> None:
    assert redact._package_version("pytest") != "unknown"
    assert redact._package_version("kensa-definitely-missing") == "unknown"


# --- deterministic recognizers -------------------------------------------------------


def test_detect_emails_and_urls() -> None:
    spans = redact._detect_emails("mail alice@example.com now")
    assert [(span.start, span.end) for span in spans] == [(5, 22)]
    assert spans[0].entity_type == "EMAIL_ADDRESS"
    assert spans[0].detector is DetectorKind.KENSA_DETERMINISTIC
    urls = redact._detect_urls("see https://example.com/x and www.example.org")
    assert len(urls) == 2
    assert all(span.entity_type == "URL" for span in urls)


def test_detect_credit_cards_luhn_and_sanity() -> None:
    assert redact._detect_credit_cards("card 4111 1111 1111 1111 ok")
    assert redact._detect_credit_cards("card 4012888888881881 ok")
    assert redact._detect_credit_cards("card 4111-1111-1111-1111 ok")
    assert not redact._detect_credit_cards("card 4111 1111 1111 1112 ok")
    assert not redact._detect_credit_cards("card 1111111111111111 ok")
    assert not redact._detect_credit_cards("card 0000000000000000 ok")
    assert not redact._detect_credit_cards("nope 4444444444444444444444")


def test_detect_us_ssns() -> None:
    assert redact._detect_us_ssns("ssn is 078-05-1120")
    assert redact._detect_us_ssns("her number 078 05 1120")
    assert not redact._detect_us_ssns("ssn is 666-05-1120")
    assert not redact._detect_us_ssns("ssn is 900-05-1120")
    assert not redact._detect_us_ssns("ssn is 078-00-1120")
    assert not redact._detect_us_ssns("ssn is 078-05-0000")
    context = redact._detect_us_ssns("social security 078051120")
    assert context
    assert context[0].score == pytest.approx(0.75)
    assert not redact._detect_us_ssns("order id 078051120")


def test_detect_dob_dates_requires_context() -> None:
    hits = redact._detect_dob_dates("DOB: 01/02/1990 and 1990-01-02 and Jan 2, 1990")
    assert len(hits) == 3
    assert all(span.entity_type == "DATE_TIME" for span in hits)
    assert not redact._detect_dob_dates("meeting on 01/02/2026")


def test_detect_ip_and_mac_addresses() -> None:
    spans = redact._detect_ip_addresses("hosts 10.0.0.1 and ::1 and 999.1.1.1 and 12:30:45")
    rendered = {span.entity_type for span in spans}
    assert rendered == {"IP_ADDRESS"}
    assert len(spans) == 2
    macs = redact._detect_mac_addresses("mac 00:1A:2b:3C:4d:5E and 00-1a-2b-3c-4d-5e")
    assert len(macs) == 2


def test_detect_ibans() -> None:
    assert redact._detect_ibans("iban GB82WEST12345698765432 ok")
    assert not redact._detect_ibans("iban GB82WEST12345698765431 ok")


def test_detect_crypto_addresses() -> None:
    assert redact._detect_crypto_addresses("btc 1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2")
    assert not redact._detect_crypto_addresses("btc 1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN3")
    assert redact._detect_crypto_addresses("btc bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq")
    assert redact._detect_crypto_addresses("eth 0x52908400098527886e0f7030069857d2e4169ee7")
    assert redact._detect_crypto_addresses("eth 0x52908400098527886E0F7030069857D2E4169EE7")
    assert redact._detect_crypto_addresses("eth 0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed")
    assert not redact._detect_crypto_addresses("eth 0x5AAeb6053F3E94C9b9A09f33669435E7Ef1BeAed")
    digits = "0x" + "1" * 40
    assert redact._detect_crypto_addresses(f"eth {digits}")


def test_base58check_rejects_bad_alphabet_and_length() -> None:
    assert redact._base58check_valid("1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2") is True
    assert redact._base58check_valid("0BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2") is False
    assert redact._base58check_valid("111") is False


def test_detect_jwts_and_auth_headers() -> None:
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig-part"
    assert redact._detect_jwts(f"token {jwt} here")
    assert not redact._detect_jwts("plain.text.value")
    assert redact._detect_auth_headers("Authorization: Bearer abcdef1234567890")
    assert redact._detect_auth_headers("authorization: basic dXNlcjpwYXNzd29yZA==")
    assert not redact._detect_auth_headers("bearer of good news")


def test_keccak_and_eip55_vectors() -> None:
    assert redact._keccak256(b"").hex() == (
        "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    )
    assert redact._keccak256(b"abc").hex() == (
        "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"
    )
    # Multi-block absorb (200 bytes > 136-byte rate); verified against pycryptodome.
    assert redact._keccak256(b"x" * 200).hex() == (
        "3c3800defb6a25a70a2737e0716eeb5d270559ad3cad8f6abddac58802d7158e"
    )
    assert redact._rotl64(1, 64) == 1
    assert redact._eip55_valid("0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359")


# --- span merge and chunking ---------------------------------------------------------


def _span(
    start: int,
    end: int,
    entity: str = "PERSON",
    score: float = 0.85,
    detector: DetectorKind = DetectorKind.SPACY_NER,
) -> RedactionSpan:
    return RedactionSpan(start=start, end=end, entity_type=entity, score=score, detector=detector)


def test_merge_spans_union_and_labeling() -> None:
    assert redact._merge_spans([]) == []
    merged = redact._merge_spans(
        [
            _span(0, 5),
            _span(3, 10, "EMAIL_ADDRESS", 0.85, DetectorKind.KENSA_DETERMINISTIC),
            _span(20, 25, "URL", 0.9, DetectorKind.KENSA_DETERMINISTIC),
        ]
    )
    assert len(merged) == 2
    assert (merged[0].start, merged[0].end) == (0, 10)
    assert merged[0].entity_type == "EMAIL_ADDRESS"
    assert merged[0].conflicted is False
    assert merged[1].entity_type == "URL"


def test_merge_spans_conflict_and_priority() -> None:
    conflicted = redact._merge_spans(
        [
            _span(0, 5, "PERSON", 0.85, DetectorKind.SPACY_NER),
            _span(0, 5, "LOCATION", 0.85, DetectorKind.SPACY_NER),
        ]
    )
    assert conflicted[0].conflicted is True
    higher_score_wins = redact._merge_spans(
        [
            _span(0, 5, "PERSON", 0.99, DetectorKind.SPACY_NER),
            _span(0, 5, "LOCATION", 0.85, DetectorKind.SPACY_NER),
        ]
    )
    assert higher_score_wins[0].entity_type == "PERSON"
    assert higher_score_wins[0].conflicted is False


def test_chunks_short_and_long() -> None:
    assert list(redact._chunks("short")) == [(0, "short")]
    text = "a" * 12_000
    chunks = list(redact._chunks(text))
    assert chunks[0][0] == 0
    assert all(len(piece) <= 5_000 for _offset, piece in chunks)
    covered_end = max(offset + len(piece) for offset, piece in chunks)
    assert covered_end == len(text)


# --- engine loading ------------------------------------------------------------------


def test_load_engine_reports_missing_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_redaction: FakeRedactionEnv,
) -> None:
    fake_redaction.make_ready(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)

    def missing_import(name: str) -> Any:
        raise ImportError(f"missing {name}")

    monkeypatch.setattr(redact, "_import_module", missing_import)
    readiness = read_redaction_readiness()
    assert readiness is not None
    with pytest.raises(RedactionNotReadyError, match="dependencies unavailable"):
        redact._load_engine(readiness)


def test_load_engine_reports_analyzer_setup_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_redaction: FakeRedactionEnv,
) -> None:
    fake_redaction.make_ready(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    readiness = read_redaction_readiness()
    assert readiness is not None

    broken = SimpleNamespace(
        NlpEngineProvider=lambda nlp_configuration: (_ for _ in ()).throw(ValueError("bad model"))
    )
    original = redact._import_module

    def broken_import(name: str) -> Any:
        if name == "presidio_analyzer.nlp_engine":
            return broken
        return original(name)

    monkeypatch.setattr(redact, "_import_module", broken_import)
    with pytest.raises(RedactionNotReadyError, match="Presidio analyzer unavailable"):
        redact._load_engine(readiness)


def test_load_engine_rejects_empty_entity_sets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_redaction: FakeRedactionEnv,
) -> None:
    fake_redaction.make_ready(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    fake_redaction.supported_entities = []
    readiness = read_redaction_readiness()
    assert readiness is not None
    with pytest.raises(RedactionNotReadyError, match="no supported English entities"):
        redact._load_engine(readiness)


def test_load_engine_registers_explicit_recognizers_and_config(
    redaction_ready: FakeRedactionEnv,
) -> None:
    redactor = Redactor(environment="local")
    assert tuple(redaction_ready.registered_recognizers) == redact._PRESIDIO_RECOGNIZER_NAMES
    assert redaction_ready.secret_plugin_names == [
        str(plugin["name"]) for plugin in redact._DETECT_SECRETS_PLUGINS
    ]
    assert redaction_ready.secret_plugin_config == {
        "plugins_used": [dict(plugin) for plugin in redact._DETECT_SECRETS_PLUGINS]
    }
    assert redaction_ready.default_score_threshold == pytest.approx(0.3)
    configuration = redaction_ready.nlp_configuration
    assert configuration is not None
    ner_config = configuration["ner_model_configuration"]
    assert set(redact._SPACY_LABELS_TO_IGNORE) <= set(ner_config["labels_to_ignore"])
    assert configuration["models"][0]["model_name"].endswith("en_core_web_lg-3.8.0")
    assert redactor.readiness.model_tier == "lg"
    assert redactor.environment is EvidenceEnvironment.LOCAL


# --- run-level redactor --------------------------------------------------------------


def test_redactor_requires_readiness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_redaction: FakeRedactionEnv,
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(RedactionNotReadyError, match="Run kensa init"):
        Redactor(environment="local")


def test_redactor_redacts_values_with_stable_aliases(
    redaction_ready: FakeRedactionEnv,
) -> None:
    redactor = Redactor(environment="local")
    value = {
        "input": "Ask Alice to email alice@example.com",
        "output": "Alice emailed alice@example.com",
        "other": "Ask Bob",
    }
    redacted = redact_value(redactor, value)
    assert redacted["input"] == "Ask [PERSON_1] to email [EMAIL_ADDRESS_1]"
    assert redacted["output"] == "[PERSON_1] emailed [EMAIL_ADDRESS_1]"
    assert redacted["other"] == "Ask Bob"
    again = Redactor(environment="local")
    assert redact_value(again, value) == redacted


@pytest.mark.parametrize("entity_type", ["PERSON", "ORGANIZATION", "LOCATION", "NRP"])
def test_redactor_normalizes_alias_identity_within_each_trace(
    entity_type: str,
    redaction_ready: FakeRedactionEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix = f"{entity_type}:"

    def analyze(text: str) -> list[FakeRecognizerResult]:
        if not text.startswith(prefix):
            return []
        return [FakeRecognizerResult(entity_type, len(prefix), len(text), 0.85)]

    monkeypatch.setattr(redaction_ready, "analyze", analyze)
    redactor = Redactor(environment="local")

    result = redactor.redact_trace_view(
        {"input": [f"{prefix}Straße  Group", f"{prefix}\tSTRASSE GROUP \n"]}
    )

    alias = f"[{entity_type}_1]"
    assert result.trace["input"] == [f"{prefix}{alias}", f"{prefix}{alias}"]


def test_redactor_scopes_alias_identity_to_each_trace(
    redaction_ready: FakeRedactionEnv,
) -> None:
    redactor = Redactor(environment="local")

    first = redactor.redact_trace_view({"input": "Alice"})
    second = redactor.redact_trace_view({"input": "Alice"})

    assert first.trace["input"] == "[PERSON_1]"
    assert second.trace["input"] == "[PERSON_2]"
    assert redactor.manifest()["entity_instance_counts"] == {"PERSON": 2}


def test_redactor_keeps_entity_types_separate_for_same_text(
    redaction_ready: FakeRedactionEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redaction_ready.persons = []

    def analyze(text: str) -> list[FakeRecognizerResult]:
        entity_type, separator, _value = text.partition(":")
        if not separator or entity_type not in {"PERSON", "ORGANIZATION"}:
            return []
        return [FakeRecognizerResult(entity_type, len(entity_type) + 1, len(text), 0.85)]

    monkeypatch.setattr(redaction_ready, "analyze", analyze)
    redactor = Redactor(environment="local")

    result = redactor.redact_trace_view({"input": ["PERSON:Acme", "ORGANIZATION:Acme"]})

    assert result.trace["input"] == ["PERSON:[PERSON_1]", "ORGANIZATION:[ORGANIZATION_1]"]


def test_redactor_keeps_name_variants_distinct(
    redaction_ready: FakeRedactionEnv,
) -> None:
    redaction_ready.persons = ["Alice Smith", "Alice B. Smith"]
    redactor = Redactor(environment="local")

    result = redactor.redact_trace_view({"input": ["Alice Smith", "Alice B. Smith"]})

    assert result.trace["input"] == ["[PERSON_1]", "[PERSON_2]"]


def test_redactor_preserves_exact_alias_identity_for_sensitive_values(
    redaction_ready: FakeRedactionEnv,
) -> None:
    redaction_ready.persons = []
    redaction_ready.secret_markers = ["tok_Live", "tok_live"]
    redactor = Redactor(environment="local")
    lower_crypto = "0x52908400098527886e0f7030069857d2e4169ee7"
    upper_crypto = lower_crypto.upper().replace("0X", "0x")

    result = redactor.redact_trace_view(
        {
            "input": [
                "https://example.com/Case",
                "https://example.com/case",
                "tok_Live",
                "tok_live",
                lower_crypto,
                upper_crypto,
                "078-05-1120",
                "078 05 1120",
            ]
        }
    )

    assert result.trace["input"] == [
        "[URL_1]",
        "[URL_2]",
        "[SECRET_1]",
        "[SECRET_2]",
        "[CRYPTO_1]",
        "[CRYPTO_2]",
        "[US_SSN_1]",
        "[US_SSN_2]",
    ]


def test_redactor_secret_keys_and_key_rewrites(
    redaction_ready: FakeRedactionEnv,
) -> None:
    redactor = Redactor(environment="local")
    value = {
        "api_key": "super-secret",
        "attributes": {
            "alice@example.com": "contact",
            "password": {"nested": True},
        },
        "safe_key": "plain",
    }
    redacted = redactor.redact_value(value)
    assert redacted["api_key"] == "[SECRET_1]"
    assert redacted["attributes"]["[EMAIL_ADDRESS_1]"] == "contact"
    assert redacted["attributes"]["password"] == "[SECRET_2]"
    assert redacted["safe_key"] == "plain"
    # Key text outside free-form containers is never rewritten (schema preservation).
    top = redactor.redact_value({"alice@example.com": "x"})
    assert "alice@example.com" in top


def test_redactor_freeform_keys_use_full_detector_suite(
    redaction_ready: FakeRedactionEnv,
) -> None:
    redaction_ready.persons = ["Alice Smith"]
    redaction_ready.secret_markers = ["AKIAV7EXAMPLEKEY"]
    redaction_ready.phone_numbers = ["+1-202-555-0182"]
    redactor = Redactor(environment="local")

    redacted = redactor.redact_value(
        {
            "attributes": {
                "Alice Smith": "person key",
                "AKIAV7EXAMPLEKEY": "secret key",
                "+1-202-555-0182": "phone key",
            },
            "Alice Smith": "schema key",
        }
    )

    assert redacted["attributes"] == {
        "[PERSON_1]": "person key",
        "[SECRET_1]": "secret key",
        "[PHONE_NUMBER_1]": "phone key",
    }
    assert redacted["Alice Smith"] == "schema key"


def test_redactor_preserves_normalized_key_collisions(
    redaction_ready: FakeRedactionEnv,
) -> None:
    redaction_ready.persons = ["Alice", "alice"]
    redactor = Redactor(environment="local")

    redacted = redactor.redact_value(
        {"attributes": {"Alice": 1, "[REDACTED_KEY_1]": "literal", "alice": 2}}
    )

    assert redacted["attributes"] == {
        "[PERSON_1]": 1,
        "[REDACTED_KEY_1]": "literal",
        "[REDACTED_KEY_2]": 2,
    }


def test_redactor_preserves_generic_redacted_key_collisions(
    redaction_ready: FakeRedactionEnv,
) -> None:
    redaction_ready.persons = []
    redaction_ready.extra_results = [
        FakeRecognizerResult("MYSTERY_LABEL", 0, 4, 0.9, "CustomRecognizer")
    ]
    redactor = Redactor(environment="local")

    redacted = redactor.redact_value({"attributes": {"abcd": 1, "wxyz": 2}})

    assert redacted["attributes"] == {"[REDACTED]": 1, "[REDACTED_KEY_1]": 2}


def test_redactor_preserves_literal_placeholder_key_collisions(
    redaction_ready: FakeRedactionEnv,
) -> None:
    redaction_ready.persons = ["Alice"]
    redactor = Redactor(environment="local")

    redacted = redactor.redact_value({"attributes": {"Alice": "detected", "[PERSON_1]": "literal"}})

    assert redacted["attributes"] == {
        "[PERSON_1]": "detected",
        "[REDACTED_KEY_1]": "literal",
    }


def test_redactor_scalar_type_preservation(
    redaction_ready: FakeRedactionEnv,
) -> None:
    redaction_ready.secret_markers = ["1234567890"]
    redactor = Redactor(environment="local")
    value = {
        "count": 7,
        "ratio": 2.5,
        "flag": True,
        "nothing": None,
        "account_code": 1234567890,
        "items": ["Alice", 3],
    }
    redacted = redactor.redact_value(value)
    assert redacted["count"] == 7
    assert redacted["ratio"] == 2.5
    assert redacted["flag"] is True
    assert redacted["nothing"] is None
    assert redacted["account_code"] == "[SECRET_1]"
    assert redacted["items"] == ["[PERSON_1]", 3]
    assert redactor.redact_value("   ") == "   "
    marker = object()
    assert redactor.redact_value(marker) is marker


def test_redactor_timing_allowlist_exempts_date_time_only(
    redaction_ready: FakeRedactionEnv,
) -> None:
    redaction_ready.persons = []
    redactor = Redactor(environment="local")
    value = {
        "started_at_unix_nano": "DOB 01/02/1990",
        "spans": [
            {
                "started_at_unix_nano": "DOB 01/02/1990",
                "ended_at_unix_nano": "DOB 01/02/1990",
                "duration_ms": "DOB 01/02/1990",
            }
        ],
        "note": "DOB 01/02/1990",
        "input": {"timestamp": "DOB 01/02/1990"},
        "raw": {"created_at": "DOB 01/02/1990"},
        "attributes": {"end_time": "DOB 01/02/1990"},
    }
    redacted = redactor.redact_value(value)
    assert redacted["started_at_unix_nano"] == "DOB 01/02/1990"
    assert redacted["spans"][0] == {
        "started_at_unix_nano": "DOB 01/02/1990",
        "ended_at_unix_nano": "DOB 01/02/1990",
        "duration_ms": "DOB 01/02/1990",
    }
    assert redacted["note"] == "DOB [DATE_TIME_1]"
    assert redacted["input"]["timestamp"] == "DOB [DATE_TIME_1]"
    assert redacted["raw"]["created_at"] == "DOB [DATE_TIME_1]"
    assert redacted["attributes"]["end_time"] == "DOB [DATE_TIME_1]"
    redaction_ready.secret_markers = ["tok_live"]
    timing_secret = redactor.redact_value({"timestamp": "tok_live"})
    assert timing_secret["timestamp"] == "[SECRET_1]"


def test_redactor_exempts_only_generated_provenance_and_redacts_locator_paths(
    redaction_ready: FakeRedactionEnv,
) -> None:
    redaction_ready.secret_markers = ["sk-live-value"]
    redactor = Redactor(environment="local")
    trace = {
        "schema_version": "kensa.trace_view.v1",
        "source": {
            "provider": "langfuse",
            "import_run_id": "import-2026-07-10T00-00-00Z",
            "imported_at": "2026-07-10T00:00:00Z",
            "source_path": "/tmp/sk-live-value/alice@example.com",
            "source_url": (
                "https://collector.example.com/v1/sk-live-value/alice@example.com?token=raw"
            ),
            "trace_url": "https://trace.example.com/sk-live-value/alice@example.com",
        },
        "input": "https://collector.example.com/v1",
    }
    redacted = redactor.redact_value(trace)
    assert redacted["schema_version"] == "kensa.trace_view.v1"
    assert redacted["source"]["provider"] == "langfuse"
    assert redacted["source"]["import_run_id"] == "import-2026-07-10T00-00-00Z"
    assert redacted["source"]["imported_at"] == "2026-07-10T00:00:00Z"
    assert redacted["source"]["source_path"] == "/tmp/[SECRET_1]/[EMAIL_ADDRESS_1]"
    assert redacted["source"]["source_url"] == (
        "https://collector.example.com/v1/[SECRET_1]/[EMAIL_ADDRESS_1]"
    )
    assert redacted["source"]["trace_url"] == (
        "https://trace.example.com/[SECRET_1]/[EMAIL_ADDRESS_1]"
    )
    assert redacted["input"] == "[URL_1]"
    invalid_port = redactor.redact_value(
        {"source": {"source_url": "https://collector.example.com:bad/alice@example.com"}}
    )
    assert invalid_port["source"]["source_url"] == (
        "https://collector.example.com/[EMAIL_ADDRESS_1]"
    )


def test_redactor_decodes_url_segments_for_scanning_and_preserves_safe_encoding(
    redaction_ready: FakeRedactionEnv,
) -> None:
    redaction_ready.persons = []
    redaction_ready.secret_markers = ["AKIAIOSFODNN7EXAMPLE"]
    redactor = Redactor(environment="local")
    encoded_jwt = "eyJhbGciOiJIUzI1NiJ9%2EeyJzdWIiOiIxIn0%2Esig-part"

    redacted = redactor.redact_value(
        {
            "source": {
                "source_url": (
                    "https://collector.example.com/%41KIAIOSFODNN7EXAMPLE/" + encoded_jwt
                ),
                "trace_url": "https://trace.example.com/safe%20segment",
            }
        }
    )

    assert redacted["source"]["source_url"] == (
        "https://collector.example.com/[SECRET_1]/[SECRET_2]"
    )
    assert redacted["source"]["trace_url"] == "https://trace.example.com/safe%20segment"


def test_redactor_locator_redacts_ipv6_hosts(
    redaction_ready: FakeRedactionEnv,
) -> None:
    redactor = Redactor(environment="local")

    redacted = redactor.redact_value(
        {"source": {"source_url": "https://[2001:db8::1]/alice@example.com"}}
    )

    assert redacted["source"]["source_url"] == ("https://[IP_ADDRESS_1]/[EMAIL_ADDRESS_1]")


def test_redactor_locator_scans_hostname_labels(
    redaction_ready: FakeRedactionEnv,
) -> None:
    redaction_ready.persons = ["Alice"]
    redaction_ready.secret_markers = ["AKIAV7EXAMPLEKEY"]
    redactor = Redactor(environment="local")

    redacted = redactor.redact_value(
        {
            "source": {
                "source_url": "https://Alice.example.com/path",
                "trace_url": "https://AKIAV7EXAMPLEKEY.example.com/path",
            }
        }
    )

    assert redacted["source"]["source_url"] == "https://[PERSON_1].example.com/path"
    assert redacted["source"]["trace_url"] == "https://[SECRET_1].example.com/path"


def test_redactor_unknown_entity_and_conflicts_render_redacted(
    redaction_ready: FakeRedactionEnv,
) -> None:
    redaction_ready.persons = []
    redaction_ready.extra_results = [
        FakeRecognizerResult("MYSTERY_LABEL", 0, 4, 0.9, "CustomRecognizer"),
        FakeRecognizerResult("PERSON", 5, 9, 0.85, "SpacyRecognizer"),
        FakeRecognizerResult("LOCATION", 5, 9, 0.85, "SpacyRecognizer"),
    ]
    redactor = Redactor(environment="local")
    redacted = redactor.redact_value({"input": "abcd wxyz tail"})
    assert redacted["input"] == "[REDACTED] [REDACTED] tail"


def test_presidio_detector_classification() -> None:
    spacy_result = FakeRecognizerResult("PERSON", 0, 1, 0.85, "SpacyRecognizer")
    builtin_result = FakeRecognizerResult("US_SSN", 0, 1, 0.85, "UsSsnRecognizer")
    bare_ner = FakeRecognizerResult("LOCATION", 0, 1, 0.85, None)
    bare_builtin = FakeRecognizerResult("US_SSN", 0, 1, 0.85, None)
    assert redact._presidio_detector(spacy_result) is DetectorKind.SPACY_NER
    assert redact._presidio_detector(builtin_result) is DetectorKind.PRESIDIO_BUILTIN
    assert redact._presidio_detector(bare_ner) is DetectorKind.SPACY_NER
    assert redact._presidio_detector(bare_builtin) is DetectorKind.PRESIDIO_BUILTIN


def test_redactor_fails_closed_on_analyzer_errors(
    redaction_ready: FakeRedactionEnv,
) -> None:
    redactor = Redactor(environment="local")
    redaction_ready.analyzer_error = RuntimeError("model exploded")
    with pytest.raises(RedactionError, match="value redaction failed"):
        redactor.redact_value({"input": "anything at all"})
    redaction_ready.analyzer_error = RedactionError("already redaction error")
    with pytest.raises(RedactionError, match="already redaction error"):
        redactor.redact_value({"input": "anything at all"})
    redaction_ready.analyzer_error = None
    redaction_ready.secret_scan_error = RuntimeError("scanner exploded")
    with pytest.raises(RedactionError, match="value redaction failed"):
        redactor.redact_value({"input": "anything at all"})


def test_redactor_chunks_long_text(
    redaction_ready: FakeRedactionEnv,
) -> None:
    redactor = Redactor(environment="local")
    text = ("x" * 6_000) + " Alice"
    redacted = redactor.redact_value({"input": text})
    assert redacted["input"].endswith(" [PERSON_1]")
    assert len(redaction_ready.analyze_texts) >= 2


def test_redact_trace_view_counts_and_manifest(
    redaction_ready: FakeRedactionEnv,
) -> None:
    redactor = Redactor(environment="staging")
    trace = {
        "id": "tr_1",
        "input": "Alice says hi to Alice",
        "attributes": {"api_key": "secret"},
    }
    result = redact_trace_view(redactor, trace)
    assert result.trace["input"] == "[PERSON_1] says hi to [PERSON_1]"
    assert result.redacted_span_count == 3
    assert result.changed_value_count == 2
    assert result.secret_keys_redacted is True
    manifest = redactor.manifest()
    assert manifest["version"] == "kensa.redactor.v2"
    assert manifest["mandatory"] is True
    assert manifest["language"] == "en"
    assert manifest["value_redaction_applied"] is True
    assert manifest["redaction_available"] is True
    assert manifest["redacted_span_count"] == 3
    assert manifest["changed_value_count"] == 2
    assert manifest["secret_keys_redacted"] is True
    assert manifest["ruleset_hash"] == redact.RULESET_HASH
    assert manifest["pseudonymization"] == "instance-counter"
    assert manifest["entity_instance_counts"] == {"PERSON": 1, "SECRET": 1}
    assert manifest["detectors"]["presidio"]["recognizers"] == list(
        redact._PRESIDIO_RECOGNIZER_NAMES
    )
    assert manifest["detectors"]["kensa_deterministic"]["recognizers"] == list(
        redact._DETERMINISTIC_RECOGNIZER_NAMES
    )
    assert manifest["detectors"]["detect_secrets"]["version"] == "test"
    assert {"name": "AWSKeyDetector"} in manifest["detectors"]["detect_secrets"]["plugins"]
    assert manifest["detectors"]["spacy_ner"]["labels_to_ignore"] == list(
        redact._SPACY_LABELS_TO_IGNORE
    )
    assert manifest["model"] == {
        "name": "en_core_web_lg",
        "version": "3.8.0",
        "tier": "lg",
        "fallback_used": False,
        "checksum_verified": True,
    }
    assert manifest["evidence_environment"] == "staging"
    assert manifest["trace_count"] == 1
    # The value-to-alias map itself is never part of the manifest.
    assert "Alice" not in json.dumps(manifest)


def test_locate_secret_token_expansion() -> None:
    assert redact._locate_secret("token ghp_abc123 end", "ghp") == [(6, 16)]
    assert redact._locate_secret("token xyzghp123 end", "ghp") == [(6, 15)]
    assert redact._locate_secret("a tok b tok", "tok") == [(2, 5), (8, 11)]
    spans = redact._locate_secret("x tok_live y tok_live z", "tok_live")
    assert spans == [(2, 10), (13, 21)]
    assert redact._locate_secret("value", None) is None
    assert redact._locate_secret("value", "") is None
    assert redact._locate_secret("value", "absent") is None


def test_redactor_secret_hits_expand_to_tokens_and_fail_safe(
    redaction_ready: FakeRedactionEnv,
) -> None:
    redaction_ready.persons = []
    redaction_ready.secret_markers = ["ghp_abc"]
    redactor = Redactor(environment="local")
    redacted = redactor.redact_value({"input": "use ghp_abc123XYZ here"})
    assert redacted["input"] == "use [SECRET_1] here"
    # A hit that cannot be located in the value redacts the whole value.
    redaction_ready.unlocatable_secret = True
    redacted = redactor.redact_value({"input": "anything sensitive"})
    assert redacted["input"] == "[SECRET_2]"


def test_load_engine_reports_secret_plugin_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_redaction: FakeRedactionEnv,
) -> None:
    fake_redaction.make_ready(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    readiness = read_redaction_readiness()
    assert readiness is not None
    original = redact._import_module

    def broken_import(name: str) -> Any:
        if name == "detect_secrets.core.plugins.initialize":
            return SimpleNamespace(
                from_plugin_classname=lambda plugin_name: (_ for _ in ()).throw(
                    ValueError(f"unknown plugin {plugin_name}")
                )
            )
        return original(name)

    monkeypatch.setattr(redact, "_import_module", broken_import)
    with pytest.raises(RedactionNotReadyError, match="detect-secrets plugins unavailable"):
        redact._load_engine(readiness)


def test_redactor_phone_detection_via_engine(
    redaction_ready: FakeRedactionEnv,
) -> None:
    redaction_ready.persons = []
    redaction_ready.phone_numbers = ["(212) 555-0182"]
    redactor = Redactor(environment="local")
    redacted = redactor.redact_value({"input": "call (212) 555-0182 now"})
    assert redacted["input"] == "call [PHONE_NUMBER_1] now"


# --- manifest safety gates -----------------------------------------------------------


def test_safe_manifest_accepts_safe_v2() -> None:
    assert safe_manifest(_safe_manifest_dict(), environment="local") is True
    assert safe_manifest(_safe_manifest_dict(), environment="production") is True
    assert_safe_manifest(_safe_manifest_dict(), environment="staging")


@pytest.mark.parametrize(
    ("manifest", "match"),
    [
        (None, "no redaction manifest"),
        ({}, "no redaction manifest"),
        ("nope", "no redaction manifest"),
        ({"raw_source": True}, "raw source telemetry"),
        ({"version": "kensa.redactor.v1"}, "mandatory kensa.redactor.v2"),
        (_safe_manifest_dict(mandatory=False), "not marked mandatory"),
        (
            _safe_manifest_dict(value_redaction_applied=False),
            "without mandatory value redaction",
        ),
        (
            _safe_manifest_dict(redaction_available=False),
            "while redaction was unavailable",
        ),
        (_safe_manifest_dict(ruleset_hash="wrong"), "unknown ruleset"),
        (_safe_manifest_dict(language="fr"), "unsupported language"),
        (_safe_manifest_dict(pseudonymization=None), "no pseudonymization scheme"),
        (
            _safe_manifest_dict(pseudonymization="anything"),
            "unsupported pseudonymization scheme",
        ),
        (_safe_manifest_dict(model=None), "no model metadata"),
        (_safe_manifest_dict(model={"tier": "lg"}), "corrupt model metadata"),
        (
            _safe_manifest_dict(
                model={
                    "name": "untrusted-0",
                    "version": "3.8.0",
                    "tier": "lg",
                    "checksum_verified": True,
                }
            ),
            "corrupt model metadata",
        ),
        (
            _safe_manifest_dict(
                model={
                    "name": "en_core_web_lg",
                    "version": "3.8.0",
                    "tier": "xl",
                    "checksum_verified": True,
                }
            ),
            "corrupt model metadata",
        ),
        (
            _safe_manifest_dict(
                model={
                    "name": "en_core_web_lg",
                    "version": "3.8.0",
                    "tier": "lg",
                    "checksum_verified": False,
                }
            ),
            "unverified model",
        ),
    ],
)
def test_safe_manifest_rejects_unsafe_conditions(manifest: Any, match: str) -> None:
    assert safe_manifest(manifest, environment="local") is False
    with pytest.raises(RedactionGateError, match="Re-import traces with kensa import"):
        assert_safe_manifest(manifest, environment="local")


def test_safe_manifest_blocks_production_on_sm_tier() -> None:
    manifest = _safe_manifest_dict(tier="sm")
    assert safe_manifest(manifest, environment="local") is True
    assert safe_manifest(manifest, environment="staging") is True
    assert safe_manifest(manifest, environment="production") is False
    with pytest.raises(RedactionGateError, match="production trace workflows are blocked"):
        assert_safe_manifest(manifest, environment="production")


# --- readiness -----------------------------------------------------------------------


def test_read_redaction_readiness_states(tmp_path: Path) -> None:
    assert read_redaction_readiness(tmp_path) is None
    path = readiness_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{")
    with pytest.raises(RedactionNotReadyError, match="unreadable"):
        read_redaction_readiness(tmp_path)
    path.write_text(json.dumps({"schema_version": "other"}))
    with pytest.raises(RedactionNotReadyError, match="invalid"):
        read_redaction_readiness(tmp_path)
    path.write_text(
        json.dumps(
            {
                "schema_version": "kensa.redaction_readiness.v1",
                "redaction_available": True,
                "language": "en",
                "model": "en_core_web_lg",
                "model_version": "3.8.0",
                "model_tier": "lg",
                "fallback_used": False,
                "checksum_verified": True,
                "created_at": "2026-07-10T00:00:00Z",
            }
        )
    )
    readiness = read_redaction_readiness(tmp_path)
    assert readiness is not None
    assert readiness.model_tier == "lg"
    assert readiness.to_dict()["schema_version"] == "kensa.redaction_readiness.v1"


def test_assert_redaction_ready_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        redact,
        "missing_redaction_dependencies",
        lambda: redact.REDACTION_EXTRA_MODULES,
    )
    with pytest.raises(RedactionNotReadyError, match="dependencies are missing"):
        assert_redaction_ready(environment="local")


def test_assert_redaction_ready_validates_readiness_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_redaction: FakeRedactionEnv,
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(RedactionNotReadyError, match="Run kensa init"):
        assert_redaction_ready(environment="local")

    fake_redaction.make_ready(tmp_path, monkeypatch)
    assert assert_redaction_ready(environment="local").model == "en_core_web_lg"

    path = readiness_path(tmp_path)
    payload = json.loads(path.read_text())
    payload["redaction_available"] = False
    path.write_text(json.dumps(payload))
    with pytest.raises(RedactionNotReadyError, match="redaction unavailable"):
        assert_redaction_ready(environment="local")

    payload["redaction_available"] = True
    payload["checksum_verified"] = False
    path.write_text(json.dumps(payload))
    with pytest.raises(RedactionNotReadyError, match="never checksum-verified"):
        assert_redaction_ready(environment="local")

    payload["checksum_verified"] = True
    payload["model_version"] = "9.9.9"
    path.write_text(json.dumps(payload))
    with pytest.raises(RedactionNotReadyError, match="unpinned spaCy model"):
        assert_redaction_ready(environment="local")


def test_assert_redaction_ready_detects_corrupt_model_dirs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_redaction: FakeRedactionEnv,
) -> None:
    monkeypatch.chdir(tmp_path)
    fake_redaction.make_ready(tmp_path, monkeypatch)
    meta = tmp_path / "kensa-models" / "en_core_web_lg-3.8.0" / "meta.json"
    meta.unlink()
    with pytest.raises(RedactionNotReadyError, match="missing or corrupt"):
        assert_redaction_ready(environment="local")


def test_assert_redaction_ready_blocks_production_on_sm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_redaction: FakeRedactionEnv,
) -> None:
    monkeypatch.chdir(tmp_path)
    fake_redaction.make_ready(tmp_path, monkeypatch, tier="sm")
    assert assert_redaction_ready(environment="staging").model_tier == "sm"
    with pytest.raises(RedactionGateError, match="production trace workflows"):
        assert_redaction_ready(environment="production")


# --- model validation and bootstrap --------------------------------------------------


def test_validate_model_dir_mismatches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(redact, "_package_version", lambda package: "3.8.7")
    model_dir = tmp_path / DEFAULT_SPACY_MODEL.label
    write_fake_model_dir(model_dir, DEFAULT_SPACY_MODEL)
    redact._validate_model_dir(model_dir, DEFAULT_SPACY_MODEL)
    with pytest.raises(RedactionNotReadyError, match="does not match"):
        redact._validate_model_dir(model_dir, FALLBACK_SPACY_MODEL)
    monkeypatch.setattr(redact, "_package_version", lambda package: "3.9.1")
    with pytest.raises(RedactionNotReadyError, match="requires spacy"):
        redact._validate_model_dir(model_dir, DEFAULT_SPACY_MODEL)


def test_spacy_version_supported() -> None:
    assert redact._spacy_version_supported("3.8.7", ">=3.8.0,<3.9.0") is True
    assert redact._spacy_version_supported("3.9.0", ">=3.8.0,<3.9.0") is False
    assert redact._spacy_version_supported("3.7.0", ">=3.8.0") is False
    assert redact._spacy_version_supported("3.8.0", "==3.8.0") is True
    assert redact._spacy_version_supported("3.8.0", "<=3.8.0") is True
    assert redact._spacy_version_supported("3.8.1", ">3.8.0") is True
    assert redact._spacy_version_supported("3.8.0", "") is True
    assert redact._spacy_version_supported("3.8.0", "weird-clause, ,>=3.8") is True
    assert redact._spacy_version_supported("unknown", ">=3.8.0") is False
    assert redact._version_tuple("3.8.0.dev1") == (3, 8, 0)


class _FakeResponse:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    def read(self, _size: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def test_download_model_wheel_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    insecure = redact.SpacyModelSpec(
        name="en_core_web_lg",
        version="3.8.0",
        tier="lg",
        url="http://insecure.example.com/model.whl",
        sha256="0" * 64,
    )
    with pytest.raises(RedactionBootstrapError, match="HTTPS"):
        redact._download_model_wheel(insecure, tmp_path / "a.whl")

    payload = b"wheel-bytes"
    good = redact.SpacyModelSpec(
        name="en_core_web_lg",
        version="3.8.0",
        tier="lg",
        url="https://example.com/model.whl",
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    monkeypatch.setattr(redact, "_urlopen", lambda url: _FakeResponse([payload]))
    destination = tmp_path / "model.whl"
    redact._download_model_wheel(good, destination)
    assert destination.read_bytes() == payload

    bad = redact.SpacyModelSpec(
        name="en_core_web_lg",
        version="3.8.0",
        tier="lg",
        url="https://example.com/model.whl",
        sha256="f" * 64,
    )
    with pytest.raises(RedactionBootstrapError, match="checksum mismatch"):
        redact._download_model_wheel(bad, destination)

    def network_error(url: str) -> Any:
        raise OSError("connection refused")

    monkeypatch.setattr(redact, "_urlopen", network_error)
    with pytest.raises(RedactionBootstrapError, match="could not download"):
        redact._download_model_wheel(good, destination)


def _model_wheel_bytes(spec: redact.SpacyModelSpec, *, meta: dict[str, Any] | None = None) -> bytes:
    lang, _sep, name = spec.name.partition("_")
    meta_payload = meta or {
        "lang": lang,
        "name": name,
        "version": spec.version,
        "spacy_version": ">=3.8.0,<3.9.0",
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            f"{spec.name}/{spec.label}/meta.json",
            json.dumps(meta_payload),
        )
        archive.writestr(f"{spec.name}/{spec.label}/model.bin", "weights")
    return buffer.getvalue()


def test_extract_model_wheel_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(redact, "_package_version", lambda package: "3.8.7")
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    wheel = tmp_path / "model.whl"

    wheel.write_bytes(b"not a zip")
    with pytest.raises(RedactionBootstrapError, match="could not extract"):
        redact._extract_model_wheel(wheel, DEFAULT_SPACY_MODEL, models_dir)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("something/else.txt", "nope")
    wheel.write_bytes(buffer.getvalue())
    with pytest.raises(RedactionBootstrapError, match="expected model directory"):
        redact._extract_model_wheel(wheel, DEFAULT_SPACY_MODEL, models_dir)

    wheel.write_bytes(
        _model_wheel_bytes(DEFAULT_SPACY_MODEL, meta={"lang": "xx", "name": "bad", "version": "0"})
    )
    with pytest.raises(RedactionBootstrapError, match="does not match"):
        redact._extract_model_wheel(wheel, DEFAULT_SPACY_MODEL, models_dir)

    wheel.write_bytes(_model_wheel_bytes(DEFAULT_SPACY_MODEL))
    target = redact._extract_model_wheel(wheel, DEFAULT_SPACY_MODEL, models_dir)
    assert (target / "meta.json").exists()

    # Existing corrupt target is replaced atomically.
    (target / "meta.json").write_text("{}")
    wheel.write_bytes(_model_wheel_bytes(DEFAULT_SPACY_MODEL))
    target = redact._extract_model_wheel(wheel, DEFAULT_SPACY_MODEL, models_dir)
    assert json.loads((target / "meta.json").read_text())["version"] == "3.8.0"


def test_prepare_model_uses_cache_and_replaces_corrupt_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KENSA_MODELS_DIR", str(tmp_path / "models"))
    monkeypatch.setattr(redact, "_package_version", lambda package: "3.8.7")
    downloads: list[str] = []

    def fake_download(spec: redact.SpacyModelSpec, destination: Path) -> None:
        downloads.append(spec.label)
        destination.write_bytes(_model_wheel_bytes(spec))

    monkeypatch.setattr(redact, "_download_model_wheel", fake_download)
    target = redact._prepare_model(DEFAULT_SPACY_MODEL)
    assert downloads == ["en_core_web_lg-3.8.0"]
    assert (target / "meta.json").exists()

    # Valid cache short-circuits the download.
    redact._prepare_model(DEFAULT_SPACY_MODEL)
    assert downloads == ["en_core_web_lg-3.8.0"]

    # Corrupt cache is discarded and re-downloaded.
    (target / "meta.json").write_text("{}")
    redact._prepare_model(DEFAULT_SPACY_MODEL)
    assert downloads == ["en_core_web_lg-3.8.0", "en_core_web_lg-3.8.0"]


def test_ensure_redaction_ready_requires_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        redact,
        "missing_redaction_dependencies",
        lambda: redact.REDACTION_EXTRA_MODULES,
    )
    with pytest.raises(RedactionNotReadyError, match="Install kensa"):
        ensure_redaction_ready()


def test_ensure_redaction_ready_prepares_default_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_redaction: FakeRedactionEnv,
) -> None:
    monkeypatch.setenv("KENSA_MODELS_DIR", str(tmp_path / "models"))

    def fake_download(spec: redact.SpacyModelSpec, destination: Path) -> None:
        destination.write_bytes(_model_wheel_bytes(spec))

    monkeypatch.setattr(redact, "_download_model_wheel", fake_download)
    readiness = ensure_redaction_ready(tmp_path)
    assert readiness.model == "en_core_web_lg"
    assert readiness.model_tier == "lg"
    assert readiness.fallback_used is False
    assert readiness.checksum_verified is True
    payload = json.loads(readiness_path(tmp_path).read_text())
    assert payload["schema_version"] == "kensa.redaction_readiness.v1"
    assert payload["redaction_available"] is True
    assert payload["model_tier"] == "lg"


def test_ensure_redaction_ready_falls_back_to_sm_as_degraded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_redaction: FakeRedactionEnv,
) -> None:
    monkeypatch.setenv("KENSA_MODELS_DIR", str(tmp_path / "models"))

    def fake_download(spec: redact.SpacyModelSpec, destination: Path) -> None:
        if spec.tier == "lg":
            raise RedactionBootstrapError("lg download failed")
        destination.write_bytes(_model_wheel_bytes(spec))

    monkeypatch.setattr(redact, "_download_model_wheel", fake_download)
    readiness = ensure_redaction_ready(tmp_path)
    assert readiness.model == "en_core_web_sm"
    assert readiness.model_tier == "sm"
    assert readiness.fallback_used is True


def test_ensure_redaction_ready_writes_nothing_when_no_model_prepared(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_redaction: FakeRedactionEnv,
) -> None:
    monkeypatch.setenv("KENSA_MODELS_DIR", str(tmp_path / "models"))

    def fail_download(spec: redact.SpacyModelSpec, destination: Path) -> None:
        raise RedactionBootstrapError(f"{spec.label} unavailable")

    monkeypatch.setattr(redact, "_download_model_wheel", fail_download)
    with pytest.raises(RedactionBootstrapError, match="could not prepare any redaction model"):
        ensure_redaction_ready(tmp_path)
    assert not readiness_path(tmp_path).exists()


# --- packaging boundary --------------------------------------------------------------


def test_kensa_imports_and_eval_runs_never_load_the_nlp_stack(tmp_path: Path) -> None:
    """Importing kensa and running an eval must not import the heavy NLP modules.

    The manifest safety gates also work without the redaction extra installed;
    only Redactor/ensure_redaction_ready require it (AC 51-52).
    """

    eval_dir = tmp_path / "tests" / "evals"
    eval_dir.mkdir(parents=True)
    (eval_dir / "test_light.py").write_text(
        "import pytest\n"
        "from kensa.pytest import kensa_case\n"
        "from kensa import record_llm_call\n"
        "\n"
        "\n"
        "@pytest.fixture\n"
        "def kensa_run():\n"
        "    def _run(case):\n"
        "        with record_llm_call(provider='test', model='test-model'):\n"
        "            return {'ok': case.input}\n"
        "\n"
        "    return _run\n"
        "\n"
        "\n"
        "@pytest.mark.kensa(trials=1)\n"
        "@pytest.mark.parametrize('case', [kensa_case(id='light', input='hello')])\n"
        "def test_light(case, kensa_run, kensa_trace):\n"
        "    assert case.run(kensa_run) == {'ok': 'hello'}\n"
        "    assert kensa_trace.llm_turns == 1\n"
    )
    program = (
        "import sys\n"
        "import pytest\n"
        "import kensa\n"
        "import kensa.cli\n"
        "import kensa.traces\n"
        "from kensa.redact import assert_safe_manifest, safe_manifest\n"
        "from kensa.redact import RedactionGateError\n"
        "assert safe_manifest({}, environment='local') is False\n"
        "try:\n"
        "    assert_safe_manifest(None, environment='production')\n"
        "except RedactionGateError:\n"
        "    pass\n"
        "else:\n"
        "    raise AssertionError('gate must block without the redaction extra')\n"
        "code = pytest.main(['tests/evals', '-q', '-p', 'no:cacheprovider'])\n"
        "assert code == 0, code\n"
        "blocked = [m for m in ('spacy', 'presidio_analyzer', 'detect_secrets', "
        "'phonenumbers') if m in sys.modules]\n"
        "assert not blocked, blocked\n"
        "print('light-eval-ok')\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout
    assert "light-eval-ok" in completed.stdout
