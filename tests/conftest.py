from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kensa import redact

pytest_plugins = ("pytester",)


class FakeRecognizerResult:
    def __init__(
        self,
        entity_type: str,
        start: int,
        end: int,
        score: float,
        recognizer_name: str | None = "SpacyRecognizer",
    ) -> None:
        self.entity_type = entity_type
        self.start = start
        self.end = end
        self.score = score
        self.recognition_metadata = (
            {"recognizer_name": recognizer_name} if recognizer_name is not None else None
        )


class FakeRedactionEnv:
    """Fake spaCy/Presidio/detect-secrets/phonenumbers stack for unit tests."""

    def __init__(self) -> None:
        self.persons: list[str] = ["Alice"]
        self.secret_markers: list[str] = ["tok_live"]
        self.phone_numbers: list[str] = []
        self.extra_results: list[FakeRecognizerResult] = []
        self.analyzer_error: Exception | None = None
        self.secret_scan_error: Exception | None = None
        self.supported_entities: list[str] = [
            "CREDIT_CARD",
            "DATE_TIME",
            "EMAIL_ADDRESS",
            "LOCATION",
            "NRP",
            "ORGANIZATION",
            "PERSON",
            "PHONE_NUMBER",
            "US_SSN",
        ]
        self.analyze_texts: list[str] = []
        self.registered_recognizers: list[str] = []
        self.nlp_configuration: dict[str, Any] | None = None
        self.default_score_threshold: float | None = None
        self.secret_plugin_names: list[str] = []
        self.secret_plugin_config: dict[str, Any] | None = None
        self.unlocatable_secret = False

    def analyze(self, text: str) -> list[FakeRecognizerResult]:
        if self.analyzer_error is not None:
            raise self.analyzer_error
        self.analyze_texts.append(text)
        results: list[FakeRecognizerResult] = []
        for person in self.persons:
            cursor = 0
            while (index := text.find(person, cursor)) != -1:
                results.append(FakeRecognizerResult("PERSON", index, index + len(person), 0.85))
                cursor = index + len(person)
        results.extend(result for result in self.extra_results if result.end <= len(text))
        return results

    def analyze_secret_line(self, line: str) -> list[Any]:
        if self.secret_scan_error is not None:
            raise self.secret_scan_error
        if self.unlocatable_secret:
            return [SimpleNamespace(secret_value=None)]
        return [
            SimpleNamespace(secret_value=marker) for marker in self.secret_markers if marker in line
        ]

    def phone_matches(self, text: str) -> list[Any]:
        matches: list[Any] = []
        for number in self.phone_numbers:
            index = text.find(number)
            if index != -1:
                matches.append(SimpleNamespace(start=index, end=index + len(number)))
        return matches

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        env = self

        class FakeTransientSettings:
            def __init__(self, config: dict[str, Any]) -> None:
                env.secret_plugin_config = config

            def __enter__(self) -> None:
                return None

            def __exit__(self, *args: object) -> None:
                return None

        class FakeSecretPlugin:
            def __init__(self, name: str) -> None:
                self._name = name

            def analyze_line(self, filename: str, line: str) -> list[Any]:
                assert filename == "kensa-trace-import"
                return env.analyze_secret_line(line)

        def from_plugin_classname(name: str) -> FakeSecretPlugin:
            env.secret_plugin_names.append(name)
            return FakeSecretPlugin(name)

        class FakeProvider:
            def __init__(self, nlp_configuration: dict[str, Any]) -> None:
                env.nlp_configuration = nlp_configuration

            def create_engine(self) -> str:
                return "nlp-engine"

        class FakeRegistry:
            def __init__(self, supported_languages: list[str]) -> None:
                assert supported_languages == ["en"]

            def add_recognizer(self, recognizer: Any) -> None:
                env.registered_recognizers.append(type(recognizer).__name__)

        class FakeAnalyzer:
            def __init__(
                self,
                nlp_engine: str,
                registry: Any,
                supported_languages: list[str],
                default_score_threshold: float,
            ) -> None:
                assert nlp_engine == "nlp-engine"
                assert supported_languages == ["en"]
                env.default_score_threshold = default_score_threshold

            def get_supported_entities(self, language: str) -> list[str]:
                assert language == "en"
                return env.supported_entities

            def analyze(self, text: str, language: str) -> list[FakeRecognizerResult]:
                assert language == "en"
                return env.analyze(text)

        class FakeMatcher:
            def __init__(self, text: str, region: str) -> None:
                assert region
                self._matches = env.phone_matches(text)

            def __iter__(self) -> Any:
                return iter(self._matches)

        def make_recognizer(name: str) -> type:
            def _init(self: Any, supported_language: str) -> None:
                assert supported_language == "en"

            return type(name, (), {"__init__": _init})

        recognizers = {name: make_recognizer(name) for name in redact._PRESIDIO_RECOGNIZER_NAMES}
        modules: dict[str, Any] = {
            "presidio_analyzer": SimpleNamespace(
                RecognizerRegistry=FakeRegistry,
                AnalyzerEngine=FakeAnalyzer,
            ),
            "presidio_analyzer.predefined_recognizers": SimpleNamespace(**recognizers),
            "presidio_analyzer.nlp_engine": SimpleNamespace(NlpEngineProvider=FakeProvider),
            "detect_secrets.settings": SimpleNamespace(transient_settings=FakeTransientSettings),
            "detect_secrets.core.plugins.initialize": SimpleNamespace(
                from_plugin_classname=from_plugin_classname
            ),
            "phonenumbers": SimpleNamespace(PhoneNumberMatcher=FakeMatcher),
        }
        monkeypatch.setattr(redact, "_import_module", lambda name: modules[name])
        monkeypatch.setattr(redact, "_module_available", lambda name: True)
        monkeypatch.setattr(
            redact,
            "_package_version",
            lambda package: "3.8.7" if package == "spacy" else "test",
        )

    def make_ready(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        checksum_verified: bool = True,
    ) -> None:
        spec = redact.DEFAULT_SPACY_MODEL
        models_dir = tmp_path / "kensa-models"
        monkeypatch.setenv("KENSA_MODELS_DIR", str(models_dir))
        write_fake_model_dir(models_dir / spec.label, spec)
        readiness = redact.RedactionReadiness(
            model=spec.name,
            model_version=spec.version,
            checksum_verified=checksum_verified,
        )
        path = redact.settings_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = (
            json.loads(path.read_text())
            if path.exists()
            else {"schema_version": "kensa.settings.v1"}
        )
        payload["redaction"] = readiness.to_dict()
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def write_fake_model_dir(path: Path, spec: redact.SpacyModelSpec) -> None:
    path.mkdir(parents=True, exist_ok=True)
    lang, _separator, name = spec.name.partition("_")
    (path / "meta.json").write_text(
        json.dumps(
            {
                "lang": lang,
                "name": name,
                "version": spec.version,
                "spacy_version": ">=3.8.0,<3.9.0",
            }
        )
    )


