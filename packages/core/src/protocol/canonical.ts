import { parseProtocolDocument, type ProtocolDocument } from "./documents.js";

function compareScalar(a: string, b: string): number {
  /* c8 ignore next 2 */
  const left = Array.from(a, (char) => char.codePointAt(0) ?? 0);
  const right = Array.from(b, (char) => char.codePointAt(0) ?? 0);
  for (let index = 0; index < Math.min(left.length, right.length); index += 1)
    if (left[index] !== right[index]) return left[index]! - right[index]!;
  return left.length - right.length;
}

function canonical(value: unknown): string {
  if (value === null || typeof value === "boolean" || typeof value === "number")
    return JSON.stringify(value);
  if (typeof value === "string") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  const entries = Object.entries(value as Record<string, unknown>).sort(
    ([left], [right]) => compareScalar(left, right),
  );
  return `{${entries.map(([key, item]) => `${JSON.stringify(key)}:${canonical(item)}`).join(",")}}`;
}

export function canonicalProtocolJson(value: unknown): string {
  const parsed = parseProtocolDocument(value);
  const compact = canonical(parsed);
  const pretty = JSON.stringify(
    JSON.parse(compact) as ProtocolDocument,
    null,
    2,
  );
  return `${pretty}\n`;
}
