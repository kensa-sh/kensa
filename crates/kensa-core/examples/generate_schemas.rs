use std::env;
use std::fs;
use std::path::PathBuf;

use kensa_core::protocol::{canonical_json, generated_schemas};

fn main() {
    let output = env::args_os()
        .nth(1)
        .map(PathBuf::from)
        .expect("usage: generate_schemas <output-directory>");
    fs::create_dir_all(&output).expect("create schema directory");
    for (filename, schema) in generated_schemas().expect("generate schemas") {
        fs::write(
            output.join(filename),
            canonical_json(&schema).expect("serialize schema"),
        )
        .expect("write schema");
    }
}
