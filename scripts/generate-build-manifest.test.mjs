import { afterEach, describe, expect, it, vi } from "vitest";

const generateRepositoryBuildManifest = vi.fn();

vi.mock("./build-manifest.mjs", () => ({
  generateRepositoryBuildManifest,
  repositoryRoot: "/repository",
}));

const originalArguments = [...process.argv];

afterEach(() => {
  process.argv = [...originalArguments];
});

describe("build manifest CLI", () => {
  it("forwards output arguments and the repository root", async () => {
    process.argv = [
      "node",
      "generate-build-manifest.mjs",
      "--output",
      "manifest.json",
    ];

    await import("./generate-build-manifest.mjs");

    expect(generateRepositoryBuildManifest).toHaveBeenCalledWith(
      ["--output", "manifest.json"],
      "/repository",
    );
  });
});
