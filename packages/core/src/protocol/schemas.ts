import { z } from "zod";
import {
  checkResultSchema,
  evalRunSchema,
  invocationSchema,
  protocolDocumentSchema,
  spanSchema,
} from "./documents.js";
import type { JsonObject } from "./json.js";

export function generateProtocolSchemas(): Readonly<
  Record<string, JsonObject>
> {
  const options = { target: "draft-2020-12" as const, reused: "ref" as const };
  return {
    "eval-run": z.toJSONSchema(evalRunSchema, options) as JsonObject,
    invocation: z.toJSONSchema(invocationSchema, options) as JsonObject,
    span: z.toJSONSchema(spanSchema, options) as JsonObject,
    "check-result": z.toJSONSchema(checkResultSchema, options) as JsonObject,
    "protocol-document": z.toJSONSchema(
      protocolDocumentSchema,
      options,
    ) as JsonObject,
  };
}
