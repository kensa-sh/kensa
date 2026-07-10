"""Mandatory trace redaction: engine, readiness, model bootstrap, and manifest gates.

Every trace evidence boundary in Kensa runs through this module. Import-time
redaction uses a run-level :class:`Redactor`; read-time exposure uses the pure
manifest gates :func:`safe_manifest` / :func:`assert_safe_manifest`, which work
without the optional ``kensa[redaction]`` dependencies installed. Heavy NLP
imports (spaCy, Presidio, detect-secrets, phonenumbers) stay function-local so
eval-only installs never load them.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import importlib.util
import ipaddress
import json
import os
import re
import shutil
import tempfile
import urllib.request
import zipfile
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from re import Match
from typing import Any, cast

REDACTOR_MANIFEST_VERSION = "kensa.redactor.v2"
REDACTION_READINESS_SCHEMA_VERSION = "kensa.redaction_readiness.v1"
REDACTION_READINESS_PATH = Path(".kensa") / "redaction.json"
REDACTION_EXTRA_MODULES = ("spacy", "presidio_analyzer", "detect_secrets", "phonenumbers")
REDACTED_PLACEHOLDER = "[REDACTED]"
PSEUDONYMIZATION_SCHEME = "instance-counter"
LANGUAGE = "en"
_MODELS_DIR_ENV = "KENSA_MODELS_DIR"


class RedactionError(ValueError):
    """Base error for mandatory redaction failures. Redaction fails closed."""


class RedactionNotReadyError(RedactionError):
    """Redaction dependencies or model readiness are missing."""


class RedactionGateError(RedactionError):
    """A trace artifact is unsafe to expose and must be re-imported."""


class RedactionBootstrapError(RedactionError):
    """Model download, verification, or extraction failed during kensa init."""


class EvidenceEnvironment(StrEnum):
    LOCAL = "local"
    STAGING = "staging"
    PRODUCTION = "production"


class DetectorKind(StrEnum):
    KENSA_DETERMINISTIC = "kensa-deterministic"
    DETECT_SECRETS = "detect-secrets"
    PRESIDIO_BUILTIN = "presidio-builtin"
    SPACY_NER = "spacy-ner"


class EntityType(StrEnum):
    """Active entity catalog for v1 (US, UK, EU, and AU markets, English NLP)."""

    PERSON = "PERSON"
    LOCATION = "LOCATION"
    ORGANIZATION = "ORGANIZATION"
    NRP = "NRP"
    DATE_TIME = "DATE_TIME"
    EMAIL_ADDRESS = "EMAIL_ADDRESS"
    PHONE_NUMBER = "PHONE_NUMBER"
    CREDIT_CARD = "CREDIT_CARD"
    CRYPTO = "CRYPTO"
    IBAN_CODE = "IBAN_CODE"
    IP_ADDRESS = "IP_ADDRESS"
    MAC_ADDRESS = "MAC_ADDRESS"
    MEDICAL_LICENSE = "MEDICAL_LICENSE"
    URL = "URL"
    SECRET = "SECRET"
    US_BANK_NUMBER = "US_BANK_NUMBER"
    US_DRIVER_LICENSE = "US_DRIVER_LICENSE"
    US_ITIN = "US_ITIN"
    US_MBI = "US_MBI"
    US_NPI = "US_NPI"
    US_PASSPORT = "US_PASSPORT"
    US_SSN = "US_SSN"
    UK_NHS = "UK_NHS"
    UK_NINO = "UK_NINO"
    UK_PASSPORT = "UK_PASSPORT"
    UK_POSTCODE = "UK_POSTCODE"
    UK_VEHICLE_REGISTRATION = "UK_VEHICLE_REGISTRATION"
    AU_ABN = "AU_ABN"
    AU_ACN = "AU_ACN"
    AU_TFN = "AU_TFN"
    AU_MEDICARE = "AU_MEDICARE"
    ES_NIF = "ES_NIF"
    ES_NIE = "ES_NIE"
    IT_FISCAL_CODE = "IT_FISCAL_CODE"
    IT_DRIVER_LICENSE = "IT_DRIVER_LICENSE"
    IT_VAT_CODE = "IT_VAT_CODE"
    IT_PASSPORT = "IT_PASSPORT"
    IT_IDENTITY_CARD = "IT_IDENTITY_CARD"
    PL_PESEL = "PL_PESEL"
    FI_PERSONAL_IDENTITY_CODE = "FI_PERSONAL_IDENTITY_CODE"


_KNOWN_ENTITY_LABELS = frozenset(str(entity) for entity in EntityType)

# Catalog entities with no built-in implementation in the pinned Presidio release and no
# Kensa deterministic recognizer in this pass. Deferred to the future list (AC 62); their
# identifiers fall back to generic NER or [REDACTED] rather than a typed placeholder.
FUTURE_ENTITIES = frozenset(
    {
        "DE_HANDELSREGISTER",
        "DE_HEALTH_INSURANCE",
        "DE_ID_CARD",
        "DE_KFZ",
        "DE_PASSPORT",
        "DE_PLZ",
        "DE_SOCIAL_SECURITY",
        "DE_TAX_ID",
        "DE_TAX_NUMBER",
        "ES_PASSPORT",
        "SE_ORGANISATIONSNUMMER",
        "SE_PERSONNUMMER",
        "UK_DRIVING_LICENCE",
    }
)


@dataclass(frozen=True)
class SpacyModelSpec:
    name: str
    version: str
    tier: str
    url: str
    sha256: str

    @property
    def label(self) -> str:
        return f"{self.name}-{self.version}"


DEFAULT_SPACY_MODEL = SpacyModelSpec(
    name="en_core_web_lg",
    version="3.8.0",
    tier="lg",
    url=(
        "https://github.com/explosion/spacy-models/releases/download/"
        "en_core_web_lg-3.8.0/en_core_web_lg-3.8.0-py3-none-any.whl"
    ),
    sha256="293e9547a655b25499198ab15a525b05b9407a75f10255e405e8c3854329ab63",
)
FALLBACK_SPACY_MODEL = SpacyModelSpec(
    name="en_core_web_sm",
    version="3.8.0",
    tier="sm",
    url=(
        "https://github.com/explosion/spacy-models/releases/download/"
        "en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl"
    ),
    sha256="1932429db727d4bff3deed6b34cfc05df17794f4a52eeb26cf8928f7c1a0fb85",
)
_PINNED_SPACY_MODELS = (DEFAULT_SPACY_MODEL, FALLBACK_SPACY_MODEL)

# Recall-favoring detection thresholds. Constants by design; not user-configurable.
_SCORE_SECRET = 1.0
_SCORE_PARSER = 0.95
_SCORE_PATTERN = 0.85
_SCORE_CONTEXT = 0.75
_PRESIDIO_SCORE_THRESHOLD = 0.3
_DETECTOR_PRIORITY = {
    DetectorKind.KENSA_DETERMINISTIC: 3,
    DetectorKind.DETECT_SECRETS: 2,
    DetectorKind.PRESIDIO_BUILTIN: 1,
    DetectorKind.SPACY_NER: 0,
}

# Long strings are chunked with overlap for NLP analysis; nothing is truncated.
_CHUNK_CHARS = 5_000
_CHUNK_OVERLAP = 500

_SECRET_KEY = re.compile(r"(secret|token|password|api[_-]?key|authorization|credential)", re.I)

# Schema-owned timing fields are exempt from DATE_TIME only; every other entity and
# secret scanning still applies to them (folded into the ruleset hash).
_TIMING_FIELD_ALLOWLIST = frozenset(
    {
        "checked_at",
        "created_at",
        "duration_ms",
        "endTime",
        "end_time",
        "end_time_unix_nano",
        "ended_at_unix_nano",
        "imported_at",
        "startTime",
        "start_time",
        "start_time_unix_nano",
        "started_at_unix_nano",
        "timeUnixNano",
        "time_unix_nano",
        "timestamp",
    }
)
# Kensa-generated provenance subtree on TraceView rows; trace_url is sanitized with
# safe_endpoint at import time. The top-level schema_version value is a Kensa
# constant written by the importer itself and can never carry payload data. Both
# are folded into the ruleset hash.
_PROVENANCE_PATHS = (("source",), ("schema_version",))
# Dict keys may only be rewritten inside free-form payload containers, never where the
# key is part of the TraceView/SpanView schema.
_FREEFORM_CONTAINERS = frozenset({"attributes", "raw", "input", "output", "events", "metadata"})

_PRESIDIO_RECOGNIZER_NAMES = (
    "AuAbnRecognizer",
    "AuAcnRecognizer",
    "AuMedicareRecognizer",
    "AuTfnRecognizer",
    "CreditCardRecognizer",
    "CryptoRecognizer",
    "DateRecognizer",
    "EmailRecognizer",
    "EsNieRecognizer",
    "EsNifRecognizer",
    "FiPersonalIdentityCodeRecognizer",
    "IbanRecognizer",
    "IpRecognizer",
    "ItDriverLicenseRecognizer",
    "ItFiscalCodeRecognizer",
    "ItIdentityCardRecognizer",
    "ItPassportRecognizer",
    "ItVatCodeRecognizer",
    "MacAddressRecognizer",
    "MedicalLicenseRecognizer",
    "NhsRecognizer",
    "PhoneRecognizer",
    "PlPeselRecognizer",
    "SpacyRecognizer",
    "UkNinoRecognizer",
    "UkPassportRecognizer",
    "UkPostcodeRecognizer",
    "UkVehicleRegistrationRecognizer",
    "UrlRecognizer",
    "UsBankRecognizer",
    "UsItinRecognizer",
    "UsLicenseRecognizer",
    "UsMbiRecognizer",
    "UsNpiRecognizer",
    "UsPassportRecognizer",
    "UsSsnRecognizer",
)
# spaCy labels with no Presidio entity mapping are dropped to avoid log flooding on
# high-field-count payloads; this changes no redaction outcome (AC 45).
_SPACY_LABELS_TO_IGNORE = (
    "CARDINAL",
    "EVENT",
    "FAC",
    "LANGUAGE",
    "LAW",
    "MONEY",
    "ORDINAL",
    "PERCENT",
    "PRODUCT",
    "QUANTITY",
    "WORK_OF_ART",
)
_SPACY_ENTITY_MAPPING = {
    "PERSON": "PERSON",
    "PER": "PERSON",
    "GPE": "LOCATION",
    "LOC": "LOCATION",
    "ORG": "ORGANIZATION",
    "NORP": "NRP",
    "DATE": "DATE_TIME",
    "TIME": "DATE_TIME",
}
_NER_ENTITY_LABELS = frozenset({"PERSON", "LOCATION", "ORGANIZATION", "NRP"})
_PHONE_REGIONS = ("US", "GB", "AU", "DE", "ES", "FR", "IT", "PL", "FI", "SE")
# Explicit detect-secrets plugin configuration (pinned detect-secrets==1.5.0).
# Plugins are instantiated directly and called through analyze_line: the ad-hoc
# scan_line helper forces eager entropy search, which ignores the entropy limits
# and flags nearly every string. Entropy limits are the library defaults.
_DETECT_SECRETS_PLUGINS: tuple[dict[str, Any], ...] = (
    {"name": "AWSKeyDetector"},
    {"name": "ArtifactoryDetector"},
    {"name": "AzureStorageKeyDetector"},
    {"name": "Base64HighEntropyString", "limit": 4.5},
    {"name": "BasicAuthDetector"},
    {"name": "CloudantDetector"},
    {"name": "DiscordBotTokenDetector"},
    {"name": "GitHubTokenDetector"},
    {"name": "GitLabTokenDetector"},
    {"name": "HexHighEntropyString", "limit": 3.0},
    {"name": "IPPublicDetector"},
    {"name": "IbmCloudIamDetector"},
    {"name": "IbmCosHmacDetector"},
    {"name": "JwtTokenDetector"},
    {"name": "KeywordDetector"},
    {"name": "MailchimpDetector"},
    {"name": "NpmDetector"},
    {"name": "OpenAIDetector"},
    {"name": "PrivateKeyDetector"},
    {"name": "PypiTokenDetector"},
    {"name": "SendGridDetector"},
    {"name": "SlackDetector"},
    {"name": "SoftlayerDetector"},
    {"name": "SquareOAuthDetector"},
    {"name": "StripeDetector"},
    {"name": "TelegramBotTokenDetector"},
    {"name": "TwilioKeyDetector"},
)

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_URL_RE = re.compile(r"\b(?:https?://|www\.)[^\s\"'<>`)\]]+", re.I)
_CARD_RE = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")
_SSN_DELIMITED_RE = re.compile(r"\b(\d{3})[- ](\d{2})[- ](\d{4})\b")
_SSN_PLAIN_RE = re.compile(r"\b(\d{3})(\d{2})(\d{4})\b")
_SSN_CONTEXT_RE = re.compile(r"\b(?:ssn|social\s+security)\b", re.I)
_DATE_RE = re.compile(
    r"\b(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2}"
    r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? \d{1,2},? \d{4})\b"
)
_DOB_CONTEXT_RE = re.compile(r"\b(?:dob|date\s+of\s+birth|birth\s?date|born)\b", re.I)
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6_RE = re.compile(
    r"(?<![0-9A-Fa-f:.])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![0-9A-Fa-f:.])"
)
_MAC_RE = re.compile(r"\b[0-9A-Fa-f]{2}(?:([:-])[0-9A-Fa-f]{2}){5}\b")
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b")
_BTC_BASE58_RE = re.compile(r"\b[13][1-9A-HJ-NP-Za-km-z]{24,34}\b")
_BTC_BECH32_RE = re.compile(r"\bbc1[02-9ac-hj-np-z]{8,87}\b")
_ETH_RE = re.compile(r"\b0x[0-9a-fA-F]{40}\b")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]*\.eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]*")
_AUTH_HEADER_RE = re.compile(r"\b(?:bearer|basic)\s+[A-Za-z0-9\-._~+/]{12,}=*", re.I)
_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


@dataclass(frozen=True)
class RedactionSpan:
    start: int
    end: int
    entity_type: str
    score: float
    detector: DetectorKind


@dataclass(frozen=True)
class RedactionResult:
    trace: dict[str, Any]
    redacted_span_count: int
    changed_value_count: int
    secret_keys_redacted: bool


@dataclass(frozen=True)
class RedactionReadiness:
    redaction_available: bool
    language: str
    model: str
    model_version: str
    model_tier: str
    fallback_used: bool
    checksum_verified: bool
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": REDACTION_READINESS_SCHEMA_VERSION,
            "redaction_available": self.redaction_available,
            "language": self.language,
            "model": self.model,
            "model_version": self.model_version,
            "model_tier": self.model_tier,
            "fallback_used": self.fallback_used,
            "checksum_verified": self.checksum_verified,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class _MergedSpan:
    start: int
    end: int
    entity_type: str
    conflicted: bool


@dataclass(frozen=True)
class _EngineHandle:
    analyzer: Any
    entities: tuple[str, ...]
    detect_secret_spans: Callable[[str], list[tuple[int, int]]]
    phone_matches: Callable[[str], list[tuple[int, int]]]
    dependency_versions: dict[str, str]


_import_module = importlib.import_module


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def missing_redaction_dependencies() -> tuple[str, ...]:
    return tuple(name for name in REDACTION_EXTRA_MODULES if not _module_available(name))


def models_root() -> Path:
    configured = os.environ.get(_MODELS_DIR_ENV)
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".kensa" / "models"


def readiness_path(root: Path | str | None = None) -> Path:
    base = Path(root) if root is not None else Path.cwd()
    return base / REDACTION_READINESS_PATH


def _normalize_environment(
    environment: EvidenceEnvironment | str | None,
) -> EvidenceEnvironment:
    if environment is None:
        return EvidenceEnvironment.LOCAL
    try:
        return EvidenceEnvironment(str(environment))
    except ValueError as exc:
        raise RedactionError(
            f"unknown evidence environment: {environment}. Use local, staging, or production."
        ) from exc


def _package_version(package: str) -> str:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


# --- deterministic recognizers -------------------------------------------------------


def _match_span(
    match: Match[str],
    entity: EntityType,
    score: float,
) -> RedactionSpan:
    return RedactionSpan(
        start=match.start(),
        end=match.end(),
        entity_type=str(entity),
        score=score,
        detector=DetectorKind.KENSA_DETERMINISTIC,
    )


def _detect_emails(text: str) -> list[RedactionSpan]:
    return [
        _match_span(match, EntityType.EMAIL_ADDRESS, _SCORE_PATTERN)
        for match in _EMAIL_RE.finditer(text)
    ]


def _detect_urls(text: str) -> list[RedactionSpan]:
    return [_match_span(match, EntityType.URL, _SCORE_PATTERN) for match in _URL_RE.finditer(text)]


def _luhn_valid(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _detect_credit_cards(text: str) -> list[RedactionSpan]:
    spans: list[RedactionSpan] = []
    for match in _CARD_RE.finditer(text):
        digits = re.sub(r"[ -]", "", match.group())
        if (
            13 <= len(digits) <= 19
            and digits[0] in "23456"
            and len(set(digits)) > 1
            and _luhn_valid(digits)
        ):
            spans.append(_match_span(match, EntityType.CREDIT_CARD, _SCORE_PARSER))
    return spans


def _ssn_groups_valid(area: str, group: str, serial: str) -> bool:
    if area in {"000", "666"} or area.startswith("9"):
        return False
    return group != "00" and serial != "0000"


def _detect_us_ssns(text: str) -> list[RedactionSpan]:
    spans = [
        _match_span(match, EntityType.US_SSN, _SCORE_PATTERN)
        for match in _SSN_DELIMITED_RE.finditer(text)
        if _ssn_groups_valid(*match.groups())
    ]
    if _SSN_CONTEXT_RE.search(text):
        spans.extend(
            _match_span(match, EntityType.US_SSN, _SCORE_CONTEXT)
            for match in _SSN_PLAIN_RE.finditer(text)
            if _ssn_groups_valid(*match.groups())
        )
    return spans


def _detect_dob_dates(text: str) -> list[RedactionSpan]:
    if not _DOB_CONTEXT_RE.search(text):
        return []
    return [
        _match_span(match, EntityType.DATE_TIME, _SCORE_CONTEXT)
        for match in _DATE_RE.finditer(text)
    ]


def _detect_ip_addresses(text: str) -> list[RedactionSpan]:
    spans: list[RedactionSpan] = []
    for pattern in (_IPV4_RE, _IPV6_RE):
        for match in pattern.finditer(text):
            candidate = match.group()
            try:
                ipaddress.ip_address(candidate)
            except ValueError:
                continue
            spans.append(_match_span(match, EntityType.IP_ADDRESS, _SCORE_PARSER))
    return spans


def _detect_mac_addresses(text: str) -> list[RedactionSpan]:
    return [
        _match_span(match, EntityType.MAC_ADDRESS, _SCORE_PATTERN)
        for match in _MAC_RE.finditer(text)
    ]


def _iban_valid(candidate: str) -> bool:
    rearranged = candidate[4:] + candidate[:4]
    digits = "".join(str(int(char, 36)) for char in rearranged)
    return int(digits) % 97 == 1


def _detect_ibans(text: str) -> list[RedactionSpan]:
    return [
        _match_span(match, EntityType.IBAN_CODE, _SCORE_PARSER)
        for match in _IBAN_RE.finditer(text)
        if _iban_valid(match.group())
    ]


def _base58check_valid(candidate: str) -> bool:
    number = 0
    for char in candidate:
        index = _BASE58_ALPHABET.find(char)
        if index < 0:
            return False
        number = number * 58 + index
    body = number.to_bytes((number.bit_length() + 7) // 8, "big")
    zeros = len(candidate) - len(candidate.lstrip("1"))
    payload = b"\x00" * zeros + body
    if len(payload) != 25:
        return False
    checksum = hashlib.sha256(hashlib.sha256(payload[:-4]).digest()).digest()[:4]
    return checksum == payload[-4:]


_KECCAK_ROUND_CONSTANTS = (
    0x0000000000000001,
    0x0000000000008082,
    0x800000000000808A,
    0x8000000080008000,
    0x000000000000808B,
    0x0000000080000001,
    0x8000000080008081,
    0x8000000000008009,
    0x000000000000008A,
    0x0000000000000088,
    0x0000000080008009,
    0x000000008000000A,
    0x000000008000808B,
    0x800000000000008B,
    0x8000000000008089,
    0x8000000000008003,
    0x8000000000008002,
    0x8000000000000080,
    0x000000000000800A,
    0x800000008000000A,
    0x8000000080008081,
    0x8000000000008080,
    0x0000000080000001,
    0x8000000080008008,
)
_LANE_MASK = (1 << 64) - 1


def _rotl64(value: int, shift: int) -> int:
    shift %= 64
    if shift == 0:
        return value & _LANE_MASK
    return ((value << shift) | (value >> (64 - shift))) & _LANE_MASK


def _keccak_permutation(lanes: list[list[int]]) -> list[list[int]]:
    for round_constant in _KECCAK_ROUND_CONSTANTS:
        parity = [
            lanes[x][0] ^ lanes[x][1] ^ lanes[x][2] ^ lanes[x][3] ^ lanes[x][4] for x in range(5)
        ]
        theta = [parity[(x + 4) % 5] ^ _rotl64(parity[(x + 1) % 5], 1) for x in range(5)]
        lanes = [[lanes[x][y] ^ theta[x] for y in range(5)] for x in range(5)]
        x, y = 1, 0
        current = lanes[x][y]
        for step in range(24):
            x, y = y, (2 * x + 3 * y) % 5
            current, lanes[x][y] = lanes[x][y], _rotl64(current, (step + 1) * (step + 2) // 2)
        for row_index in range(5):
            row = [lanes[x][row_index] for x in range(5)]
            for column in range(5):
                lanes[column][row_index] = row[column] ^ (
                    (~row[(column + 1) % 5]) & row[(column + 2) % 5]
                )
        lanes[0][0] ^= round_constant
    return lanes


def _keccak256(data: bytes) -> bytes:
    rate = 136
    lanes = [[0] * 5 for _ in range(5)]
    padded = bytearray(data)
    padded.append(0x01)
    while len(padded) % rate:
        padded.append(0x00)
    padded[-1] ^= 0x80
    for block in range(0, len(padded), rate):
        for lane in range(rate // 8):
            offset = block + 8 * lane
            lanes[lane % 5][lane // 5] ^= int.from_bytes(padded[offset : offset + 8], "little")
        lanes = _keccak_permutation(lanes)
    digest = bytearray()
    for lane in range(4):
        digest += lanes[lane % 5][lane // 5].to_bytes(8, "little")
    return bytes(digest)


def _eip55_valid(address: str) -> bool:
    body = address[2:]
    digest = _keccak256(body.lower().encode()).hex()
    for char, nibble in zip(body, digest, strict=False):
        if char.isalpha() and (int(nibble, 16) >= 8) != char.isupper():
            return False
    return True


def _detect_crypto_addresses(text: str) -> list[RedactionSpan]:
    spans = [
        _match_span(match, EntityType.CRYPTO, _SCORE_PARSER)
        for match in _BTC_BASE58_RE.finditer(text)
        if _base58check_valid(match.group())
    ]
    spans.extend(
        _match_span(match, EntityType.CRYPTO, _SCORE_PATTERN)
        for match in _BTC_BECH32_RE.finditer(text)
    )
    for match in _ETH_RE.finditer(text):
        body = match.group()[2:]
        if body.islower() or body.isupper() or body.isdigit() or _eip55_valid(match.group()):
            spans.append(_match_span(match, EntityType.CRYPTO, _SCORE_PARSER))
    return spans


def _detect_jwts(text: str) -> list[RedactionSpan]:
    return [
        _match_span(match, EntityType.SECRET, _SCORE_PATTERN) for match in _JWT_RE.finditer(text)
    ]


def _detect_auth_headers(text: str) -> list[RedactionSpan]:
    return [
        _match_span(match, EntityType.SECRET, _SCORE_PATTERN)
        for match in _AUTH_HEADER_RE.finditer(text)
    ]


_DETERMINISTIC_RECOGNIZERS: tuple[tuple[str, Callable[[str], list[RedactionSpan]]], ...] = (
    ("email", _detect_emails),
    ("url", _detect_urls),
    ("credit-card", _detect_credit_cards),
    ("us-ssn", _detect_us_ssns),
    ("dob-date-context", _detect_dob_dates),
    ("ip-address", _detect_ip_addresses),
    ("mac-address", _detect_mac_addresses),
    ("iban", _detect_ibans),
    ("crypto-address", _detect_crypto_addresses),
    ("jwt", _detect_jwts),
    ("auth-header", _detect_auth_headers),
)
_DETERMINISTIC_RECOGNIZER_NAMES = (
    *(name for name, _detector in _DETERMINISTIC_RECOGNIZERS),
    "phone-number",
)

_RULESET = {
    "chunk_chars": _CHUNK_CHARS,
    "chunk_overlap": _CHUNK_OVERLAP,
    "detect_secrets_plugins": [dict(plugin) for plugin in _DETECT_SECRETS_PLUGINS],
    "deterministic_recognizers": list(_DETERMINISTIC_RECOGNIZER_NAMES),
    "freeform_containers": sorted(_FREEFORM_CONTAINERS),
    "language": LANGUAGE,
    "phone_regions": list(_PHONE_REGIONS),
    "presidio_recognizers": list(_PRESIDIO_RECOGNIZER_NAMES),
    "presidio_score_threshold": _PRESIDIO_SCORE_THRESHOLD,
    "provenance_paths": [list(path) for path in _PROVENANCE_PATHS],
    "pseudonymization": PSEUDONYMIZATION_SCHEME,
    "secret_key_pattern": _SECRET_KEY.pattern,
    "spacy_entity_mapping": _SPACY_ENTITY_MAPPING,
    "spacy_labels_to_ignore": list(_SPACY_LABELS_TO_IGNORE),
    "spacy_models": [spec.label for spec in _PINNED_SPACY_MODELS],
    "thresholds": {
        "context": _SCORE_CONTEXT,
        "parser": _SCORE_PARSER,
        "pattern": _SCORE_PATTERN,
        "secret": _SCORE_SECRET,
    },
    "timing_field_allowlist": sorted(_TIMING_FIELD_ALLOWLIST),
    "version": REDACTOR_MANIFEST_VERSION,
}
RULESET_HASH = hashlib.sha256(json.dumps(_RULESET, sort_keys=True).encode()).hexdigest()


# --- span merge and rendering --------------------------------------------------------


def _merge_spans(spans: list[RedactionSpan]) -> list[_MergedSpan]:
    """Union-merge overlapping spans; label each merged span with its best candidate.

    Never drops a redaction span: overlapping detections extend the merged span, the
    highest (score, detector-priority) candidate names it, and unresolved ties between
    different entity types render as [REDACTED].
    """

    if not spans:
        return []
    ordered = sorted(spans, key=lambda span: (span.start, span.end))
    groups: list[list[RedactionSpan]] = [[ordered[0]]]
    end = ordered[0].end
    for span in ordered[1:]:
        if span.start < end:
            groups[-1].append(span)
            end = max(end, span.end)
        else:
            groups.append([span])
            end = span.end
    merged: list[_MergedSpan] = []
    for group in groups:
        best = max(group, key=lambda span: (span.score, _DETECTOR_PRIORITY[span.detector]))
        best_key = (best.score, _DETECTOR_PRIORITY[best.detector])
        winners = {
            span.entity_type
            for span in group
            if (span.score, _DETECTOR_PRIORITY[span.detector]) == best_key
        }
        merged.append(
            _MergedSpan(
                start=min(span.start for span in group),
                end=max(span.end for span in group),
                entity_type=best.entity_type,
                conflicted=len(winners) > 1,
            )
        )
    return merged


def _chunks(text: str) -> Iterator[tuple[int, str]]:
    if len(text) <= _CHUNK_CHARS:
        yield 0, text
        return
    step = _CHUNK_CHARS - _CHUNK_OVERLAP
    for offset in range(0, len(text), step):
        yield offset, text[offset : offset + _CHUNK_CHARS]
        if offset + _CHUNK_CHARS >= len(text):
            return


# --- engine loading ------------------------------------------------------------------


def _load_engine(readiness: RedactionReadiness) -> _EngineHandle:
    spec = _pinned_model_spec(readiness)
    model_path = models_root() / spec.label
    try:
        presidio = cast(Any, _import_module("presidio_analyzer"))
        recognizers = cast(Any, _import_module("presidio_analyzer.predefined_recognizers"))
        nlp_engine_module = cast(Any, _import_module("presidio_analyzer.nlp_engine"))
        settings_module = cast(Any, _import_module("detect_secrets.settings"))
        plugins_module = cast(Any, _import_module("detect_secrets.core.plugins.initialize"))
        phonenumbers = cast(Any, _import_module("phonenumbers"))
    except ImportError as exc:
        raise RedactionNotReadyError(
            f"trace redaction dependencies unavailable: {exc}. "
            "Install kensa[redaction] and re-run kensa init."
        ) from exc
    try:
        configuration = {
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": LANGUAGE, "model_name": str(model_path)}],
            "ner_model_configuration": {
                "model_to_presidio_entity_mapping": dict(_SPACY_ENTITY_MAPPING),
                "labels_to_ignore": list(_SPACY_LABELS_TO_IGNORE),
                "low_confidence_score_multiplier": 0.4,
                "low_score_entity_names": [],
            },
        }
        provider = nlp_engine_module.NlpEngineProvider(nlp_configuration=configuration)
        nlp_engine = provider.create_engine()
        registry = presidio.RecognizerRegistry(supported_languages=[LANGUAGE])
        for name in _PRESIDIO_RECOGNIZER_NAMES:
            recognizer_type = getattr(recognizers, name)
            registry.add_recognizer(recognizer_type(supported_language=LANGUAGE))
        analyzer = presidio.AnalyzerEngine(
            nlp_engine=nlp_engine,
            registry=registry,
            supported_languages=[LANGUAGE],
            default_score_threshold=_PRESIDIO_SCORE_THRESHOLD,
        )
        entities = tuple(
            sorted(str(entity) for entity in analyzer.get_supported_entities(language=LANGUAGE))
        )
    except Exception as exc:
        raise RedactionNotReadyError(
            f"Presidio analyzer unavailable: {exc}. Re-run kensa init."
        ) from exc
    if not entities:
        raise RedactionNotReadyError(
            "Presidio analyzer unavailable: no supported English entities. Re-run kensa init."
        )

    try:
        with settings_module.transient_settings(
            {"plugins_used": [dict(plugin) for plugin in _DETECT_SECRETS_PLUGINS]}
        ):
            secret_plugins = [
                plugins_module.from_plugin_classname(str(plugin["name"]))
                for plugin in _DETECT_SECRETS_PLUGINS
            ]
    except Exception as exc:
        raise RedactionNotReadyError(
            f"detect-secrets plugins unavailable: {exc}. Re-run kensa init."
        ) from exc

    def detect_secret_spans(value: str) -> list[tuple[int, int]]:
        line = f"value = {json.dumps(value)}"
        spans: list[tuple[int, int]] = []
        for plugin in secret_plugins:
            for hit in plugin.analyze_line(filename="kensa-trace-import", line=line):
                secret_value = getattr(hit, "secret_value", None)
                located = _locate_secret(value, secret_value)
                if located is None:
                    # A hit that cannot be located redacts the whole value.
                    return [(0, len(value))]
                spans.extend(located)
        return spans

    matcher_type = phonenumbers.PhoneNumberMatcher

    def phone_matches(value: str) -> list[tuple[int, int]]:
        seen: set[tuple[int, int]] = set()
        for region in _PHONE_REGIONS:
            for match in matcher_type(value, region):
                seen.add((int(match.start), int(match.end)))
        return sorted(seen)

    return _EngineHandle(
        analyzer=analyzer,
        entities=entities,
        detect_secret_spans=detect_secret_spans,
        phone_matches=phone_matches,
        dependency_versions={
            "detect-secrets": _package_version("detect-secrets"),
            "phonenumbers": _package_version("phonenumbers"),
            "presidio-analyzer": _package_version("presidio-analyzer"),
            "spacy": _package_version("spacy"),
        },
    )


# --- run-level redactor --------------------------------------------------------------


class Redactor:
    """Run-level redaction engine: one instance per import run, one artifact.

    Caches the analyzer, holds the in-memory value-to-alias map for stable
    instance-counter pseudonymization, and accumulates artifact-level manifest
    stats. The alias map is discarded with the instance and never persisted.
    """

    def __init__(
        self,
        *,
        environment: EvidenceEnvironment | str | None = None,
        root: Path | str | None = None,
    ) -> None:
        self._environment = _normalize_environment(environment)
        self._readiness = assert_redaction_ready(environment=self._environment, root=root)
        self._engine = _load_engine(self._readiness)
        self._alias_map: dict[tuple[str, str], str] = {}
        self._instance_counts: Counter[str] = Counter()
        self._span_count = 0
        self._changed_value_count = 0
        self._secret_keys_redacted = False
        self._trace_count = 0

    @property
    def readiness(self) -> RedactionReadiness:
        return self._readiness

    @property
    def environment(self) -> EvidenceEnvironment:
        return self._environment

    def redact_trace_view(self, trace: dict[str, Any]) -> RedactionResult:
        spans_before = self._span_count
        changed_before = self._changed_value_count
        redacted = cast(dict[str, Any], self.redact_value(trace))
        self._trace_count += 1
        return RedactionResult(
            trace=redacted,
            redacted_span_count=self._span_count - spans_before,
            changed_value_count=self._changed_value_count - changed_before,
            secret_keys_redacted=self._secret_keys_redacted,
        )

    def redact_value(self, value: Any, *, path: tuple[str, ...] = ()) -> Any:
        if isinstance(value, dict):
            return self._redact_dict(value, path)
        if isinstance(value, list):
            return [self.redact_value(item, path=(*path, "[]")) for item in value]
        if isinstance(value, str):
            return self._redact_string_leaf(value, path)
        if isinstance(value, bool) or value is None:
            return value
        if isinstance(value, int | float):
            return self._redact_numeric_leaf(value, path)
        return value

    def manifest(self) -> dict[str, Any]:
        return {
            "version": REDACTOR_MANIFEST_VERSION,
            "mandatory": True,
            "language": LANGUAGE,
            "value_redaction_applied": True,
            "redaction_available": True,
            "redacted_span_count": self._span_count,
            "changed_value_count": self._changed_value_count,
            "secret_keys_redacted": self._secret_keys_redacted,
            "trace_count": self._trace_count,
            "ruleset_hash": RULESET_HASH,
            "pseudonymization": PSEUDONYMIZATION_SCHEME,
            "entity_instance_counts": dict(sorted(self._instance_counts.items())),
            "detectors": {
                "kensa_deterministic": {
                    "recognizers": list(_DETERMINISTIC_RECOGNIZER_NAMES),
                    "phone_regions": list(_PHONE_REGIONS),
                },
                "detect_secrets": {
                    "version": self._engine.dependency_versions["detect-secrets"],
                    "plugins": [dict(plugin) for plugin in _DETECT_SECRETS_PLUGINS],
                },
                "presidio": {
                    "version": self._engine.dependency_versions["presidio-analyzer"],
                    "recognizers": list(_PRESIDIO_RECOGNIZER_NAMES),
                    "entities": list(self._engine.entities),
                },
                "spacy_ner": {
                    "version": self._engine.dependency_versions["spacy"],
                    "entity_mapping": dict(_SPACY_ENTITY_MAPPING),
                    "labels_to_ignore": list(_SPACY_LABELS_TO_IGNORE),
                },
            },
            "model": {
                "name": self._readiness.model,
                "version": self._readiness.model_version,
                "tier": self._readiness.model_tier,
                "fallback_used": self._readiness.fallback_used,
                "checksum_verified": self._readiness.checksum_verified,
            },
            "evidence_environment": str(self._environment),
        }

    def _redact_dict(self, value: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any]:
        redacted: dict[str, Any] = {}
        rewritable = any(part in _FREEFORM_CONTAINERS for part in path)
        for key, item in value.items():
            text_key = str(key)
            rendered_key = self._render_key(text_key, rewritable=rewritable)
            if _SECRET_KEY.search(text_key):
                self._secret_keys_redacted = True
                self._span_count += 1
                self._changed_value_count += 1
                redacted[rendered_key] = self._secret_value_alias(item)
                continue
            redacted[rendered_key] = self.redact_value(item, path=(*path, text_key))
        return redacted

    def _render_key(self, key: str, *, rewritable: bool) -> str:
        # Keys are identifiers, not prose: only the deterministic recognizers run
        # on key text, so NER never rewrites schema-ish keys like `span_id`.
        if not rewritable or not key.strip():
            return key
        spans: list[RedactionSpan] = []
        for _name, detector in _DETERMINISTIC_RECOGNIZERS:
            spans.extend(detector(key))
        merged = _merge_spans(spans)
        if not merged:
            return key
        self._span_count += len(merged)
        self._changed_value_count += 1
        return self._render_merged(key, merged)

    def _secret_value_alias(self, value: Any) -> str:
        canonical = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
        return self._alias(str(EntityType.SECRET), canonical)

    def _redact_string_leaf(self, value: str, path: tuple[str, ...]) -> str:
        if self._is_provenance_path(path):
            return value
        redacted, span_count = self._redact_text(value, timing_exempt=self._timing_exempt(path))
        self._span_count += span_count
        if redacted != value:
            self._changed_value_count += 1
        return redacted

    def _redact_numeric_leaf(self, value: int | float, path: tuple[str, ...]) -> Any:
        rendered = str(value)
        redacted, span_count = self._redact_text(rendered, timing_exempt=self._timing_exempt(path))
        if span_count == 0:
            return value
        self._span_count += span_count
        self._changed_value_count += 1
        return redacted

    @staticmethod
    def _timing_exempt(path: tuple[str, ...]) -> bool:
        return bool(path) and path[-1] in _TIMING_FIELD_ALLOWLIST

    @staticmethod
    def _is_provenance_path(path: tuple[str, ...]) -> bool:
        return any(path[: len(prefix)] == prefix for prefix in _PROVENANCE_PATHS)

    def _redact_text(self, text: str, *, timing_exempt: bool) -> tuple[str, int]:
        if not text.strip():
            return text, 0
        try:
            spans: list[RedactionSpan] = []
            for _name, detector in _DETERMINISTIC_RECOGNIZERS:
                spans.extend(detector(text))
            spans.extend(
                RedactionSpan(
                    start=start,
                    end=end,
                    entity_type=str(EntityType.PHONE_NUMBER),
                    score=_SCORE_PARSER,
                    detector=DetectorKind.KENSA_DETERMINISTIC,
                )
                for start, end in self._engine.phone_matches(text)
            )
            spans.extend(
                RedactionSpan(
                    start=start,
                    end=end,
                    entity_type=str(EntityType.SECRET),
                    score=_SCORE_SECRET,
                    detector=DetectorKind.DETECT_SECRETS,
                )
                for start, end in self._engine.detect_secret_spans(text)
            )
            spans.extend(self._presidio_spans(text))
        except RedactionError:
            raise
        except Exception as exc:
            # Fail closed: never return input text unredacted on analyzer errors.
            raise RedactionError(f"value redaction failed; aborting import: {exc}") from exc
        if timing_exempt:
            spans = [span for span in spans if span.entity_type != str(EntityType.DATE_TIME)]
        merged = _merge_spans(spans)
        return self._render_merged(text, merged), len(merged)

    def _render_merged(self, text: str, merged: list[_MergedSpan]) -> str:
        # Aliases are assigned left-to-right (first-seen order) before splicing.
        replacements = [(span.start, span.end, self._render_span(span, text)) for span in merged]
        redacted = text
        for start, end, replacement in reversed(replacements):
            redacted = redacted[:start] + replacement + redacted[end:]
        return redacted

    def _presidio_spans(self, text: str) -> list[RedactionSpan]:
        spans: list[RedactionSpan] = []
        for offset, piece in _chunks(text):
            spans.extend(
                RedactionSpan(
                    start=offset + int(result.start),
                    end=offset + int(result.end),
                    entity_type=str(result.entity_type),
                    score=float(result.score),
                    detector=_presidio_detector(result),
                )
                for result in self._engine.analyzer.analyze(text=piece, language=LANGUAGE)
            )
        return spans

    def _render_span(self, span: _MergedSpan, text: str) -> str:
        if span.conflicted or span.entity_type not in _KNOWN_ENTITY_LABELS:
            return REDACTED_PLACEHOLDER
        return self._alias(span.entity_type, text[span.start : span.end])

    def _alias(self, entity_type: str, original: str) -> str:
        key = (entity_type, original)
        existing = self._alias_map.get(key)
        if existing is not None:
            return existing
        self._instance_counts[entity_type] += 1
        alias = f"[{entity_type}_{self._instance_counts[entity_type]}]"
        self._alias_map[key] = alias
        return alias


def _locate_secret(value: str, secret_value: Any) -> list[tuple[int, int]] | None:
    """Locate a detect-secrets hit in the original value, expanded to whole tokens.

    Returns None when the hit cannot be located, in which case the caller redacts
    the whole value (fail-safe). Expanding to the surrounding non-whitespace token
    keeps recall when a plugin reports only a fragment of a credential.
    """

    if not isinstance(secret_value, str) or not secret_value:
        return None
    start = value.find(secret_value)
    if start < 0:
        return None
    spans: list[tuple[int, int]] = []
    while start >= 0:
        end = start + len(secret_value)
        while start > 0 and not value[start - 1].isspace():
            start -= 1
        while end < len(value) and not value[end].isspace():
            end += 1
        spans.append((start, end))
        start = value.find(secret_value, end)
    return spans


def _presidio_detector(result: Any) -> DetectorKind:
    metadata = getattr(result, "recognition_metadata", None)
    if isinstance(metadata, dict) and metadata.get("recognizer_name") == "SpacyRecognizer":
        return DetectorKind.SPACY_NER
    if metadata is None and str(result.entity_type) in _NER_ENTITY_LABELS:
        return DetectorKind.SPACY_NER
    return DetectorKind.PRESIDIO_BUILTIN


def redact_value(redactor: Redactor, value: Any) -> Any:
    return redactor.redact_value(value)


def redact_trace_view(redactor: Redactor, trace: dict[str, Any]) -> RedactionResult:
    return redactor.redact_trace_view(trace)


# --- manifest safety gates -----------------------------------------------------------


def _manifest_problem(
    manifest: Any,
    environment: EvidenceEnvironment,
) -> str | None:
    if not isinstance(manifest, dict) or not manifest:
        return "trace artifact has no redaction manifest"
    if manifest.get("raw_source") is True:
        return "trace artifact is raw source telemetry and is never exposable as evidence"
    if manifest.get("version") != REDACTOR_MANIFEST_VERSION:
        return (
            "trace artifact was not redacted with the mandatory "
            f"{REDACTOR_MANIFEST_VERSION} redactor"
        )
    if manifest.get("mandatory") is not True:
        return "trace artifact redaction manifest is not marked mandatory"
    if manifest.get("value_redaction_applied") is not True:
        return "trace artifact was written without mandatory value redaction"
    if manifest.get("redaction_available") is not True:
        return "trace artifact was written while redaction was unavailable"
    if not manifest.get("pseudonymization"):
        return "trace artifact redaction manifest records no pseudonymization scheme"
    model = manifest.get("model")
    if not isinstance(model, dict):
        return "trace artifact redaction manifest has no model metadata"
    tier = model.get("tier")
    if (
        not model.get("name")
        or not model.get("version")
        or tier not in {spec.tier for spec in _PINNED_SPACY_MODELS}
    ):
        return "trace artifact redaction manifest records corrupt model metadata"
    if model.get("checksum_verified") is not True:
        return "trace artifact redaction manifest records an unverified model"
    if environment is EvidenceEnvironment.PRODUCTION and tier == FALLBACK_SPACY_MODEL.tier:
        return (
            "production trace workflows are blocked on the "
            f"{FALLBACK_SPACY_MODEL.label} fallback model"
        )
    return None


def safe_manifest(
    manifest: Any,
    *,
    environment: EvidenceEnvironment | str | None,
) -> bool:
    """Pure gate over a redaction manifest dict; performs no settings or file I/O."""

    return _manifest_problem(manifest, _normalize_environment(environment)) is None


def assert_safe_manifest(
    manifest: Any,
    *,
    environment: EvidenceEnvironment | str | None,
) -> None:
    problem = _manifest_problem(manifest, _normalize_environment(environment))
    if problem is not None:
        raise RedactionGateError(
            f"Trace payload exposure blocked: {problem}. "
            "Re-import traces with kensa import after mandatory redaction is ready."
        )


# --- readiness -----------------------------------------------------------------------


def read_redaction_readiness(root: Path | str | None = None) -> RedactionReadiness | None:
    """Read `.kensa/redaction.json`. Returns None when missing; raises when invalid."""

    path = readiness_path(root)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RedactionNotReadyError(
            f"redaction readiness file is unreadable: {path}. Re-run kensa init."
        ) from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != REDACTION_READINESS_SCHEMA_VERSION
    ):
        raise RedactionNotReadyError(
            f"redaction readiness file is invalid: {path}. Re-run kensa init."
        )
    return RedactionReadiness(
        redaction_available=bool(payload.get("redaction_available")),
        language=str(payload.get("language") or ""),
        model=str(payload.get("model") or ""),
        model_version=str(payload.get("model_version") or ""),
        model_tier=str(payload.get("model_tier") or ""),
        fallback_used=bool(payload.get("fallback_used")),
        checksum_verified=bool(payload.get("checksum_verified")),
        created_at=str(payload.get("created_at") or ""),
    )


def _pinned_model_spec(readiness: RedactionReadiness) -> SpacyModelSpec:
    for spec in _PINNED_SPACY_MODELS:
        if (
            readiness.model == spec.name
            and readiness.model_version == spec.version
            and readiness.model_tier == spec.tier
        ):
            return spec
    raise RedactionNotReadyError(
        "redaction readiness names an unpinned spaCy model "
        f"({readiness.model}-{readiness.model_version}). Re-run kensa init."
    )


def assert_redaction_ready(
    *,
    environment: EvidenceEnvironment | str | None = None,
    root: Path | str | None = None,
) -> RedactionReadiness:
    """Fail-closed readiness check. Never downloads models; that is kensa init's job."""

    normalized = _normalize_environment(environment)
    missing = missing_redaction_dependencies()
    if missing:
        raise RedactionNotReadyError(
            "trace redaction dependencies are missing: "
            + ", ".join(missing)
            + ". Install kensa[redaction] and re-run kensa init."
        )
    readiness = read_redaction_readiness(root)
    if readiness is None:
        raise RedactionNotReadyError(
            "mandatory trace redaction is not bootstrapped: "
            f"{readiness_path(root)} is missing. Run kensa init."
        )
    if not readiness.redaction_available or readiness.language != LANGUAGE:
        raise RedactionNotReadyError(
            "redaction readiness reports redaction unavailable. Re-run kensa init."
        )
    if not readiness.checksum_verified:
        raise RedactionNotReadyError(
            "the cached spaCy model was never checksum-verified. Re-run kensa init."
        )
    spec = _pinned_model_spec(readiness)
    _validate_model_dir(models_root() / spec.label, spec)
    if normalized is EvidenceEnvironment.PRODUCTION and spec.tier == FALLBACK_SPACY_MODEL.tier:
        raise RedactionGateError(
            f"production trace workflows are blocked on the {spec.label} fallback model. "
            f"Re-run kensa init to prepare {DEFAULT_SPACY_MODEL.label}."
        )
    return readiness


