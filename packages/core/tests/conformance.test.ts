import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import {
  cancelCase,
  canonicalJson,
  checkCase,
  completeCase,
  CoreValidationError,
  digestJson,
  nextAction,
  observeCase,
  parseCase,
  parseCheck,
  parseObservation,
  startCase,
  type CoreIssue,
} from "../src/index.js";

interface Vector {
  name: string;
  input: unknown;
  canonical: string;
  sha256: string;
}

const vectors = JSON.parse(
  readFileSync(
    new URL("../conformance/canonical-json.json", import.meta.url),
    "utf8",
  ),
) as Vector[];

interface EvaluationVector {
  name: string;
  case: unknown;
  observation: unknown;
  check: unknown;
  actions: ["invoke_agent", "evaluate_check", null];
  terminal: Record<string, unknown>;
}

interface InvalidVector {
  name: string;
  parser: "case" | "observation" | "check";
  input: unknown;
  issue: Pick<CoreIssue, "code" | "path">;
}

interface EvaluationVectors {
  version: number;
  valid: EvaluationVector[];
  multi_check: Omit<EvaluationVector, "check"> & { checks: unknown[] };
  cancelled: {
    name: string;
    case: unknown;
    observation: unknown;
    reason: string;
    terminal: Record<string, unknown>;
  };
  invalid: InvalidVector[];
}

const evaluationVectors = JSON.parse(
  readFileSync(
    new URL("../conformance/evaluation.json", import.meta.url),
    "utf8",
  ),
) as EvaluationVectors;

describe("canonical JSON conformance", () => {
  it.each(vectors)("matches $name", async ({ input, canonical, sha256 }) => {
    expect(canonicalJson(input)).toBe(canonical);
    await expect(digestJson(input)).resolves.toBe(sha256);
  });
});

describe("evaluation conformance", () => {
  it("uses the supported vector version", () => {
    expect(evaluationVectors.version).toBe(1);
  });

  it.each(evaluationVectors.valid)(
    "matches $name",
    ({ case: testCase, observation, check, actions, terminal }) => {
      const started = startCase(testCase);
      const observed = observeCase(started, observation);
      const complete = checkCase(observed, check);

      expect([
        nextAction(started),
        nextAction(observed),
        nextAction(complete),
      ]).toEqual(actions);
      expect(complete).toMatchObject(terminal);
    },
  );

  it(`matches ${evaluationVectors.multi_check.name}`, () => {
    const vector = evaluationVectors.multi_check;
    const started = startCase(vector.case);
    const observed = observeCase(started, vector.observation);
    const complete = completeCase(observed, vector.checks);

    expect([
      nextAction(started),
      nextAction(observed),
      nextAction(complete),
    ]).toEqual(vector.actions);
    expect(complete).toMatchObject(vector.terminal);
  });

  it(`matches ${evaluationVectors.cancelled.name}`, () => {
    const vector = evaluationVectors.cancelled;
    const observed = observeCase(startCase(vector.case), vector.observation);
    const cancelled = cancelCase(observed, vector.reason);

    expect(cancelled).toMatchObject(vector.terminal);
    expect(cancelled.observation).toEqual(observed.observation);
  });

  it.each(evaluationVectors.invalid)(
    "rejects $name with a stable issue",
    ({ parser, input, issue }) => {
      const parse = {
        case: parseCase,
        check: parseCheck,
        observation: parseObservation,
      }[parser];

      try {
        parse(input);
        throw new Error("expected vector to be rejected");
      } catch (error) {
        expect(error).toBeInstanceOf(CoreValidationError);
        expect((error as CoreValidationError).issues).toContainEqual(
          expect.objectContaining(issue),
        );
      }
    },
  );
});
