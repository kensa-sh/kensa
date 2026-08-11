import { z } from "zod";

import { verifyRunResult, type RunResult } from "./aggregation.js";
import { KensaCoreError, parseInput } from "./errors.js";
import { canonicalJson, digestJson, type JsonValue } from "./json.js";
import { verifyProtectionSuite, type ProtectionSuite } from "./protection.js";

const nonblankSchema = z.string().trim().min(1);
const githubContextSchema = z.strictObject({
  repository: z.string().regex(/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/),
  repository_id: z.string().regex(/^[1-9]\d*$/),
  event: z.enum(["pull_request", "push", "workflow_dispatch"]),
  sha: z.string().regex(/^(?:[0-9a-f]{40}|[0-9a-f]{64})$/),
  ref: z.string().regex(/^refs\/(?:heads|tags|pull)\/.+$/),
  workflow_path: z.string().regex(/^\.github\/workflows\/[^/]+\.ya?ml$/),
  job: nonblankSchema,
  run_id: z.string().regex(/^[1-9]\d*$/),
  run_attempt: z.number().int().positive(),
  actor: nonblankSchema,
  application: nonblankSchema,
  environment: nonblankSchema,
});
const protectionResultInputSchema = z.strictObject({
  suite: z.unknown(),
  result: z.unknown(),
  github: githubContextSchema,
});
const protectionResultSchema = z.strictObject({
  schema_version: z.literal("kensa.protection_result.v1"),
  suite: z.unknown(),
  result: z.unknown(),
  github: githubContextSchema,
  digest: z.string().regex(/^[0-9a-f]{64}$/),
});

export type GitHubRunContext = z.infer<typeof githubContextSchema>;

export interface ProtectionResult {
  schema_version: "kensa.protection_result.v1";
  suite: ProtectionSuite;
  result: RunResult;
  github: GitHubRunContext;
  digest: string;
}

export async function buildProtectionResult(
  input: unknown,
): Promise<ProtectionResult> {
  const parsed = parseInput(
    protectionResultInputSchema,
    input,
    "protection result input violates the core contract",
  );
  const suite = await verifyProtectionSuite(parsed.suite);
  const result = verifyRunResult(parsed.result);
  validateGitHubEventRef(parsed.github);
  validateGitHubBinding(suite, parsed.github);
  validateResultCoverage(suite, result);
  const value = {
    schema_version: "kensa.protection_result.v1" as const,
    suite,
    result,
    github: parsed.github,
  };
  return { ...value, digest: await digestJson(value) };
}

export async function verifyProtectionResult(
  input: unknown,
): Promise<ProtectionResult> {
  const artifact = parseInput(
    protectionResultSchema,
    input,
    "protection result violates the core contract",
  );
  const expected = await buildProtectionResult({
    suite: artifact.suite,
    result: artifact.result,
    github: artifact.github,
  });
  if (canonicalJson(input) !== canonicalJson(expected)) {
    throw new KensaCoreError(
      "invalid_input",
      "protection result is not canonical",
    );
  }
  return expected;
}

function validateGitHubBinding(
  suite: ProtectionSuite,
  github: GitHubRunContext,
): void {
  const expected = {
    workflow_path: suite.bindings.workflow.path,
    job: suite.bindings.workflow.job,
    application: suite.bindings.application.name,
    environment: suite.bindings.application.environment,
  };
  for (const [field, value] of Object.entries(expected)) {
    if (github[field as keyof typeof expected] !== value) {
      throw new KensaCoreError(
        "invalid_input",
        `GitHub ${field} contradicts the protection suite binding`,
      );
    }
  }
}

function validateGitHubEventRef(github: GitHubRunContext): void {
  const valid =
    github.event === "pull_request"
      ? github.ref.startsWith("refs/pull/")
      : github.ref.startsWith("refs/heads/") ||
        github.ref.startsWith("refs/tags/");
  if (!valid) {
    throw new KensaCoreError(
      "invalid_input",
      "GitHub event contradicts its ref",
    );
  }
}

function validateResultCoverage(
  suite: ProtectionSuite,
  result: RunResult,
): void {
  if (!result.complete) {
    throw new KensaCoreError(
      "invalid_input",
      "protection result must contain a complete run",
    );
  }
  const suiteCases = new Map(suite.cases.map((item) => [item.id, item]));
  const resultCases = new Set<string>();
  const nodePrefix = `${suite.bindings.eval.path}::${suite.bindings.eval.entrypoint}`;
  for (const trial of result.trials) {
    if (trial.smoke) {
      throw new KensaCoreError(
        "invalid_input",
        "protection result cannot contain smoke trials",
      );
    }
    if (
      trial.nodeid !== nodePrefix &&
      !trial.nodeid.startsWith(`${nodePrefix}[`)
    ) {
      throw new KensaCoreError(
        "invalid_input",
        `trial ${trial.nodeid} contradicts the suite eval binding`,
      );
    }
    const protectionCase = suiteCases.get(trial.case_id);
    if (protectionCase === undefined) {
      throw new KensaCoreError(
        "invalid_input",
        `protection result contains unknown case ${trial.case_id}`,
      );
    }
    if (
      canonicalJson(trialCaseInput(trial.case)) !==
      canonicalJson(protectionCase.input)
    ) {
      throw new KensaCoreError(
        "invalid_input",
        `trial ${trial.nodeid} contradicts protection case input`,
      );
    }
    resultCases.add(trial.case_id);
  }
  const missing = [...suiteCases.keys()].filter(
    (caseId) => !resultCases.has(caseId),
  );
  if (missing.length > 0) {
    throw new KensaCoreError(
      "invalid_input",
      `protection result is missing cases: ${missing.sort(compareText).join(", ")}`,
    );
  }
}

function trialCaseInput(value: Record<string, JsonValue>): JsonValue {
  if ("input" in value) return value.input;
  if ("messages" in value) return value.messages;
  const entries = Object.entries(value).filter(([key]) => key !== "id");
  return entries.length === 1 ? entries[0]![1] : Object.fromEntries(entries);
}

function compareText(left: string, right: string): number {
  return Number(left > right) - Number(left < right);
}
