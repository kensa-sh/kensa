import { z } from "zod";
import {
  checkResultSchema,
  evalRunSchema,
  invocationSchema,
  protocolDocumentSchema,
  spanSchema,
} from "./documents.js";
import { isJsonObject, type JsonObject } from "./json.js";

export function jsonObject(value: unknown): JsonObject {
  const parsed: unknown = JSON.parse(JSON.stringify(value));
  if (!isJsonObject(parsed)) throw new TypeError("Schema is not a JSON object");
  return parsed;
}

export function generateProtocolSchemas(): Readonly<
  Record<string, JsonObject>
> {
  const options = { target: "draft-2020-12" as const, reused: "ref" as const };
  return {
    "eval-run": jsonObject(z.toJSONSchema(evalRunSchema, options)),
    invocation: jsonObject(z.toJSONSchema(invocationSchema, options)),
    span: jsonObject(z.toJSONSchema(spanSchema, options)),
    "check-result": jsonObject(z.toJSONSchema(checkResultSchema, options)),
    "protocol-document": jsonObject(
      z.toJSONSchema(protocolDocumentSchema, options),
    ),
  };
}
