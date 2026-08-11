import { describe, expect, it } from "vitest";

import {
  canonicalJson,
  CoreValidationError,
  digestJson,
  parseJsonValue,
} from "../src/index.js";

describe("canonical JSON", () => {
  it("sorts object keys recursively without reordering arrays", () => {
    expect(canonicalJson({ z: 1, a: { y: true, x: [2, 1] } })).toBe(
      '{"a":{"x":[2,1],"y":true},"z":1}',
    );
  });

  it("orders keys by UTF-16 code units independent of locale and insertion", () => {
    expect(
      canonicalJson({ é: 7, é: 6, a: 5, Z: 4, B: 3, "2": 2, "10": 1 }),
    ).toBe('{"10":1,"2":2,"B":3,"Z":4,"a":5,"é":6,"é":7}');
  });

  it("produces a stable SHA-256 digest", async () => {
    await expect(digestJson({ b: 2, a: 1 })).resolves.toBe(
      "43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777",
    );
  });

  it("rejects unsafe JSON values", () => {
    expect(() => parseJsonValue(undefined)).toThrow(CoreValidationError);
    expect(() => parseJsonValue(Number.POSITIVE_INFINITY)).toThrow(
      CoreValidationError,
    );
    expect(() => parseJsonValue(9_007_199_254_740_992)).toThrow(
      CoreValidationError,
    );
    expect(() => canonicalJson(undefined)).toThrow(CoreValidationError);
    expect(() => canonicalJson(new Date())).toThrow(CoreValidationError);
    expect(() => canonicalJson(9_007_199_254_740_992)).toThrow(
      CoreValidationError,
    );
  });

  it("preserves interoperable numeric boundaries", () => {
    expect(canonicalJson(Number.MAX_SAFE_INTEGER)).toBe("9007199254740991");
    expect(canonicalJson(-0)).toBe("0");
    expect(canonicalJson(1e-7)).toBe("1e-7");
  });

  it("preserves prototype-shaped keys", () => {
    const value = JSON.parse('{"__proto__":{"safe":true},"constructor":1}');
    expect(canonicalJson(value)).toBe(
      '{"__proto__":{"safe":true},"constructor":1}',
    );
  });

  it("rejects cyclic objects", () => {
    const value: Record<string, unknown> = {};
    value.self = value;
    expect(() => parseJsonValue(value)).toThrow(CoreValidationError);
  });
});
