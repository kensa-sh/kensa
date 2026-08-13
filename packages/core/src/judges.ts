import { z } from "zod";

import {
  completeCase,
  parseChecks,
  type AwaitingCheck,
  type EvaluationCheck,
  type MultiCheckComplete,
} from "./evaluation.js";
import { parseInput } from "./errors.js";
import { jsonObjectSchema } from "./json.js";

const judgeObservationSchema = z
  .strictObject({
    id: z.string().trim().min(1),
    criteria: z.string().trim().min(1),
    required: z.boolean(),
    passed: z.boolean(),
    reasoning: z.string().trim().min(1),
    evidence: z.array(z.string()),
    provider: z.string().trim().min(1).nullable(),
    model: z.string().trim().min(1).nullable(),
    metadata: jsonObjectSchema,
    error: z.boolean(),
    error_kind: z.enum(["contract", "execution"]).nullable(),
  })
  .superRefine((judge, context) => {
    if (judge.id === "pytest") {
      context.addIssue({
        code: "custom",
        path: ["id"],
        message: "judge ID is reserved for the pytest check",
      });
    }
    if (judge.error !== (judge.error_kind !== null)) {
      context.addIssue({
        code: "custom",
        path: ["error_kind"],
        message: "judge error and error kind must agree",
      });
    }
    if (judge.passed && judge.error) {
      context.addIssue({
        code: "custom",
        path: ["passed"],
        message: "a judge error cannot be passing",
      });
    }
  });

const judgeObservationsSchema = z
  .array(judgeObservationSchema)
  .superRefine((judges, context) => {
    const ids = new Set<string>();
    for (const [index, judge] of judges.entries()) {
      if (ids.has(judge.id)) {
        context.addIssue({
          code: "custom",
          path: [index, "id"],
          message: "judge observations contain duplicate IDs",
        });
      }
      ids.add(judge.id);
    }
  });

export type JudgeObservation = z.infer<typeof judgeObservationSchema>;

export interface JudgeEvaluationComplete extends MultiCheckComplete {
  judges: JudgeObservation[];
}

export function parseJudgeObservations(input: unknown): JudgeObservation[] {
  return parseInput(
    judgeObservationsSchema,
    input,
    "judge observations violate the core contract",
  ).sort(compareJudges);
}

export function completeCaseWithJudges(
  state: AwaitingCheck,
  checksInput: unknown,
  judgesInput: unknown,
): JudgeEvaluationComplete {
  const checks = parseChecks(checksInput);
  const judges = parseJudgeObservations(judgesInput);
  const judgeChecks =
    state.observation.failure === null
      ? judges.flatMap((judge) =>
          judge.required ? [checkForJudge(judge)] : [],
        )
      : [];
  return {
    ...completeCase(state, [...checks, ...judgeChecks]),
    judges,
  };
}

function checkForJudge(judge: JudgeObservation): EvaluationCheck {
  if (judge.error) {
    return {
      id: judge.id,
      outcome: "error",
      failure: failureForJudge(judge, judge.error_kind!),
    };
  }
  if (!judge.passed) {
    return {
      id: judge.id,
      outcome: "unsatisfied",
      failure: failureForJudge(judge, "criteria"),
    };
  }
  return { id: judge.id, outcome: "satisfied", failure: null };
}

function failureForJudge(
  judge: JudgeObservation,
  kind: "contract" | "criteria" | "execution",
): NonNullable<EvaluationCheck["failure"]> {
  return {
    category: "judge",
    kind,
    message: judge.reasoning,
    evidence: {
      criteria: judge.criteria,
      evidence: judge.evidence,
      provider: judge.provider,
      model: judge.model,
      metadata: judge.metadata,
    },
  };
}

function compareJudges(
  left: JudgeObservation,
  right: JudgeObservation,
): number {
  return Number(left.id > right.id) - Number(left.id < right.id);
}
