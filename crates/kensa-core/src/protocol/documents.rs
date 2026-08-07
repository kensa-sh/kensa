use std::collections::BTreeMap;
use std::num::NonZeroU32;

use schemars::JsonSchema;
use serde::{Deserialize, Deserializer, Serialize, Serializer, de, ser::SerializeStruct};
use serde_json::Value;

use super::primitives::{
    CaseId, CheckResultId, EvalRunId, InvocationId, NonBlankString, SpanId, Timestamp, TraceId,
};

pub type JsonObject = BTreeMap<String, Value>;

macro_rules! serialize_validated {
    ($name:ident { $($field:ident),+ $(,)? }) => {
        impl Serialize for $name {
            fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
            where
                S: Serializer,
            {
                self.validate().map_err(serde::ser::Error::custom)?;
                let field_count = [$(stringify!($field)),+].len();
                let mut state = serializer.serialize_struct(stringify!($name), field_count)?;
                $(state.serialize_field(stringify!($field), &self.$field)?;)+
                state.end()
            }
        }
    };
}

fn required_option<'de, D, T>(deserializer: D) -> Result<Option<T>, D::Error>
where
    D: Deserializer<'de>,
    T: Deserialize<'de>,
{
    Option::deserialize(deserializer)
}

macro_rules! wire_enum {
    ($name:ident { $($variant:ident),+ $(,)? }) => {
        #[derive(Clone, Copy, Debug, Deserialize, Eq, JsonSchema, PartialEq, Serialize)]
        #[serde(rename_all = "snake_case")]
        pub enum $name {
            $($variant),+
        }
    };
}

wire_enum!(EvalRunStatus {
    Pending,
    Running,
    Pass,
    Fail,
    Error,
    Cancelled,
    Interrupted,
});
wire_enum!(InvocationStatus {
    Pending,
    Running,
    Pass,
    Fail,
    Error,
    Cancelled,
    Skipped,
    Interrupted,
});
wire_enum!(CheckStatus {
    Pass,
    Fail,
    Error,
    Skipped,
});
wire_enum!(SpanStatus { Unset, Ok, Error });
wire_enum!(FailureCategory {
    Agent,
    Simulator,
    Judge,
    Configuration,
    Infrastructure,
    Harness,
    Unknown,
});
wire_enum!(EffectPolicy {
    None,
    Captured,
    Sandboxed,
    Live,
});
wire_enum!(EvidenceStatus {
    Complete,
    Pending,
    Partial,
    Unavailable,
});

#[derive(Clone, Copy, Debug, Deserialize, Eq, JsonSchema, PartialEq, Serialize)]
pub enum SchemaVersion {
    #[serde(rename = "kensa.protocol.v1")]
    V1,
}

macro_rules! document_kind {
    ($name:ident, $variant:ident, $wire:literal) => {
        #[derive(Clone, Copy, Debug, Deserialize, Eq, JsonSchema, PartialEq, Serialize)]
        pub enum $name {
            #[serde(rename = $wire)]
            $variant,
        }
    };
}

document_kind!(EvalRunDocumentKind, EvalRun, "eval_run");
document_kind!(InvocationDocumentKind, Invocation, "invocation");
document_kind!(SpanDocumentKind, Span, "span");
document_kind!(CheckResultDocumentKind, CheckResult, "check_result");

#[derive(Clone, Debug, Deserialize, JsonSchema, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct CaseSnapshot {
    pub id: CaseId,
    pub input: Value,
    pub metadata: JsonObject,
}

#[derive(Clone, Debug, Deserialize, JsonSchema, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Failure {
    pub category: FailureCategory,
    pub kind: NonBlankString,
    pub message: NonBlankString,
    pub evidence: JsonObject,
}

#[derive(Clone, Debug, Deserialize, JsonSchema, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ExecutionProvenance {
    pub producer: NonBlankString,
    pub producer_version: NonBlankString,
    #[serde(deserialize_with = "required_option")]
    pub adapter: Option<NonBlankString>,
    #[serde(deserialize_with = "required_option")]
    pub adapter_version: Option<NonBlankString>,
    pub runtime: NonBlankString,
    pub runtime_version: NonBlankString,
    #[serde(deserialize_with = "required_option")]
    pub revision: Option<NonBlankString>,
    #[serde(deserialize_with = "required_option")]
    pub environment: Option<NonBlankString>,
    pub effects: EffectPolicy,
}

