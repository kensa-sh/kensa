import { isJsonValue, type JsonValue } from "./json.js";

function scalarValue(character: string): number {
  const high = character.charCodeAt(0);
  if (character.length === 1) return high;
  const low = character.charCodeAt(1);
  return (high - 0xd800) * 0x400 + low - 0xdc00 + 0x10000;
}

export function compareScalar(leftValue: string, rightValue: string): number {
  const left = Array.from(leftValue, scalarValue);
  const right = Array.from(rightValue, scalarValue);
  for (const [index, leftScalar] of left.entries()) {
    const rightScalar = right[index];
    if (rightScalar === undefined) return 1;
    const difference = leftScalar - rightScalar;
    if (difference !== 0) return difference;
  }
  return left.length - right.length;
}

function compactCanonicalJson(value: JsonValue): string {
  if (value === null || typeof value === "boolean" || typeof value === "number")
    return JSON.stringify(value);
  if (typeof value === "string") return JSON.stringify(value);
  if (Array.isArray(value))
    return `[${value.map(compactCanonicalJson).join(",")}]`;
  const entries = Object.entries(value).sort(([left], [right]) =>
    compareScalar(left, right),
  );
  return `{${entries
    .map(
      ([key, item]) => `${JSON.stringify(key)}:${compactCanonicalJson(item)}`,
    )
    .join(",")}}`;
}

export function canonicalJson(value: unknown): string {
  if (!isJsonValue(value)) throw new TypeError("Value is not JSON-compatible");
  const compact = compactCanonicalJson(value);
  return `${JSON.stringify(JSON.parse(compact) as JsonValue, null, 2)}\n`;
}
