import { describe, expect, it } from "vitest";
import * as core from "../src/index.js";

describe("public wire contract", () => {
  it("exports the complete protocol surface", () => {
    for (const name of [
      "SCHEMA_VERSION",
      "DOCUMENT_KINDS",
      "evalRunSchema",
      "invocationSchema",
      "spanSchema",
      "checkResultSchema",
      "protocolDocumentSchema",
      "caseSnapshotSchema",
      "failureSchema",
      "provenanceSchema",
      "completenessSchema",
      "parseEvalRun",
      "parseInvocation",
      "parseSpan",
      "parseCheckResult",
      "parseProtocolDocument",
      "parseProtocolJson",
      "canonicalProtocolJson",
      "generateProtocolSchemas",
    ])
      expect(core).toHaveProperty(name);
    expect(core.SCHEMA_VERSION).toBe("kensa.protocol.v1");
    expect(core.DOCUMENT_KINDS).toEqual([
      "eval_run",
      "invocation",
      "span",
      "check_result",
    ]);
  });

  it("generates deterministic Draft 2020-12 schemas", () => {
    const first = core.generateProtocolSchemas();
    const second = core.generateProtocolSchemas();
    expect(second).toEqual(first);
    expect(Object.keys(first)).toEqual([
      "eval-run",
      "invocation",
      "span",
      "check-result",
      "protocol-document",
    ]);
    for (const schema of Object.values(first)) {
      expect(schema.$schema).toBe(
        "https://json-schema.org/draft/2020-12/schema",
      );
      expect(JSON.stringify(schema)).toContain('"additionalProperties":false');
    }
    expect(first["protocol-document"]?.oneOf).toHaveLength(4);
  });

  it("encodes structural primitive constraints in JSON Schema", () => {
    const schemas = JSON.stringify(core.generateProtocolSchemas());
    expect(schemas).toContain("9007199254740991");
    expect(schemas).toContain("4294967295");
    expect(schemas).toContain("^(?!0{32}$)[0-9a-f]{32}$");
    expect(schemas).toContain("^(?!0{16}$)[0-9a-f]{16}$");
    expect(schemas).toContain("(\\\\d{4})-(\\\\d{2})-(\\\\d{2})T");
    expect(schemas).toContain('"required":["schema_version"');
  });
});