#[derive(Clone, Debug, JsonSchema, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct EvidenceCompleteness {
    pub status: EvidenceStatus,
    pub reason: Option<NonBlankString>,
}

impl EvidenceCompleteness {
    pub fn validate(&self) -> Result<(), &'static str> {
        let valid = match self.status {
            EvidenceStatus::Complete => self.reason.is_none(),
            EvidenceStatus::Pending | EvidenceStatus::Partial | EvidenceStatus::Unavailable => {
                self.reason.is_some()
            }
        };
        if valid {
            Ok(())
        } else {
            Err("reason contradicts evidence completeness status")
        }
    }
}

serialize_validated!(EvidenceCompleteness { status, reason });

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RawEvidenceCompleteness {
    status: EvidenceStatus,
    #[serde(deserialize_with = "required_option")]
    reason: Option<NonBlankString>,
}

impl<'de> Deserialize<'de> for EvidenceCompleteness {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        let raw = RawEvidenceCompleteness::deserialize(deserializer)?;
        let value = Self {
            status: raw.status,
            reason: raw.reason,
        };
        value.validate().map_err(de::Error::custom)?;
        Ok(value)
    }
}

#[derive(Clone, Debug, JsonSchema, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct EvalRun {
    pub schema_version: SchemaVersion,
    pub document_kind: EvalRunDocumentKind,
    pub id: EvalRunId,
    pub status: EvalRunStatus,
    pub created_at: Timestamp,
    pub started_at: Option<Timestamp>,
    pub ended_at: Option<Timestamp>,
    pub duration_ms: Option<u64>,
    pub attributes: JsonObject,
    pub failure: Option<Failure>,
}

impl EvalRun {
    pub fn validate(&self) -> Result<(), &'static str> {
        let failure_required = matches!(
            self.status,
            EvalRunStatus::Fail
                | EvalRunStatus::Error
                | EvalRunStatus::Cancelled
                | EvalRunStatus::Interrupted
        );
        if failure_required == self.failure.is_some() {
            Ok(())
        } else {
            Err("failure contradicts eval run status")
        }
    }
}

serialize_validated!(EvalRun {
    schema_version,
    document_kind,
    id,
    status,
    created_at,
    started_at,
    ended_at,
    duration_ms,
    attributes,
    failure,
});

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RawEvalRun {
    schema_version: SchemaVersion,
    document_kind: EvalRunDocumentKind,
    id: EvalRunId,
    status: EvalRunStatus,
    created_at: Timestamp,
    #[serde(deserialize_with = "required_option")]
    started_at: Option<Timestamp>,
    #[serde(deserialize_with = "required_option")]
    ended_at: Option<Timestamp>,
    #[serde(deserialize_with = "required_option")]
    duration_ms: Option<u64>,
    attributes: JsonObject,
    #[serde(deserialize_with = "required_option")]
    failure: Option<Failure>,
}

impl<'de> Deserialize<'de> for EvalRun {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        let raw = RawEvalRun::deserialize(deserializer)?;
        let value = Self {
            schema_version: raw.schema_version,
            document_kind: raw.document_kind,
            id: raw.id,
            status: raw.status,
            created_at: raw.created_at,
            started_at: raw.started_at,
            ended_at: raw.ended_at,
            duration_ms: raw.duration_ms,
            attributes: raw.attributes,
            failure: raw.failure,
        };
        value.validate().map_err(de::Error::custom)?;
        Ok(value)
    }
}

#[derive(Clone, Debug, JsonSchema, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Invocation {
    pub schema_version: SchemaVersion,
    pub document_kind: InvocationDocumentKind,
    pub id: InvocationId,
    pub run_id: EvalRunId,
    pub case: CaseSnapshot,
    pub attempt: NonZeroU32,
    pub status: InvocationStatus,
    pub started_at: Option<Timestamp>,
    pub ended_at: Option<Timestamp>,
    pub duration_ms: Option<u64>,
    pub output_recorded: bool,
    pub output: Option<Value>,
    pub provenance: ExecutionProvenance,
    pub evidence_completeness: EvidenceCompleteness,
    pub attributes: JsonObject,
    pub failure: Option<Failure>,
}

