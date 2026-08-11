import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import { canonicalJson, digestJson } from "../src/index.js";

interface Vector {
  name: string;
  input: unknown;
  canonical: string;
  sha256: string;
}

const vectors = JSON.parse(
  readFileSync(
    new URL("../conformance/canonical-json.json", import.meta.url),
    "utf8",
  ),
) as Vector[];

describe("canonical JSON conformance", () => {
  it.each(vectors)("matches $name", async ({ input, canonical, sha256 }) => {
    expect(canonicalJson(input)).toBe(canonical);
    await expect(digestJson(input)).resolves.toBe(sha256);
  });
});
