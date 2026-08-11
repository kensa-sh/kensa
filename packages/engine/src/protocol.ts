import { z } from "zod";

import { caseSchema, checkSchema, observationSchema } from "@kensa/core";

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
  case: caseSchema,
});
const observeRequest = z.strictObject({
  type: z.literal("observe"),
  evaluation_id: z.string().min(1),
  observation: observationSchema,
});
const checkRequest = z.strictObject({
  type: z.literal("check"),
  evaluation_id: z.string().min(1),
  check: checkSchema,
});
const cancelRequest = z.strictObject({
  type: z.literal("cancel"),
  evaluation_id: z.string().min(1),
  reason: z.string().min(1),
});

export const requestEnvelopeSchema = z.strictObject({
  id: z.string().min(1),
  request: z.discriminatedUnion("type", [
    handshakeRequest,
    startCaseRequest,
    observeRequest,
    checkRequest,
    cancelRequest,
  ]),
});

export type RequestEnvelope = z.infer<typeof requestEnvelopeSchema>;
export type EngineRequest = RequestEnvelope["request"];

export interface EngineFailure {
  code: "internal" | "invalid_message" | "invalid_transition" | "unknown_evaluation" | "version_mismatch";
  message: string;
  details: Record<string, unknown>;
}

export type ResponseEnvelope =
  | { id: string | null; ok: true; response: Record<string, unknown> }
  | { id: string | null; ok: false; failure: EngineFailure };