impl Invocation {
    pub fn validate(&self) -> Result<(), &'static str> {
        let failure_required = matches!(
            self.status,
            InvocationStatus::Fail
                | InvocationStatus::Error
                | InvocationStatus::Cancelled
                | InvocationStatus::Skipped
                | InvocationStatus::Interrupted
        );
        if failure_required != self.failure.is_some() {
            return Err("failure contradicts invocation status");
        }
        if !self.output_recorded && self.output.is_some() {
            return Err("output present when output_recorded is false");
        }
        self.evidence_completeness.validate()
    }
}

serialize_validated!(Invocation {
    schema_version,
    document_kind,
    id,
    run_id,
    case,
    attempt,
    status,
    started_at,
    ended_at,
    duration_ms,
    output_recorded,
    output,
    provenance,
    evidence_completeness,
    attributes,
    failure,
});

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RawInvocation {
    schema_version: SchemaVersion,
    document_kind: InvocationDocumentKind,
    id: InvocationId,
    run_id: EvalRunId,
    case: CaseSnapshot,
    attempt: NonZeroU32,
    status: InvocationStatus,
    #[serde(deserialize_with = "required_option")]
    started_at: Option<Timestamp>,
    #[serde(deserialize_with = "required_option")]
    ended_at: Option<Timestamp>,
    #[serde(deserialize_with = "required_option")]
    duration_ms: Option<u64>,
    output_recorded: bool,
    #[serde(deserialize_with = "required_option")]
    output: Option<Value>,
    provenance: ExecutionProvenance,
    evidence_completeness: EvidenceCompleteness,
    attributes: JsonObject,
    #[serde(deserialize_with = "required_option")]
    failure: Option<Failure>,
}

impl<'de> Deserialize<'de> for Invocation {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        let raw = RawInvocation::deserialize(deserializer)?;
        let value = Self {
            schema_version: raw.schema_version,
            document_kind: raw.document_kind,
            id: raw.id,
            run_id: raw.run_id,
            case: raw.case,
            attempt: raw.attempt,
            status: raw.status,
            started_at: raw.started_at,
            ended_at: raw.ended_at,
            duration_ms: raw.duration_ms,
            output_recorded: raw.output_recorded,
            output: raw.output,
            provenance: raw.provenance,
            evidence_completeness: raw.evidence_completeness,
            attributes: raw.attributes,
            failure: raw.failure,
        };
        value.validate().map_err(de::Error::custom)?;
        Ok(value)
    }
}

#[derive(Clone, Debug, JsonSchema, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Span {
    pub schema_version: SchemaVersion,
    pub document_kind: SpanDocumentKind,
    pub invocation_id: InvocationId,
    pub trace_id: TraceId,
    pub span_id: SpanId,
    pub parent_span_id: Option<SpanId>,
    pub name: NonBlankString,
    pub span_kind: NonBlankString,
    pub status: SpanStatus,
    pub status_message: Option<NonBlankString>,
    pub started_at: Option<Timestamp>,
    pub ended_at: Option<Timestamp>,
    pub duration_ms: Option<u64>,
    pub input_recorded: bool,
    pub input: Option<Value>,
    pub output_recorded: bool,
    pub output: Option<Value>,
    pub attributes: JsonObject,
}

impl Span {
    pub fn validate(&self) -> Result<(), &'static str> {
        if matches!(self.status, SpanStatus::Error) != self.status_message.is_some() {
            return Err("status_message contradicts span status");
        }
        if !self.input_recorded && self.input.is_some() {
            return Err("input present when input_recorded is false");
        }
        if !self.output_recorded && self.output.is_some() {
            return Err("output present when output_recorded is false");
        }
        Ok(())
    }
}

serialize_validated!(Span {
    schema_version,
    document_kind,
    invocation_id,
    trace_id,
    span_id,
    parent_span_id,
    name,
    span_kind,
    status,
    status_message,
    started_at,
    ended_at,
    duration_ms,
    input_recorded,
    input,
    output_recorded,
    output,
    attributes,
});

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RawSpan {
    schema_version: SchemaVersion,
    document_kind: SpanDocumentKind,
    invocation_id: InvocationId,
    trace_id: TraceId,
    span_id: SpanId,
    #[serde(deserialize_with = "required_option")]
    parent_span_id: Option<SpanId>,
    name: NonBlankString,
    span_kind: NonBlankString,
    status: SpanStatus,
    #[serde(deserialize_with = "required_option")]
    status_message: Option<NonBlankString>,
    #[serde(deserialize_with = "required_option")]
    started_at: Option<Timestamp>,
    #[serde(deserialize_with = "required_option")]
    ended_at: Option<Timestamp>,
    #[serde(deserialize_with = "required_option")]
    duration_ms: Option<u64>,
    input_recorded: bool,
    #[serde(deserialize_with = "required_option")]
    input: Option<Value>,
    output_recorded: bool,
    #[serde(deserialize_with = "required_option")]
    output: Option<Value>,
    attributes: JsonObject,
}

