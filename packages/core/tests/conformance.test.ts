import { globSync, readFileSync } from "node:fs";
import { Ajv2020 } from "ajv/dist/2020.js";
import { describe, expect, it } from "vitest";
import {
  canonicalProtocolJson,
  generateProtocolSchemas,
  parseProtocolDocument,
  type ProtocolDocument,
} from "../src/index.js";
import { protocolError } from "./helpers.js";

type DocumentKind = ProtocolDocument["document_kind"];
type FixtureEntry = Readonly<{
  path: string;
  valid: boolean;
  document_kind: DocumentKind;
  expected_rejection_boundary: "schema" | "runtime" | null;
  expected_code: string | null;
  expected_path: readonly (string | number)[] | null;
}>;
type Manifest = Readonly<{
  schema_version: "kensa.conformance.v1";
  fixtures: readonly FixtureEntry[];
}>;

const fixtureRoot = new URL(
  "../../../fixtures/conformance/v1/",
  import.meta.url,
);
const manifest = JSON.parse(
  readFileSync(new URL("manifest.json", fixtureRoot), "utf8"),
) as Manifest;
const schemas = generateProtocolSchemas();
const ajv = new Ajv2020({ allErrors: true, strict: true });
const schema = (name: string) => {
  const value = schemas[name];
  if (value === undefined) throw new Error(`Missing schema: ${name}`);
  return value;
};
const validators = {
  eval_run: ajv.compile(schema("eval-run")),
  invocation: ajv.compile(schema("invocation")),
  span: ajv.compile(schema("span")),
  check_result: ajv.compile(schema("check-result")),
};
const documentValidator = ajv.compile(schema("protocol-document"));

describe("cross-language conformance fixtures", () => {
  it("uses one canonical manifest with unique, existing paths", () => {
    expect(manifest.schema_version).toBe("kensa.conformance.v1");
    const paths = manifest.fixtures.map((fixture) => fixture.path);
    expect(new Set(paths).size).toBe(paths.length);
    expect(paths.toSorted()).toEqual(
      globSync(["valid/*.json", "invalid/*.json"], {
        cwd: fixtureRoot.pathname,
      }).toSorted(),
    );
    for (const path of paths)
      expect(() => readFileSync(new URL(path, fixtureRoot))).not.toThrow();
  });

  it("covers every closed enum and minimal or complete document form", () => {
    const values = manifest.fixtures
      .filter((fixture) => fixture.valid)
      .map(
        (fixture) =>
          JSON.parse(
            readFileSync(new URL(fixture.path, fixtureRoot), "utf8"),
          ) as Readonly<Record<string, unknown>>,
      );
    const statuses = (kind: DocumentKind) =>
      [
        ...new Set(
          values
            .filter((value) => value.document_kind === kind)
            .map((value) => value.status),
        ),
      ].toSorted();
    expect(statuses("eval_run")).toEqual(
      [
        "cancelled",
        "error",
        "fail",
        "interrupted",
        "pass",
        "pending",
        "running",
      ].toSorted(),
    );
    expect(statuses("invocation")).toEqual(
      [
        "cancelled",
        "error",
        "fail",
        "interrupted",
        "pass",
        "pending",
        "running",
        "skipped",
      ].toSorted(),
    );
    expect(statuses("span")).toEqual(["error", "ok", "unset"]);
    expect(statuses("check_result")).toEqual([
      "error",
      "fail",
      "pass",
      "skipped",
    ]);
    const serialized = JSON.stringify(values);
    for (const value of [
      "agent",
      "simulator",
      "judge",
      "configuration",
      "infrastructure",
      "harness",
      "unknown",
      "none",
      "captured",
      "sandboxed",
      "live",
      "complete",
      "pending",
      "partial",
      "unavailable",
    ])
      expect(serialized).toContain(`"${value}"`);
    const paths = manifest.fixtures.map((fixture) => fixture.path);
    for (const path of [
      "valid/eval-run-minimal.json",
      "valid/eval-run-complete.json",
      "valid/invocation-minimal.json",
      "valid/invocation-complete.json",
      "valid/span-minimal.json",
      "valid/span-complete.json",
      "valid/check-result-minimal.json",
      "valid/check-result-fail.json",
    ])
      expect(paths).toContain(path);
  });

  it("accepts every valid fixture at every boundary", () => {
    for (const fixture of manifest.fixtures.filter((entry) => entry.valid)) {
      const source = readFileSync(new URL(fixture.path, fixtureRoot), "utf8");
      const value = JSON.parse(source) as unknown;
      expect(validators[fixture.document_kind](value)).toBe(true);
      expect(documentValidator(value)).toBe(true);
      expect(parseProtocolDocument(value).document_kind).toBe(
        fixture.document_kind,
      );
      expect(canonicalProtocolJson(value)).toBe(source);
      expect(fixture.expected_rejection_boundary).toBeNull();
      expect(fixture.expected_code).toBeNull();
      expect(fixture.expected_path).toBeNull();
    }
  });

  it("rejects every invalid fixture at its declared boundary", () => {
    for (const fixture of manifest.fixtures.filter((entry) => !entry.valid)) {
      const value = JSON.parse(
        readFileSync(new URL(fixture.path, fixtureRoot), "utf8"),
      ) as unknown;
      const schemaAccepted = validators[fixture.document_kind](value);
      const unionAccepted = documentValidator(value);
      if (fixture.expected_rejection_boundary === "schema") {
        expect(schemaAccepted, fixture.path).toBe(false);
        expect(unionAccepted, fixture.path).toBe(false);
      } else {
        expect(schemaAccepted, fixture.path).toBe(true);
        expect(unionAccepted, fixture.path).toBe(true);
      }
      const error = protocolError(() => parseProtocolDocument(value));
      expect(error.code, fixture.path).toBe(fixture.expected_code);
      expect(error.path, fixture.path).toEqual(fixture.expected_path);
    }
  });
});
