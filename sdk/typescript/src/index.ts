import {
  KensaCoreError,
  checkCase,
  observeCase,
  parseCase,
  startCase,
  verifyProtectionSuite,
  type Complete,
  type EvaluationCase,
  type EvaluationObservation,
  type ProtectionSuite,
} from "@kensa/core";
import { parseDocument } from "yaml";

type Awaitable<T> = T | PromiseLike<T>;

export interface EvaluationContext {
  case: EvaluationCase;
  observation: EvaluationObservation;
}

export interface EvaluationDefinition {
  case: unknown;
  observe: (evaluationCase: EvaluationCase) => Awaitable<unknown>;
  check: (context: EvaluationContext) => Awaitable<unknown>;
}

export function defineCase(input: unknown): EvaluationCase {
  return parseCase(input);
}

export async function runEvaluation(
  definition: EvaluationDefinition,
): Promise<Complete> {
  if (
    typeof definition !== "object" ||
    definition === null ||
    typeof definition.observe !== "function" ||
    typeof definition.check !== "function"
  ) {
    throw new KensaCoreError(
      "invalid_input",
      "evaluation definition requires observe and check callbacks",
    );
  }
  const started = startCase(definition.case);
  const observed = observeCase(started, await definition.observe(started.case));
  return checkCase(
    observed,
    await definition.check({
      case: observed.case,
      observation: observed.observation,
    }),
  );
}

export async function parseProtectionSuiteYaml(
  source: string,
): Promise<ProtectionSuite> {
  if (typeof source !== "string") {
    throw new KensaCoreError(
      "invalid_input",
      "protection suite YAML must be a string",
    );
  }
  const document = parseDocument(source, {
    customTags: [],
    resolveKnownTags: false,
    schema: "core",
    strict: true,
    uniqueKeys: true,
  });
  const diagnostics = [...document.errors, ...document.warnings];
  if (diagnostics.length > 0) {
    throw new KensaCoreError(
      "invalid_input",
      `protection suite YAML is invalid: ${diagnostics[0]!.message}`,
    );
  }
  let value: unknown;
  try {
    value = document.toJS({ maxAliasCount: 0 });
  } catch (error) {
    throw new KensaCoreError(
      "invalid_input",
      `protection suite YAML is unsafe: ${String(error)}`,
    );
  }
  return verifyProtectionSuite(value);
}

export type {
  Complete,
  EvaluationCase,
  EvaluationObservation,
  EvaluationVerdict,
  ProtectionSuite,
} from "@kensa/core";