# --- model bootstrap (kensa init only) -----------------------------------------------

_urlopen = urllib.request.urlopen


def _validate_model_dir(path: Path, spec: SpacyModelSpec) -> None:
    meta_path = path / "meta.json"
    try:
        meta = json.loads(meta_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RedactionNotReadyError(
            f"cached spaCy model is missing or corrupt at {path}. Re-run kensa init."
        ) from exc
    full_name = f"{meta.get('lang')}_{meta.get('name')}"
    if full_name != spec.name or meta.get("version") != spec.version:
        raise RedactionNotReadyError(
            f"cached spaCy model at {path} does not match {spec.label}. Re-run kensa init."
        )
    requirement = str(meta.get("spacy_version") or "")
    installed = _package_version("spacy")
    if not _spacy_version_supported(installed, requirement):
        raise RedactionNotReadyError(
            f"cached spaCy model {spec.label} requires spacy{requirement}, "
            f"but spacy {installed} is installed. Re-run kensa init."
        )


def _spacy_version_supported(installed: str, requirement: str) -> bool:
    installed_parts = _version_tuple(installed)
    if not installed_parts:
        return False
    for raw_clause in requirement.split(","):
        clause = raw_clause.strip()
        if not clause:
            continue
        operator = re.match(r"(>=|<=|==|<|>)", clause)
        if operator is None:
            continue
        bound = _version_tuple(clause[len(operator.group()) :])
        satisfied = {
            ">=": installed_parts >= bound,
            "<=": installed_parts <= bound,
            "==": installed_parts == bound,
            "<": installed_parts < bound,
            ">": installed_parts > bound,
        }[operator.group()]
        if not satisfied:
            return False
    return True


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version)[:3])


