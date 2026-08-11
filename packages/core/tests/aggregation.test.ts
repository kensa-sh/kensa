import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import {
  aggregateTrials,
  buildRunResult,
  CoreValidationError,
  parseTrials,
  summarizeTrials,
  type EvaluationFailure,
  type Trial,
} from "../src/index.js";

const fixture = JSON.parse(
  readFileSync(
    new URL(
      "../../../tests/fixtures/results/kensa-result-v1.json",
      import.meta.url,
    ),
    "utf8",
  ),
) as Record<string, unknown>;

function trial(overrides: Partial<Trial> = {}): Trial {
  return {
    nodeid: "test_eval[case-trial1]",
    group_id: "test_eval[case]",
    case_id: "case",
    trial_index: 1,
    configured_trials: 1,
    status: "pass",
    case: { id: "case" },
    output: "ok",
    failure: null,
    duration_ms: 10,
    trace: {
      llm_turns: 0,
      cost_usd: null,
      known_cost_usd: null,
      cost_available: false,
    },
    judges: [],
    active_operation: null,
    smoke: false,
    ...overrides,
  };
}

function failure(
  category: EvaluationFailure["category"] = "agent",
  kind = "assertion",
): EvaluationFailure {
  return { category, kind, message: kind, evidence: {} };
}

