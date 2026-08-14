import { describe, expect, it } from "vitest";
import { stringify } from "yaml";

import { KensaCoreError, digestJson, type ProtectionSuite } from "@kensa/core";

import {
  defineCase,
  parseProtectionSuiteYaml,
  runEvaluation,
} from "../src/index.js";

const trace = {
  spans: [],
  agent_runs: [],
  tools: [],
  tool_calls: [],
  incomplete: false,
  incomplete_reason: null,
  duration_ms: 0,
  cost_usd: null,
  known_cost_usd: null,
  cost_available: false,
  llm_turns: 0,
};

const observation = {
  output: "hello",
  output_recorded: true,
  trace,
  failure: null,
};

describe("TypeScript SDK", () => {
  it("defines a case through the core contract", () => {
    expect(
      defineCase({ id: " case ", input: { prompt: "hi" }, metadata: {} }),
    ).toEqual({ id: "case", input: { prompt: "hi" }, metadata: {} });
  });

  it("rejects an invalid case", () => {
    expect(() => defineCase({ id: "", input: null, metadata: {} })).toThrow(
      KensaCoreError,
    );
  });

  it("runs async observation and check callbacks through the core", async () => {
    const events: string[] = [];
    const result = await runEvaluation({
      case: { id: "case", input: "hi", metadata: {} },
      observe: async (evaluationCase) => {
        events.push(`observe:${evaluationCase.id}`);
        return observation;
      },
      check: async ({ case: evaluationCase, observation: observed }) => {
        events.push(`check:${evaluationCase.id}:${observed.output}`);
        return { id: "correct", outcome: "satisfied", failure: null };
      },
    });

    expect(events).toEqual(["observe:case", "check:case:hello"]);
    expect(result.verdict).toBe("pass");
  });

  it("returns a failed core evaluation", async () => {
    const failure = {
      category: "judge",
      kind: "mismatch",
      message: "wrong answer",
      evidence: {},
    } as const;
    const result = await runEvaluation({
      case: { id: "case", input: "hi", metadata: {} },
      observe: () => observation,
      check: () => ({
        id: "correct",
        outcome: "unsatisfied",
        failure,
      }),
    });

    expect(result).toMatchObject({ verdict: "fail", failure });
  });

  it.each([
    null,
    {},
    { observe: () => observation },
    { check: () => ({ id: "check", outcome: "satisfied", failure: null }) },
  ])("rejects an invalid evaluation definition %#", async (definition) => {
    await expect(runEvaluation(definition as never)).rejects.toMatchObject({
      code: "invalid_input",
    });
  });

  it("propagates callback failures", async () => {
    const failure = new Error("agent failed");
    await expect(
      runEvaluation({
        case: { id: "case", input: "hi", metadata: {} },
        observe: () => {
          throw failure;
        },
        check: () => ({ id: "unused", outcome: "satisfied", failure: null }),
      }),
    ).rejects.toBe(failure);
  });
});

describe("protection suite YAML", () => {
  it("loads a strict canonical suite", async () => {
    const suite = await protectionSuite();
    await expect(parseProtectionSuiteYaml(stringify(suite))).resolves.toEqual(
      suite,
    );
  });

  it("rejects non-string input", async () => {
    await expect(parseProtectionSuiteYaml(null as never)).rejects.toMatchObject(
      { code: "invalid_input" },
    );
  });

  it("rejects duplicate mapping keys", async () => {
    await expect(
      parseProtectionSuiteYaml("schema_version: one\nschema_version: two\n"),
    ).rejects.toThrow("protection suite YAML is invalid");
  });

  it("rejects unresolved custom tags", async () => {
    await expect(
      parseProtectionSuiteYaml("schema_version: !environment VERSION\n"),
    ).rejects.toThrow("protection suite YAML is invalid");
  });

  it("rejects aliases", async () => {
    const suite = await protectionSuite();
    const source = stringify(suite)
      .replace(
        "name: Checkout protection",
        "name: &suite-name Checkout protection",
      )
      .replace("name: checkout", "name: *suite-name");
    await expect(parseProtectionSuiteYaml(source)).rejects.toThrow(
      "protection suite YAML is unsafe",
    );
  });

  it("rejects values outside the core suite contract", async () => {
    await expect(
      parseProtectionSuiteYaml("schema_version: wrong\n"),
    ).rejects.toBeInstanceOf(KensaCoreError);
  });
});

async function protectionSuite(): Promise<ProtectionSuite> {
  const value = {
    schema_version: "kensa.protection.v1" as const,
    id: "checkout",
    name: "Checkout protection",
    bindings: {
      eval: {
        framework: "pytest" as const,
        path: "tests/evals/test_checkout.py",
        entrypoint: "test_checkout",
      },
      workflow: {
        provider: "github-actions" as const,
        path: ".github/workflows/evals.yml",
        job: "evals",
      },
      application: { name: "checkout", environment: "staging" },
    },
    cases: [
      {
        id: "keeps-cart",
        input: { prompt: "keep the cart" },
        criteria: [
          {
            id: "keeps-cart",
            description: "Keeps cart",
            kind: "assertion" as const,
          },
        ],
        source: {
          candidate_id: "keeps-cart",
          candidate_digest: "a".repeat(64),
          evidence: [
            {
              identity: {
                schema_version: "kensa.source_identity.v1" as const,
                kind: "trace" as const,
                provider: "local",
                source_id: `trace_${"b".repeat(24)}`,
                digest: "c".repeat(64),
              },
              record_digest: "d".repeat(64),
            },
          ],
        },
      },
    ],
  };
  return { ...value, digest: await digestJson(value) };
}