def _download_model_wheel(spec: SpacyModelSpec, destination: Path) -> None:
    if not spec.url.startswith("https://"):
        raise RedactionBootstrapError(f"model download URL must use HTTPS: {spec.url}")
    digest = hashlib.sha256()
    try:
        with _urlopen(spec.url) as response, destination.open("wb") as handle:
            while chunk := response.read(1 << 16):
                digest.update(chunk)
                handle.write(chunk)
    except OSError as exc:
        raise RedactionBootstrapError(f"could not download {spec.label}: {exc}") from exc
    if digest.hexdigest() != spec.sha256:
        raise RedactionBootstrapError(
            f"checksum mismatch for {spec.label}: expected {spec.sha256}, got {digest.hexdigest()}"
        )


def _extract_model_wheel(wheel: Path, spec: SpacyModelSpec, models_dir: Path) -> Path:
    target = models_dir / spec.label
    with tempfile.TemporaryDirectory(dir=models_dir) as temp_dir:
        try:
            with zipfile.ZipFile(wheel) as archive:
                archive.extractall(temp_dir)
        except (OSError, zipfile.BadZipFile) as exc:
            raise RedactionBootstrapError(f"could not extract {spec.label} wheel: {exc}") from exc
        inner = Path(temp_dir) / spec.name / spec.label
        if not inner.is_dir():
            raise RedactionBootstrapError(
                f"{spec.label} wheel does not contain the expected model directory"
            )
        try:
            _validate_model_dir(inner, spec)
        except RedactionNotReadyError as exc:
            raise RedactionBootstrapError(str(exc)) from exc
        if target.exists():
            shutil.rmtree(target)
        os.replace(inner, target)
    return target


