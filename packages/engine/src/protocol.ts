import { z } from "zod";

import {
  buildRunResult,
  canonicalJson,
  CoreValidationError,
  parseCheck,
  parseObservation,
} from "@kensa/core";

export const PROTOCOL_VERSION = "kensa.engine.v1";
export const ENGINE_VERSION = "0.1.0";

const handshakeRequest = z.strictObject({
  type: z.literal("handshake"),
  protocol_version: z.string(),
  client: z.string().min(1),
});
const startCaseRequest = z.strictObject({
  type: z.literal("start_case"),
  evaluation_id: z.string().min(1),
  case: z.unknown(),
});
const observeRequest = z.strictObject({
  type: z.literal("observe"),
  evaluation_id: z.string().min(1),
  observation: z.unknown(),
});
const checkRequest = z.strictObject({
  type: z.literal("check"),
  evaluation_id: z.string().min(1),
  check: z.unknown(),
});
const cancelRequest = z.strictObject({
  type: z.literal("cancel"),
  evaluation_id: z.string().min(1),
  reason: z.unknown(),
});
const resetRequest = z.strictObject({
  type: z.literal("reset"),
});
const buildRunRequest = z.strictObject({
  type: z.literal("build_run"),
  run_id: z.string().min(1),
  complete: z.boolean(),
  interruption: z.unknown(),
  trials: z.unknown(),
});

export const requestEnvelopeSchema = z.strictObject({
  id: z.string().min(1),
  request: z.discriminatedUnion("type", [
    handshakeRequest,
    startCaseRequest,
    observeRequest,
    checkRequest,
    cancelRequest,
    resetRequest,
    buildRunRequest,
  ]),
});

export type RequestEnvelope = z.infer<typeof requestEnvelopeSchema>;
export type EngineRequest = RequestEnvelope["request"];

export interface EngineFailure {
  code:
    | "internal"
    | "invalid_message"
    | "invalid_transition"
    | "unknown_evaluation"
    | "version_mismatch";
  message: string;
  details: Record<string, unknown>;
}

const handshakeResponse = z.strictObject({
  type: z.literal("handshake"),
  protocol_version: z.literal(PROTOCOL_VERSION),
  engine_version: z.literal(ENGINE_VERSION),
});
const actionResponse = z.strictObject({
  type: z.literal("action"),
  action: z.enum(["invoke_agent", "evaluate_check"]),
  case_id: z.string().min(1),
});
const resetResponse = z.strictObject({
  type: z.literal("reset"),
  released: z.number().int().nonnegative(),
});
const completeEvaluationResponse = z
  .strictObject({
    phase: z.literal("complete"),
    case_id: z.string().min(1),
    verdict: z.enum(["pass", "fail", "error", "skipped"]),
    output: z.unknown(),
    output_recorded: z.boolean(),
    trace: z.unknown(),
    failure: z.unknown(),
    check_id: z.string().min(1),
  })
  .superRefine((evaluation, context) => {
    validateCoreValue(
      () =>
        parseObservation({
          output: evaluation.output,
          output_recorded: evaluation.output_recorded,
          trace: evaluation.trace,
          failure: evaluation.failure,
        }),
      context,
    );
  });
const cancelledEvaluationResponse = z
  .strictObject({
    phase: z.literal("cancelled"),
    case_id: z.string().min(1),
    reason: z.string().min(1),
    verdict: z.literal("error"),
    observation: z.unknown().nullable(),
    failure: z.unknown(),
  })
  .superRefine((evaluation, context) => {
    validateCoreValue(
      () =>
        parseCheck({
          id: "cancellation",
          outcome: "error",
          failure: evaluation.failure,
        }),
      context,
    );
    if (evaluation.observation !== null) {
      validateCoreValue(
        () => parseObservation(evaluation.observation),
        context,
      );
    }
  });
const resultResponse = z.strictObject({
  type: z.literal("result"),
  evaluation: z.discriminatedUnion("phase", [
    completeEvaluationResponse,
    cancelledEvaluationResponse,
  ]),
});
const runResultValueResponse = z.strictObject({
  schema_version: z.literal("kensa.result.v1"),
  run_id: z.string().min(1),
  complete: z.boolean(),
  interruption: z.unknown(),
  trials: z.unknown(),
  aggregates: z.unknown(),
  summary: z.unknown(),
});
const runResultResponse = z
  .strictObject({
    type: z.literal("run_result"),
    result: runResultValueResponse,
  })
  .superRefine((response, context) => {
    validateCoreValue(() => {
      const result = response.result;
      const rebuilt = buildRunResult({
        run_id: result.run_id,
        complete: result.complete,
        interruption: result.interruption,
        trials: result.trials,
      });
      if (canonicalJson(rebuilt) !== canonicalJson(response.result)) {
        throw new CoreValidationError(
          "run result contradicts core derivation",
          new z.ZodError([]),
        );
      }
    }, context);
  });

export const responseSchema = z.discriminatedUnion("type", [
  handshakeResponse,
  actionResponse,
  resetResponse,
  resultResponse,
  runResultResponse,
]);
export type EngineResponse = z.infer<typeof responseSchema>;

const failureResponse = z.strictObject({
  code: z.enum([
    "internal",
    "invalid_message",
    "invalid_transition",
    "unknown_evaluation",
    "version_mismatch",
  ]),
  message: z.string(),
  details: z.record(z.string(), z.unknown()),
});

export const responseEnvelopeSchema = z.discriminatedUnion("ok", [
  z.strictObject({
    id: z.string().nullable(),
    ok: z.literal(true),
    response: responseSchema,
  }),
  z.strictObject({
    id: z.string().nullable(),
    ok: z.literal(false),
    failure: failureResponse,
  }),
]);
export type ResponseEnvelope = z.infer<typeof responseEnvelopeSchema>;

function validateCoreValue(
  validate: () => unknown,
  context: z.RefinementCtx,
): void {
  try {
    validate();
  } catch (error) {
    if (!(error instanceof CoreValidationError)) {
      throw error;
    }
    context.addIssue({
      code: "custom",
      message: error.message,
      params: { issues: error.issues },
    });
  }
}