describe("run aggregation", () => {
  it("rebuilds the existing result-v1 fixture byte-for-value", () => {
    const result = buildRunResult({
      run_id: fixture.run_id as string,
      complete: fixture.complete as boolean,
      interruption: fixture.interruption,
      trials: fixture.trials,
    });

    expect(result).toEqual(fixture);
  });

  it("derives fail, flaky, error, partial, timeout, and skipped-only groups", () => {
    const trials = [
      trial({ status: "fail", failure: failure(), configured_trials: 2 }),
      trial({
        nodeid: "test_eval[case-trial2]",
        trial_index: 2,
        configured_trials: 2,
        status: "pass",
      }),
      trial({
        nodeid: "failed[one]",
        group_id: "failed",
        case_id: "failed",
        case: {},
        status: "fail",
        failure: failure(),
      }),
      trial({
        nodeid: "error[one]",
        group_id: "error",
        case_id: "error",
        case: {},
        status: "error",
        failure: failure("infrastructure", "crash"),
      }),
      trial({
        nodeid: "timeout[one]",
        group_id: "timeout",
        case_id: "timeout",
        case: {},
        status: "fail",
        failure: failure("agent", "timeout"),
      }),
      trial({
        nodeid: "partial[one]",
        group_id: "partial",
        case_id: "partial",
        case: {},
        configured_trials: 2,
      }),
      trial({
        nodeid: "skipped[one]",
        group_id: "skipped",
        case_id: "skipped",
        case: {},
        status: "skipped",
        failure: failure("harness", "skip"),
      }),
    ];

    expect(
      aggregateTrials(trials).map(({ group_id, verdict }) => [
        group_id,
        verdict,
      ]),
    ).toEqual([
      ["error", "error"],
      ["failed", "fail"],
      ["partial", "partial"],
      ["test_eval[case]", "flaky"],
      ["timeout", "error"],
    ]);
  });

  it("summarizes reliability and complete, partial, legacy, and timed-out costs", () => {
    const trials = [
      trial({
        configured_trials: 2,
        trace: {
          llm_turns: 1,
          cost_available: true,
          cost_usd: 2,
          known_cost_usd: 2,
        },
      }),
      trial({
        nodeid: "short-cohort",
        group_id: "short-cohort",
        case_id: "short-cohort",
        case: {},
      }),
      trial({
        nodeid: "test_eval[case-trial2]",
        trial_index: 2,
        configured_trials: 2,
        status: "error",
        failure: failure("agent", "timeout"),
        active_operation: { kind: "llm" },
        trace: { llm_turns: 2, cost_available: false, known_cost_usd: 1 },
      }),
      trial({
        nodeid: "infra",
        group_id: "infra",
        case_id: "infra",
        case: {},
        status: "error",
        failure: failure("infrastructure", "crash"),
        trace: { cost_usd: 3 },
      }),
      trial({
        nodeid: "smoke",
        group_id: "smoke",
        case_id: "smoke",
        case: {},
        smoke: true,
      }),
    ];

    const summary = summarizeTrials(trials);
    expect(summary.pass_k_curve).toEqual([
      { k: 1, value: 0.75, cohorts: 2 },
      { k: 2, value: 0, cohorts: 1 },
    ]);
    expect(summary.excluded_error_trials).toBe(1);
    expect(summary.error_counts).toMatchObject({ agent: 1, infrastructure: 1 });
    expect(summary.cost_latency).toMatchObject({
      known_cost_usd: 3,
      total_cost_usd: null,
      cost_known_trials: 1,
      cost_relevant_trials: 2,
      cost_complete: false,
      cost_partial: true,
    });
  });

  it("covers every cost observation compatibility shape", () => {
    const variants = [
      trial({ trace: {} }),
      trial({ trace: { cost_available: true, cost_usd: 2 } }),
      trial({ trace: { cost_available: false, known_cost_usd: 1 } }),
      trial({ trace: { known_cost_usd: 1 } }),
      trial({ trace: { cost_usd: 2 } }),
      trial({ trace: { llm_turns: 1, cost_usd: 0 } }),
      trial({ trace: { llm_turns: "2", cost_usd: "bad" } }),
      trial({
        status: "error",
        failure: failure("agent", "timeout"),
        active_operation: { kind: "llm" },
        trace: { cost_usd: 2 },
      }),
    ].map((item, index) => ({
      ...item,
      nodeid: `variant-${index}`,
      group_id: `variant-${index}`,
      case_id: `variant-${index}`,
      case: {},
    }));

    const summary = summarizeTrials(variants);
    expect(summary.cost_latency).toMatchObject({
      cost_relevant_trials: 7,
      known_cost_usd: 8,
      cost_partial: true,
    });
    expect(summarizeTrials([]).cost_latency).toMatchObject({
      latency_p50_ms: 0,
      latency_p95_ms: 0,
      latency_mean_ms: 0,
      cost_coverage: 0,
      mean_llm_turns: 0,
    });
    const precise = [1e-16, 1, -1].map((llmTurns, index) =>
      trial({
        nodeid: `precise-${index}`,
        group_id: `precise-${index}`,
        case_id: `precise-${index}`,
        case: {},
        trace: { llm_turns: llmTurns },
      }),
    );
    expect(summarizeTrials(precise).cost_latency.mean_llm_turns).toBe(
      1e-16 / 3,
    );
  });

  it("rejects invalid trials and run identities", () => {
    expect(() => parseTrials([{ ...trial(), trial_index: 2 }])).toThrow(
      CoreValidationError,
    );
    expect(() => parseTrials([{ ...trial(), failure: failure() }])).toThrow(
      CoreValidationError,
    );
    expect(() => parseTrials([{ ...trial(), case: { id: "other" } }])).toThrow(
      CoreValidationError,
    );
    expect(() => parseTrials([{ ...trial(), output: undefined }])).toThrow(
      CoreValidationError,
    );
    expect(() =>
      parseTrials([
        { ...trial(), status: "fail", failure: { category: "bad" } },
      ]),
    ).toThrow(CoreValidationError);
    expect(() =>
      buildRunResult({
        run_id: "",
        complete: true,
        interruption: null,
        trials: [],
      }),
    ).toThrow(CoreValidationError);
    expect(() =>
      buildRunResult({
        run_id: "run",
        complete: true,
        interruption: {},
        trials: [],
      }),
    ).toThrow("complete run");
    expect(() =>
      buildRunResult({
        run_id: "run",
        complete: true,
        interruption: null,
        trials: [trial({ status: "provisional" })],
      }),
    ).toThrow("provisional");
    expect(() =>
      buildRunResult({
        run_id: "run",
        complete: false,
        interruption: null,
        trials: [trial(), trial()],
      }),
    ).toThrow("duplicate node IDs");
    expect(() =>
      buildRunResult({
        run_id: "run",
        complete: false,
        interruption: null,
        trials: [trial(), trial({ nodeid: "other" })],
      }),
    ).toThrow("duplicate group trial indexes");
    expect(() =>
      buildRunResult({
        run_id: "run",
        complete: false,
        interruption: null,
        trials: [
          trial({ configured_trials: 2 }),
          trial({ nodeid: "other", trial_index: 2, configured_trials: 3 }),
        ],
      }),
    ).toThrow("inconsistent configured trials");
  });
});
