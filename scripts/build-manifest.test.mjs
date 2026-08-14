import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import {
  digestPaths,
  generateBuildManifest,
  generateRepositoryBuildManifest,
  loadCore,
  packageVersion,
  parseOutputs,
  pythonVersion,
} from "./build-manifest.mjs";
import {
  buildReleaseManifest,
  verifyReleaseManifest,
} from "../packages/core/src/build-manifest.ts";

const temporaryRoots = [];
const version = "0.19.1";
const core = { buildReleaseManifest, verifyReleaseManifest };

afterEach(() => {
  for (const root of temporaryRoots.splice(0)) {
    rmSync(root, { recursive: true, force: true });
  }
});

describe("release build manifest generator", () => {
  it("writes identical canonical package manifests", async () => {
    const root = fixtureRoot();
    const manifest = await generateBuildManifest(
      root,
      ["--output", "out/first.json", "--output", "out/second.json"],
      core,
    );

    expect(readFileSync(join(root, "out/first.json"))).toEqual(
      readFileSync(join(root, "out/second.json")),
    );
    expect(manifest.release).toBe(version);
    expect(manifest.components.sdks.typescript.name).toBe("@kensa/sdk");
    expect(manifest.contracts.map(({ id }) => id)).toEqual([
      "kensa.build_manifest.v1",
      "kensa.engine.v1",
      "kensa.result.v1",
    ]);
    expect(manifest.schemas).toHaveLength(5);
    expect(manifest.conformance).toHaveLength(1);
  });

  it("loads the built core for repository generation", async () => {
    const root = fixtureRoot();
    write(
      root,
      "packages/core/dist/index.js",
      `export async function buildReleaseManifest(input) {
        return {...input, contract_digest: "${"a".repeat(64)}", digest: "${"b".repeat(64)}"};
      }
      export async function verifyReleaseManifest(input) { return input; }
      `,
    );

    const loaded = await loadCore(root);
    expect(loaded.buildReleaseManifest).toBeTypeOf("function");
    await generateRepositoryBuildManifest(
      ["--output", "out/manifest.json"],
      root,
    );
    expect(readFileSync(join(root, "out/manifest.json"), "utf8")).toContain(
      "kensa.build_manifest.v1",
    );
  });

  it("fails closed when the built core or versions are invalid", async () => {
    const root = fixtureRoot();
    await expect(loadCore(root)).rejects.toThrow("build @kensa/core");

    write(root, "packages/engine/package.json", '{"version":"0.20.0"}');
    await expect(generateBuildManifest(root, [], core)).rejects.toThrow(
      "engine version 0.20.0 does not match release 0.19.1",
    );
    write(root, "invalid.json", '{"version":null}');
    expect(() => packageVersion(root, "invalid.json")).toThrow(
      "does not define a package version",
    );
    write(root, "invalid.json", '{"version":""}');
    expect(() => packageVersion(root, "invalid.json")).toThrow(
      "does not define a package version",
    );
    write(root, "invalid.toml", 'name = "kensa"');
    expect(() => pythonVersion(root, "invalid.toml")).toThrow(
      "does not define a project version",
    );
  });

  it("validates output arguments and provides package defaults", () => {
    const root = fixtureRoot();
    expect(parseOutputs(root, [])).toEqual([
      join(root, "dist/npm/kensa-build-manifest.json"),
      join(root, "packages/core/dist/build-manifest.json"),
      join(root, "sdk/typescript/dist/build-manifest.json"),
    ]);
    expect(() => parseOutputs(root, ["output", "value"])).toThrow("usage:");
    expect(() => parseOutputs(root, ["--output"])).toThrow("usage:");
  });

  it("normalizes text line endings and excludes transient files", () => {
    const lf = emptyRoot();
    const crlf = emptyRoot();
    write(lf, "src/value.ts", "first\nsecond\n");
    write(crlf, "src/value.ts", "first\r\nsecond\r");
    const expected = digestPaths(lf, ["src"]);

    expect(digestPaths(crlf, ["src"])).toBe(expected);
    write(lf, "src/.DS_Store", "ignored");
    write(lf, "src/value.pyc", "ignored");
    write(lf, "src/__pycache__/value.py", "ignored");
    expect(digestPaths(lf, ["src"])).toBe(expected);

    write(lf, "binary/value.bin", Buffer.from([0, 13, 10, 255]));
    expect(digestPaths(lf, ["binary/value.bin"])).toMatch(/^[0-9a-f]{64}$/);
  });
});

function fixtureRoot() {
  const root = emptyRoot();
  for (const path of [
    "packages/core/package.json",
    "packages/engine/package.json",
    "sdk/typescript/package.json",
  ]) {
    write(root, path, JSON.stringify({ version }));
  }
  write(root, "sdk/python/pyproject.toml", `version = "${version}"`);
  for (const path of [
    "packages/core/src/build-manifest.ts",
    "packages/core/src/aggregation.ts",
    "packages/core/src/evaluation.ts",
    "packages/core/src/evidence.ts",
    "packages/core/src/mining.ts",
    "packages/core/src/protection.ts",
    "packages/core/src/protection-result.ts",
    "packages/core/src/sync.ts",
    "packages/engine/src/protocol.ts",
    "sdk/python/src/kensa/api.py",
    "sdk/typescript/src/index.ts",
  ]) {
    write(root, path, `source for ${path}\n`);
  }
  write(root, "packages/core/conformance/vector.json", "{}\n");
  return root;
}

function emptyRoot() {
  const root = mkdtempSync(join(tmpdir(), "kensa-build-manifest-"));
  temporaryRoots.push(root);
  return root;
}

function write(root, path, contents) {
  const destination = join(root, path);
  mkdirSync(dirname(destination), { recursive: true });
  writeFileSync(destination, contents);
}
