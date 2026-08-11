import {
  cancelCase,
  checkCase,
  CoreValidationError,
  EvaluationTransitionError,
  KensaCoreError,
  nextAction,
  observeCase,
  startCase,
  type AwaitingCheck,
  type AwaitingObservation,
  type EvaluationAction,
} from "@kensa/core";
import { ZodError } from "zod";

import {
  ENGINE_VERSION,
  PROTOCOL_VERSION,
  requestEnvelopeSchema,
  responseEnvelopeSchema,
  responseSchema,
  type EngineFailure,
  type EngineRequest,
  type EngineResponse,
  type ResponseEnvelope,
} from "./protocol.js";

export interface EngineDependencies {
  nextAction(
    state: AwaitingObservation | AwaitingCheck,
  ): EvaluationAction | null;
  validateResponse(value: unknown): EngineResponse;
}

interface Transaction {
  response: EngineResponse;
  commit(): void;
}

const defaultDependencies: EngineDependencies = {
  nextAction,
  validateResponse: (value) => responseSchema.parse(value),
};

export class KensaEngine {
  readonly #evaluations = new Map<
    string,
    AwaitingObservation | AwaitingCheck
  >();
  readonly #dependencies: EngineDependencies;
  #handshakeComplete = false;

  constructor(dependencies: EngineDependencies = defaultDependencies) {
    this.#dependencies = dependencies;
  }