impl<'de> Deserialize<'de> for Span {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        let raw = RawSpan::deserialize(deserializer)?;
        let value = Self {
            schema_version: raw.schema_version,
            document_kind: raw.document_kind,
            invocation_id: raw.invocation_id,
            trace_id: raw.trace_id,
            span_id: raw.span_id,
            parent_span_id: raw.parent_span_id,
            name: raw.name,
            span_kind: raw.span_kind,
            status: raw.status,
            status_message: raw.status_message,
            started_at: raw.started_at,
            ended_at: raw.ended_at,
            duration_ms: raw.duration_ms,
            input_recorded: raw.input_recorded,
            input: raw.input,
            output_recorded: raw.output_recorded,
            output: raw.output,
            attributes: raw.attributes,
        };
        value.validate().map_err(de::Error::custom)?;
        Ok(value)
    }
}

#[derive(Clone, Debug, JsonSchema, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct CheckResult {
    pub schema_version: SchemaVersion,
    pub document_kind: CheckResultDocumentKind,
    pub id: CheckResultId,
    pub invocation_id: InvocationId,
    pub name: NonBlankString,
    pub status: CheckStatus,
    pub started_at: Option<Timestamp>,
    pub ended_at: Option<Timestamp>,
    pub duration_ms: Option<u64>,
    pub evidence: JsonObject,
    pub failure: Option<Failure>,
}

impl CheckResult {
    pub fn validate(&self) -> Result<(), &'static str> {
        let failure_required = matches!(
            self.status,
            CheckStatus::Fail | CheckStatus::Error | CheckStatus::Skipped
        );
        if failure_required == self.failure.is_some() {
            Ok(())
        } else {
            Err("failure contradicts check result status")
        }
    }
}

serialize_validated!(CheckResult {
    schema_version,
    document_kind,
    id,
    invocation_id,
    name,
    status,
    started_at,
    ended_at,
    duration_ms,
    evidence,
    failure,
});

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RawCheckResult {
    schema_version: SchemaVersion,
    document_kind: CheckResultDocumentKind,
    id: CheckResultId,
    invocation_id: InvocationId,
    name: NonBlankString,
    status: CheckStatus,
    #[serde(deserialize_with = "required_option")]
    started_at: Option<Timestamp>,
    #[serde(deserialize_with = "required_option")]
    ended_at: Option<Timestamp>,
    #[serde(deserialize_with = "required_option")]
    duration_ms: Option<u64>,
    evidence: JsonObject,
    #[serde(deserialize_with = "required_option")]
    failure: Option<Failure>,
}

impl<'de> Deserialize<'de> for CheckResult {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        let raw = RawCheckResult::deserialize(deserializer)?;
        let value = Self {
            schema_version: raw.schema_version,
            document_kind: raw.document_kind,
            id: raw.id,
            invocation_id: raw.invocation_id,
            name: raw.name,
            status: raw.status,
            started_at: raw.started_at,
            ended_at: raw.ended_at,
            duration_ms: raw.duration_ms,
            evidence: raw.evidence,
            failure: raw.failure,
        };
        value.validate().map_err(de::Error::custom)?;
        Ok(value)
    }
}

#[derive(Clone, Debug, Deserialize, JsonSchema, PartialEq, Serialize)]
#[serde(untagged)]
pub enum ProtocolDocument {
    EvalRun(Box<EvalRun>),
    Invocation(Box<Invocation>),
    Span(Box<Span>),
    CheckResult(Box<CheckResult>),
}

impl ProtocolDocument {
    pub fn document_kind(&self) -> &'static str {
        match self {
            Self::EvalRun(_) => "eval_run",
            Self::Invocation(_) => "invocation",
            Self::Span(_) => "span",
            Self::CheckResult(_) => "check_result",
        }
    }
}