@pytest.fixture
def fake_redaction(monkeypatch: pytest.MonkeyPatch) -> FakeRedactionEnv:
    env = FakeRedactionEnv()
    env.install(monkeypatch)
    return env


@pytest.fixture
def redaction_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_redaction: FakeRedactionEnv,
) -> FakeRedactionEnv:
    monkeypatch.chdir(tmp_path)
    fake_redaction.make_ready(tmp_path, monkeypatch)
    return fake_redaction


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-live",
        action="store_true",
        default=False,
        help="Run opt-in live tests using external services and provider API keys.",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "live: opt-in tests that call external services")
    config.addinivalue_line("markers", "openai: live OpenAI provider tests")
    config.addinivalue_line("markers", "anthropic: live Anthropic provider tests")
    if config.getoption("--run-live"):
        from dotenv import load_dotenv

        load_dotenv(Path.cwd() / ".env")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if not config.getoption("--run-live"):
        skip_live = pytest.mark.skip(reason="live tests require --run-live")
        for item in items:
            if "live" in item.keywords:
                item.add_marker(skip_live)
        return

    provider_skips = {
        "openai": ("OPENAI_API_KEY", pytest.mark.skip(reason="OPENAI_API_KEY is not set")),
        "anthropic": (
            "ANTHROPIC_API_KEY",
            pytest.mark.skip(reason="ANTHROPIC_API_KEY is not set"),
        ),
    }
    for item in items:
        if "live" in item.keywords:
            for marker_name, (env_name, skip_marker) in provider_skips.items():
                if marker_name in item.keywords and not os.environ.get(env_name):
                    item.add_marker(skip_marker)


@pytest.fixture(autouse=True)
def _configure_pytester_projects(request: pytest.FixtureRequest) -> None:
    if "pytester" not in request.fixturenames:
        return
    pytester = request.getfixturevalue("pytester")
    pytester.makeini(
        """
[pytest]
asyncio_default_fixture_loop_scope = function
"""
    )
