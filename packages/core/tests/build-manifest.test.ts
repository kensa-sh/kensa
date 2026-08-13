import { describe, expect, it } from "vitest";

import {
  KensaCoreError,
  buildReleaseManifest,
  verifyReleaseManifest,
} from "../src/index.js";

const digest = (character: string): string => character.repeat(64);

function draft() {
  return {
    schema_version: "kensa.build_manifest.v1" as const,
    release: "0.20.0",
    components: {
      core: {
        name: "@kensa/core" as const,
        version: "0.20.0",
        digest: digest("a"),
      },
      engine: {
        name: "kensa-engine" as const,
        version: "0.20.0",
        digest: digest("b"),
      },
      sdks: {
        python: {
          name: "kensa" as const,
          version: "0.20.0",
          digest: digest("c"),
        },
        typescript: {
          name: "@kensa/sdk" as const,
          version: "0.20.0",
          digest: digest("d"),
        },
      },
    },
    contracts: [
      { id: "kensa.result.v1", digest: digest("f") },
      { id: "kensa.engine.v2", digest: digest("e") },
    ],
    schemas: [
      { id: "trace-view", digest: digest("8") },
      { id: "evaluation", digest: digest("7") },
    ],
    conformance: [
      { id: "trace-view", digest: digest("2") },
      { id: "evaluation", digest: digest("1") },
    ],
  };
}

describe("release build manifest", () => {
  it("builds deterministic component and contract identities", async () => {
    const manifest = await buildReleaseManifest(draft());

    expect(manifest.contracts.map((identity) => identity.id)).toEqual([
      "kensa.engine.v2",
      "kensa.result.v1",
    ]);
    expect(manifest.schemas.map((identity) => identity.id)).toEqual([
      "evaluation",
      "trace-view",
    ]);
    expect(manifest.conformance.map((identity) => identity.id)).toEqual([
      "evaluation",
      "trace-view",
    ]);
    expect(manifest.contract_digest).toMatch(/^[0-9a-f]{64}$/);
    expect(manifest.digest).toMatch(/^[0-9a-f]{64}$/);
    await expect(verifyReleaseManifest(manifest)).resolves.toEqual(manifest);
  });

  it("is independent of identity input ordering", async () => {
    const first = await buildReleaseManifest(draft());
    const reordered = draft();
    reordered.contracts.reverse();
    reordered.schemas.reverse();
    reordered.conformance.reverse();

    await expect(buildReleaseManifest(reordered)).resolves.toEqual(first);
  });

  it.each(["contracts", "schemas", "conformance"] as const)(
    "rejects duplicate %s identities",
    async (group) => {
      const input = draft();
      input[group] = [input[group][0]!, input[group][0]!];

      await expect(buildReleaseManifest(input)).rejects.toThrow(
        `${group} contains duplicate identity`,
      );
    },
  );

  it("rejects invalid component identities", async () => {
    await expect(
      buildReleaseManifest({
        ...draft(),
        components: {
          ...draft().components,
          engine: {
            name: "other-engine",
            version: "0.20.0",
            digest: digest("b"),
          },
        },
      }),
    ).rejects.toBeInstanceOf(KensaCoreError);
  });

  it("rejects malformed identity digests and unknown fields", async () => {
    await expect(
      buildReleaseManifest({
        ...draft(),
        contracts: [{ id: "kensa.engine.v2", digest: "short" }],
        extra: true,
      }),
    ).rejects.toBeInstanceOf(KensaCoreError);
  });

  it("rejects a modified contract digest", async () => {
    const manifest = await buildReleaseManifest(draft());

    await expect(
      verifyReleaseManifest({ ...manifest, contract_digest: digest("0") }),
    ).rejects.toThrow("release build manifest is not canonical");
  });

  it("rejects a modified manifest digest", async () => {
    const manifest = await buildReleaseManifest(draft());

    await expect(
      verifyReleaseManifest({ ...manifest, digest: digest("0") }),
    ).rejects.toThrow("release build manifest is not canonical");
  });

  it("rejects non-canonical identity ordering", async () => {
    const manifest = await buildReleaseManifest(draft());

    await expect(
      verifyReleaseManifest({
        ...manifest,
        contracts: [...manifest.contracts].reverse(),
      }),
    ).rejects.toThrow("release build manifest is not canonical");
  });
});
