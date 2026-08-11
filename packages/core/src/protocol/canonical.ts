import { canonicalJson } from "./canonical-json.js";
import { parseProtocolDocument } from "./documents.js";

export function canonicalProtocolJson(value: unknown): string {
  return canonicalJson(parseProtocolDocument(value));
}
