import {
  buildRunResult,
  cancelCase,
  completeCaseWithJudges,
  conversationAction,
  ConversationTransitionError,
  CoreValidationError,
  EvaluationTransitionError,
  KensaCoreError,
  nextAction,
  normalizeTraceViews,
  observeCase,
  observeConversation,
  startCase,
  startConversation,
  type AwaitingCheck,
  type AwaitingObservation,
  type ConversationAction,
  type ConversationAwaitingResponse,
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
  conversationAction(
    state: ConversationAwaitingResponse,
  ): ConversationAction | null;
  validateResponse(value: unknown): EngineResponse;
}

interface Transaction {
  response: EngineResponse;
  commit(): void;
}

const defaultDependencies: EngineDependencies = {
  nextAction,
  conversationAction,
  validateResponse: (value) => responseSchema.parse(value),
};

export class KensaEngine {
  readonly #evaluations = new Map<
    string,
    AwaitingObservation | AwaitingCheck
  >();
  readonly #conversations = new Map<string, ConversationAwaitingResponse>();
  readonly #dependencies: EngineDependencies;
  #handshakeComplete = false;

  constructor(dependencies: Partial<EngineDependencies> = {}) {
    this.#dependencies = { ...defaultDependencies, ...dependencies };
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
      if (error instanceof ConversationTransitionError) {
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
        error instanceof UnknownConversationError ||
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
        const complete = completeCaseWithJudges(
          state,
          request.checks,
          request.judges,
        );
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
              checks: complete.checks,
              judges: complete.judges,
            },
          },
          commit: () => {
            this.#evaluations.delete(request.evaluation_id);
          },
        };
      }
      case "start_conversation": {
        if (this.#conversations.has(request.conversation_id)) {
          throw new ConversationTransitionError(
            `conversation ${request.conversation_id} has already started`,
          );
        }
        const state = startConversation(request.conversation);
        return {
          response: {
            type: "conversation_action",
            conversation_id: request.conversation_id,
            action: requiredConversationAction(
              state,
              this.#dependencies.conversationAction,
            ),
          },
          commit: () => {
            this.#conversations.set(request.conversation_id, state);
          },
        };
      }
      case "observe_conversation": {
        const state = this.#conversation(request.conversation_id);
        const observed = observeConversation(state, request.observation);
        if (observed.phase === "complete") {
          return {
            response: {
              type: "conversation_result",
              conversation_id: request.conversation_id,
              result: observed,
            },
            commit: () => {
              this.#conversations.delete(request.conversation_id);
            },
          };
        }
        return {
          response: {
            type: "conversation_action",
            conversation_id: request.conversation_id,
            action: requiredConversationAction(
              observed,
              this.#dependencies.conversationAction,
            ),
          },
          commit: () => {
            this.#conversations.set(request.conversation_id, observed);
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
        const released = this.#evaluations.size + this.#conversations.size;
        return {
          response: { type: "reset", released },
          commit: () => {
            this.#evaluations.clear();
            this.#conversations.clear();
          },
        };
      }
      case "build_run":
        return {
          response: {
            type: "run_result",
            result: buildRunResult({
              run_id: request.run_id,
              complete: request.complete,
              interruption: request.interruption,
              trials: request.trials,
            }),
          },
          commit: () => {},
        };
      case "normalize_traces":
        return {
          response: {
            type: "trace_views",
            traces: normalizeTraceViews(request.traces),
          },
          commit: () => {},
        };
    }
  }

  reset(): number {
    const released = this.#evaluations.size + this.#conversations.size;
    this.#evaluations.clear();
    this.#conversations.clear();
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

  #conversation(id: string): ConversationAwaitingResponse {
    const state = this.#conversations.get(id);
    if (state === undefined) {
      throw new UnknownConversationError(id);
    }
    return state;
  }
}

class UnknownConversationError extends Error {
  readonly code = "unknown_conversation" as const;

  constructor(id: string) {
    super(`conversation ${id} has not started`);
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

function requiredConversationAction(
  state: ConversationAwaitingResponse,
  resolve: (state: ConversationAwaitingResponse) => ConversationAction | null,
): ConversationAction {
  const action = resolve(state);
  if (action === null) {
    throw new Error("active conversation has no next action");
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