  processLine(line: string): ResponseEnvelope {
    let raw: unknown;
    try {
      raw = JSON.parse(line);
    } catch {
      return failure(null, "invalid_message", "message is not valid JSON");
    }
    const requestId = requestIdFrom(raw);
    const parsed = requestEnvelopeSchema.safeParse(raw);
    if (!parsed.success) {
      return failure(
        requestId,
        "invalid_message",
        "message violates the engine contract",
        {
          issues: parsed.error.issues.map((issue) => ({
            path: issue.path.map(String),
            code: issue.code,
          })),
        },
      );
    }
    const envelope = parsed.data;
    try {
      if (!this.#handshakeComplete && envelope.request.type !== "handshake") {
        return failure(
          envelope.id,
          "invalid_transition",
          "handshake must be the first request",
        );
      }
      const transaction = this.#dispatch(envelope.request);
      const response = this.#dependencies.validateResponse(
        transaction.response,
      );
      const responseEnvelope = success(envelope.id, response);
      transaction.commit();
      return responseEnvelope;
    } catch (error) {
      if (error instanceof ZodError) {
        return failure(
          requestId,
          "internal",
          "engine produced an invalid response",
          {
            issues: error.issues.map((issue) => ({
              path: issue.path.map(String),
              code: issue.code,
            })),
          },
        );
      }
      if (error instanceof CoreValidationError) {
        return failure(requestId, "invalid_message", error.message, {
          issues: error.issues,
        });
      }
      if (error instanceof EvaluationTransitionError) {
        return failure(requestId, "invalid_transition", error.message, {
          issues: error.issues,
        });
      }
      if (error instanceof KensaCoreError) {
        return failure(requestId, "internal", error.message, {
          issues: error.issues,
        });
      }
      if (
        error instanceof UnknownEvaluationError ||
        error instanceof VersionMismatchError
      ) {
        return failure(requestId, error.code, error.message);
      }
      return failure(requestId, "internal", errorMessage(error));
    }
  }

  #dispatch(request: EngineRequest): Transaction {
    switch (request.type) {
      case "handshake":
        if (this.#handshakeComplete) {
          throw new EvaluationTransitionError(
            "handshake has already completed",
          );
        }
        if (request.protocol_version !== PROTOCOL_VERSION) {
          throw new VersionMismatchError(request.protocol_version);
        }
        return {
          response: {
            type: "handshake",
            protocol_version: PROTOCOL_VERSION,
            engine_version: ENGINE_VERSION,
          },
          commit: () => {
            this.#handshakeComplete = true;
          },
        };
      case "start_case": {
        if (this.#evaluations.has(request.evaluation_id)) {
          throw new EvaluationTransitionError(
            `evaluation ${request.evaluation_id} has already started`,
          );
        }
        const state = startCase(request.case);
        return {
          response: {
            type: "action",
            action: requiredAction(state, this.#dependencies.nextAction),
            case_id: state.case.id,
          },
          commit: () => {
            this.#evaluations.set(request.evaluation_id, state);
          },
        };
      }
      case "observe": {
        const state = this.#awaitingObservation(request.evaluation_id);
        const observed = observeCase(state, request.observation);
        return {
          response: {
            type: "action",
            action: requiredAction(observed, this.#dependencies.nextAction),
            case_id: observed.case.id,
          },
          commit: () => {
            this.#evaluations.set(request.evaluation_id, observed);
          },
        };
      }
      case "check": {
        const state = this.#awaitingCheck(request.evaluation_id);
        const complete = checkCase(state, request.check);
        return {
          response: {
            type: "result",
            evaluation: {
              phase: "complete",
              case_id: complete.case.id,
              verdict: complete.verdict,
              output: complete.observation.output,
              output_recorded: complete.observation.output_recorded,
              trace: complete.observation.trace,
              failure: complete.failure,
              check_id: complete.check.id,
            },
          },
          commit: () => {
            this.#evaluations.delete(request.evaluation_id);
          },
        };
      }
      case "cancel": {
        const state = this.#activeEvaluation(request.evaluation_id);
        const cancelled = cancelCase(state, request.reason);
        return {
          response: {
            type: "result",
            evaluation: {
              phase: "cancelled",
              case_id: cancelled.case.id,
              reason: cancelled.reason,
              verdict: cancelled.verdict,
              observation: cancelled.observation,
              failure: cancelled.failure,
            },
          },
          commit: () => {
            this.#evaluations.delete(request.evaluation_id);
          },
        };
      }
      case "reset": {
        const released = this.#evaluations.size;
        return {
          response: { type: "reset", released },
          commit: () => {
            this.#evaluations.clear();
          },
        };
      }
    }
  }

  reset(): number {
    const released = this.#evaluations.size;
    this.#evaluations.clear();
    return released;
  }

  #evaluation(id: string): AwaitingObservation | AwaitingCheck {
    const state = this.#evaluations.get(id);
    if (state === undefined) {
      throw new UnknownEvaluationError(id);
    }
    return state;
  }

  #awaitingObservation(id: string): AwaitingObservation {
    const state = this.#evaluation(id);
    if (state.phase !== "awaiting_observation") {
      throw new EvaluationTransitionError(
        `cannot observe case in ${state.phase} phase`,
      );
    }
    return state;
  }

  #awaitingCheck(id: string): AwaitingCheck {
    const state = this.#evaluation(id);
    if (state.phase !== "awaiting_check") {
      throw new EvaluationTransitionError(
        `cannot check case in ${state.phase} phase`,
      );
    }
    return state;
  }

  #activeEvaluation(id: string): AwaitingObservation | AwaitingCheck {
    return this.#evaluation(id);
  }
}

class UnknownEvaluationError extends Error {
  readonly code = "unknown_evaluation" as const;
}

class VersionMismatchError extends Error {
  readonly code = "version_mismatch" as const;

  constructor(received: string) {
    super(
      `unsupported protocol version ${received}; expected ${PROTOCOL_VERSION}`,
    );
  }
}

function failure(
  id: string | null,
  code: EngineFailure["code"],
  message: string,
  details: Record<string, unknown> = {},
): ResponseEnvelope {
  return responseEnvelopeSchema.parse({
    id,
    ok: false,
    failure: { code, message, details },
  });
}

function success(id: string, response: EngineResponse): ResponseEnvelope {
  return responseEnvelopeSchema.parse({ id, ok: true, response });
}

function requiredAction(
  state: AwaitingObservation | AwaitingCheck,
  resolve: (
    state: AwaitingObservation | AwaitingCheck,
  ) => EvaluationAction | null,
): "invoke_agent" | "evaluate_check" {
  const action = resolve(state);
  if (action === null) {
    throw new Error(`active evaluation ${state.case.id} has no next action`);
  }
  return action;
}

function requestIdFrom(value: unknown): string | null {
  if (typeof value !== "object" || value === null || !("id" in value)) {
    return null;
  }
  return typeof value.id === "string" ? value.id : null;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "unknown engine error";
}
