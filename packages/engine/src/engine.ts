import {
  cancelCase,
  checkCase,
  EvaluationTransitionError,
  KensaCoreError,
  nextAction,
  observeCase,
  startCase,
  type EvaluationState,
} from "@kensa/core";
import { ZodError } from "zod";

import {
  ENGINE_VERSION,
  PROTOCOL_VERSION,
  requestEnvelopeSchema,
  responseSchema,
  type EngineFailure,
  type EngineRequest,
  type EngineResponse,
  type ResponseEnvelope,
} from "./protocol.js";

export class KensaEngine {
  readonly #evaluations = new Map<string, EvaluationState>();
  #handshakeComplete = false;

  processLine(line: string): ResponseEnvelope {
    let raw: unknown;
    try {
      raw = JSON.parse(line);
    } catch {
      return failure(null, "invalid_message", "message is not valid JSON");
    }
    const requestId = requestIdFrom(raw);
    try {
      const envelope = requestEnvelopeSchema.parse(raw);
      if (!this.#handshakeComplete && envelope.request.type !== "handshake") {
        return failure(
          envelope.id,
          "invalid_transition",
          "handshake must be the first request",
        );
      }
      const response = responseSchema.parse(this.#dispatch(envelope.request));
      return { id: envelope.id, ok: true, response };
    } catch (error) {
      if (error instanceof ZodError) {
        return failure(
          requestId,
          "invalid_message",
          "message violates the engine contract",
          {
            issues: error.issues.map((issue) => ({
              path: issue.path.join("."),
              code: issue.code,
            })),
          },
        );
      }
      if (error instanceof KensaCoreError) {
        return failure(
          requestId,
          error instanceof EvaluationTransitionError
            ? "invalid_transition"
            : "invalid_message",
          error.message,
          { issues: error.issues },
        );
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

  #dispatch(request: EngineRequest): EngineResponse {
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
        this.#handshakeComplete = true;
        return {
          type: "handshake",
          protocol_version: PROTOCOL_VERSION,
          engine_version: ENGINE_VERSION,
        };
      case "start_case": {
        if (this.#evaluations.has(request.evaluation_id)) {
          throw new EvaluationTransitionError(
            `evaluation ${request.evaluation_id} has already started`,
          );
        }
        const state = startCase(request.case);
        this.#evaluations.set(request.evaluation_id, state);
        return {
          type: "action",
          action: nextAction(state)!,
          case_id: state.case.id,
        };
      }
      case "observe": {
        const state = this.#evaluation(request.evaluation_id);
        const observed = observeCase(state, request.observation);
        this.#evaluations.set(request.evaluation_id, observed);
        return {
          type: "action",
          action: nextAction(observed)!,
          case_id: observed.case.id,
        };
      }
      case "check": {
        const state = this.#evaluation(request.evaluation_id);
        const complete = checkCase(state, request.check);
        this.#evaluations.delete(request.evaluation_id);
        return {
          type: "result",
          evaluation: {
            phase: "complete",
            case_id: complete.case.id,
            verdict: complete.verdict,
            output: complete.observation.output,
            output_recorded: complete.observation.output_recorded,
            trace: complete.observation.trace,
            failure: complete.check.failure,
            check_id: complete.check.id,
          },
        };
      }
      case "cancel": {
        const state = this.#evaluation(request.evaluation_id);
        const cancelled = cancelCase(state, request.reason);
        this.#evaluations.delete(request.evaluation_id);
        return {
          type: "result",
          evaluation: {
            phase: "cancelled",
            case_id: cancelled.case.id,
            reason: cancelled.reason,
            verdict: cancelled.verdict,
            failure: cancelled.failure,
          },
        };
      }
    }
  }

  #evaluation(id: string): EvaluationState {
    const state = this.#evaluations.get(id);
    if (state === undefined) {
      throw new UnknownEvaluationError(id);
    }
    return state;
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
  return { id, ok: false, failure: { code, message, details } };
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
