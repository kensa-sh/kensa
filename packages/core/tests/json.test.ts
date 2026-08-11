import { describe, expect, it } from "vitest";

import { canonicalJson, digestJson, jsonValueSchema } from "../src/index.js";

describe("canonical JSON", () => {
  it("sorts object keys recursively without reordering arrays", () => {
    expect(canonicalJson({ z: 1, a: { y: true, x: [2, 1] } })).toBe(
      '{"a":{"x":[2,1],"y":true},"z":1}',
    );
  });

  it("produces a stable SHA-256 digest", async () => {
    await expect(digestJson({ b: 2, a: 1 })).resolves.toBe(
      "43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777",
    );
  });

  it("rejects unsafe JSON values", () => {
    expect(jsonValueSchema.safeParse(undefined).success).toBe(false);
    expect(jsonValueSchema.safeParse(Number.POSITIVE_INFINITY).success).toBe(false);
    expect(jsonValueSchema.safeParse(9_007_199_254_740_992).success).toBe(false);
  });
});
