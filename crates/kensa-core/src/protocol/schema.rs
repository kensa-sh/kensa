use std::collections::BTreeMap;

use schemars::{JsonSchema, schema_for};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

use super::documents::{CheckResult, EvalRun, Invocation, ProtocolDocument, Span};

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum RejectionBoundary {
    Schema,
    Rust,
}

#[derive(Debug)]
pub struct ProtocolError {
    boundary: RejectionBoundary,
    message: String,
}

impl ProtocolError {
    pub fn boundary(&self) -> RejectionBoundary {
        self.boundary
    }

    pub fn message(&self) -> &str {
        &self.message
    }
}

pub fn parse_document(bytes: &[u8]) -> Result<ProtocolDocument, ProtocolError> {
    serde_json::from_slice(bytes).map_err(|error| ProtocolError {
        boundary: RejectionBoundary::Rust,
        message: error.to_string(),
    })
}

pub fn canonical_json<T: Serialize>(value: &T) -> Result<Vec<u8>, serde_json::Error> {
    let mut value = serde_json::to_value(value)?;
    sort_value(&mut value);
    let mut bytes = serde_json::to_string_pretty(&value)?.into_bytes();
    bytes.push(b'\n');
    Ok(bytes)
}

fn sort_value(value: &mut Value) {
    match value {
        Value::Array(values) => values.iter_mut().for_each(sort_value),
        Value::Object(object) => {
            let mut sorted = Map::new();
            for (key, mut value) in std::mem::take(object) {
                sort_value(&mut value);
                sorted.insert(key, value);
            }
            let mut entries = sorted.into_iter().collect::<Vec<_>>();
            entries.sort_by(|left, right| left.0.cmp(&right.0));
            object.extend(entries);
        }
        _ => {}
    }
}

fn schema_value<T: JsonSchema>() -> Result<Value, serde_json::Error> {
    let mut value = serde_json::to_value(schema_for!(T))?;
    require_all_properties(&mut value);
    Ok(value)
}

fn require_all_properties(value: &mut Value) {
    match value {
        Value::Array(values) => values.iter_mut().for_each(require_all_properties),
        Value::Object(object) => {
            if let Some(Value::Object(properties)) = object.get("properties") {
                object.insert(
                    "required".to_owned(),
                    Value::Array(properties.keys().cloned().map(Value::String).collect()),
                );
            }
            object.values_mut().for_each(require_all_properties);
        }
        _ => {}
    }
}

pub fn generated_schemas() -> Result<BTreeMap<String, Value>, serde_json::Error> {
    let mut union = schema_value::<ProtocolDocument>()?;
    if let Value::Object(object) = &mut union
        && let Some(branches) = object.remove("anyOf")
    {
        object.insert("oneOf".to_owned(), branches);
    }

    Ok(BTreeMap::from([
        (
            "check-result.schema.json".to_owned(),
            schema_value::<CheckResult>()?,
        ),
        (
            "eval-run.schema.json".to_owned(),
            schema_value::<EvalRun>()?,
        ),
        (
            "invocation.schema.json".to_owned(),
            schema_value::<Invocation>()?,
        ),
        ("protocol-document.schema.json".to_owned(), union),
        ("span.schema.json".to_owned(), schema_value::<Span>()?),
    ]))
}