def _prepare_model(spec: SpacyModelSpec) -> Path:
    models_dir = models_root()
    models_dir.mkdir(parents=True, exist_ok=True)
    target = models_dir / spec.label
    if target.is_dir():
        try:
            _validate_model_dir(target, spec)
        except RedactionNotReadyError:
            shutil.rmtree(target)
        else:
            return target
    wheel_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=models_dir,
            prefix=f".{spec.label}.",
            suffix=".whl",
            delete=False,
        ) as wheel_file:
            wheel_path = Path(wheel_file.name)
        _download_model_wheel(spec, wheel_path)
        return _extract_model_wheel(wheel_path, spec, models_dir)
    finally:
        if wheel_path is not None:
            wheel_path.unlink(missing_ok=True)


def ensure_redaction_ready(root: Path | str | None = None) -> RedactionReadiness:
    """Bootstrap mandatory redaction during kensa init and write `.kensa/redaction.json`.

    Prepares the pinned default model, falling back to the pinned small model in a
    degraded readiness state. When neither model can be prepared, no readiness file
    is written and the error propagates.
    """

    missing = missing_redaction_dependencies()
    if missing:
        raise RedactionNotReadyError(
            "trace redaction dependencies are missing: "
            + ", ".join(missing)
            + ". Install kensa[redaction] first."
        )
    fallback_used = False
    spec = DEFAULT_SPACY_MODEL
    try:
        _prepare_model(DEFAULT_SPACY_MODEL)
    except RedactionError as default_error:
        spec = FALLBACK_SPACY_MODEL
        fallback_used = True
        try:
            _prepare_model(FALLBACK_SPACY_MODEL)
        except RedactionError as fallback_error:
            raise RedactionBootstrapError(
                f"could not prepare any redaction model. {DEFAULT_SPACY_MODEL.label}: "
                f"{default_error} {FALLBACK_SPACY_MODEL.label}: {fallback_error}"
            ) from fallback_error
    readiness = RedactionReadiness(
        redaction_available=True,
        language=LANGUAGE,
        model=spec.name,
        model_version=spec.version,
        model_tier=spec.tier,
        fallback_used=fallback_used,
        checksum_verified=True,
        created_at=datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    )
    path = readiness_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(readiness.to_dict(), indent=2, sort_keys=True) + "\n")
    return readiness


__all__ = [
    "DEFAULT_SPACY_MODEL",
    "FALLBACK_SPACY_MODEL",
    "FUTURE_ENTITIES",
    "LANGUAGE",
    "PSEUDONYMIZATION_SCHEME",
    "REDACTED_PLACEHOLDER",
    "REDACTION_EXTRA_MODULES",
    "REDACTION_READINESS_PATH",
    "REDACTION_READINESS_SCHEMA_VERSION",
    "REDACTOR_MANIFEST_VERSION",
    "RULESET_HASH",
    "DetectorKind",
    "EntityType",
    "EvidenceEnvironment",
    "RedactionBootstrapError",
    "RedactionError",
    "RedactionGateError",
    "RedactionNotReadyError",
    "RedactionReadiness",
    "RedactionResult",
    "RedactionSpan",
    "Redactor",
    "SpacyModelSpec",
    "assert_redaction_ready",
    "assert_safe_manifest",
    "ensure_redaction_ready",
    "missing_redaction_dependencies",
    "models_root",
    "read_redaction_readiness",
    "readiness_path",
    "redact_trace_view",
    "redact_value",
    "safe_manifest",
]
