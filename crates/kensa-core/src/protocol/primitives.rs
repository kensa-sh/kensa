use std::str::FromStr;

use schemars::JsonSchema;
use serde::{Deserialize, Deserializer, Serialize, de};
use uuid::{Uuid, Variant, Version};

fn is_nonblank(value: &str) -> bool {
    value.chars().any(|character| !character.is_whitespace())
}

fn is_lower_hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        && value.bytes().any(|byte| byte != b'0')
}

fn is_kensa_id(value: &str, prefix: &str) -> bool {
    let Some(suffix) = value.strip_prefix(prefix) else {
        return false;
    };
    let Ok(uuid) = Uuid::parse_str(suffix) else {
        return false;
    };
    uuid.get_version() == Some(Version::SortRand)
        && uuid.get_variant() == Variant::RFC4122
        && uuid.hyphenated().to_string() == suffix
}

fn is_timestamp(value: &str) -> bool {
    if value.len() != 24 {
        return false;
    }
    let bytes = value.as_bytes();
    if bytes[4] != b'-'
        || bytes[7] != b'-'
        || bytes[10] != b'T'
        || bytes[13] != b':'
        || bytes[16] != b':'
        || bytes[19] != b'.'
        || bytes[23] != b'Z'
        || [0..4, 5..7, 8..10, 11..13, 14..16, 17..19, 20..23]
            .into_iter()
            .flatten()
            .any(|index| !bytes[index].is_ascii_digit())
    {
        return false;
    }

    let year = value[0..4].parse::<u16>().expect("digits checked");
    let month = value[5..7].parse::<u8>().expect("digits checked");
    let day = value[8..10].parse::<u8>().expect("digits checked");
    let hour = value[11..13].parse::<u8>().expect("digits checked");
    let minute = value[14..16].parse::<u8>().expect("digits checked");
    let second = value[17..19].parse::<u8>().expect("digits checked");
    let leap_year =
        year.is_multiple_of(4) && (!year.is_multiple_of(100) || year.is_multiple_of(400));
    let days = match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 if leap_year => 29,
        2 => 28,
        _ => return false,
    };

    (1..=days).contains(&day) && hour <= 23 && minute <= 59 && second <= 59
}

macro_rules! validated_string {
    ($name:ident, $validator:expr, $pattern:literal) => {
        #[derive(Clone, Debug, Eq, Hash, PartialEq, Serialize, JsonSchema)]
        #[serde(transparent)]
        #[schemars(transparent)]
        pub struct $name(#[schemars(regex(pattern = $pattern))] String);

        impl $name {
            pub fn as_str(&self) -> &str {
                &self.0
            }
        }

        impl FromStr for $name {
            type Err = &'static str;

            fn from_str(value: &str) -> Result<Self, Self::Err> {
                if ($validator)(value) {
                    Ok(Self(value.to_owned()))
                } else {
                    Err(concat!("invalid ", stringify!($name)))
                }
            }
        }

        impl<'de> Deserialize<'de> for $name {
            fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
            where
                D: Deserializer<'de>,
            {
                String::deserialize(deserializer)?
                    .parse()
                    .map_err(de::Error::custom)
            }
        }
    };
}

validated_string!(NonBlankString, is_nonblank, r"\S");
validated_string!(CaseId, is_nonblank, r"\S");
validated_string!(
    EvalRunId,
    |value| is_kensa_id(value, "run_"),
    r"^run_[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
);
validated_string!(
    InvocationId,
    |value| is_kensa_id(value, "inv_"),
    r"^inv_[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
);
validated_string!(
    CheckResultId,
    |value| is_kensa_id(value, "chk_"),
    r"^chk_[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
);
validated_string!(TraceId, |value| is_lower_hex(value, 32), r"^[0-9a-f]{32}$");
validated_string!(SpanId, |value| is_lower_hex(value, 16), r"^[0-9a-f]{16}$");
validated_string!(
    Timestamp,
    is_timestamp,
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z$"
);
