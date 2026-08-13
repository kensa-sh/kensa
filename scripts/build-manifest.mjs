import { createHash } from "node:crypto";
import {
  mkdirSync,
  readFileSync,
  readdirSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { dirname, extname, join, relative, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const textExtensions = new Set([".json", ".py", ".toml", ".ts"]);
export const repositoryRoot = resolve(
  dirname(fileURLToPath(import.meta.url)),
  "..",
);

export async function generateRepositoryBuildManifest(args, root) {
  const core = await loadCore(root);
  return generateBuildManifest(root, args, core);
}

export async function generateBuildManifest(root, args, core) {
  const outputs = parseOutputs(root, args);
  const versions = {
    core: packageVersion(root, "packages/core/package.json"),
    engine: packageVersion(root, "packages/engine/package.json"),
    python: pythonVersion(root, "sdk/python/pyproject.toml"),
    typescript: packageVersion(root, "sdk/typescript/package.json"),
  };
  const release = versions.python;
  for (const [component, version] of Object.entries(versions)) {
    if (version !== release) {
      throw new Error(
        `${component} version ${version} does not match release ${release}`,
      );
    }
  }

  const manifest = await core.buildReleaseManifest({
    schema_version: "kensa.build_manifest.v1",
    release,
    components: {
      core: component(root, "@kensa/core", versions.core, [
        "packages/core/package.json",
        "packages/core/src",
      ]),
      engine: component(root, "kensa-engine", versions.engine, [
        "packages/engine/package.json",
        "packages/engine/src",
      ]),
      sdks: {
        python: component(root, "kensa", versions.python, [
          "sdk/python/pyproject.toml",
          "sdk/python/src/kensa",
        ]),
        typescript: component(root, "@kensa/sdk", versions.typescript, [
          "sdk/typescript/package.json",
          "sdk/typescript/src",
        ]),
      },
    },
    contracts: [
      identity(root, "kensa.build_manifest.v1", [
        "packages/core/src/build-manifest.ts",
      ]),
      identity(root, "kensa.engine.v1", ["packages/engine/src/protocol.ts"]),
      identity(root, "kensa.result.v1", ["packages/core/src/aggregation.ts"]),
    ],
    schemas: [
      identity(root, "evaluation", ["packages/core/src/evaluation.ts"]),
      identity(root, "evidence", ["packages/core/src/evidence.ts"]),
      identity(root, "mining", ["packages/core/src/mining.ts"]),
      identity(root, "protection", [
        "packages/core/src/protection.ts",
        "packages/core/src/protection-result.ts",
      ]),
      identity(root, "sync", ["packages/core/src/sync.ts"]),
    ],
    conformance: readdirSync(join(root, "packages", "core", "conformance"))
      .filter((name) => extname(name) === ".json")
      .map((name) =>
        identity(root, name.slice(0, -extname(name).length), [
          `packages/core/conformance/${name}`,
        ]),
      ),
  });
  await core.verifyReleaseManifest(manifest);

  const content = `${JSON.stringify(manifest, null, 2)}\n`;
  for (const output of outputs) {
    mkdirSync(dirname(output), { recursive: true });
    writeFileSync(output, content);
  }
  process.stdout.write(
    `generated ${manifest.digest} at ${outputs.map((path) => relative(root, path)).join(", ")}\n`,
  );
  return manifest;
}

export async function loadCore(root) {
  const entry = join(root, "packages", "core", "dist", "index.js");
  try {
    return await import(pathToFileURL(entry).href);
  } catch (error) {
    throw new Error(
      "build @kensa/core before generating the release manifest",
      {
        cause: error,
      },
    );
  }
}

export function parseOutputs(root, args) {
  if (args.length === 0) {
    return [
      join(root, "dist", "npm", "kensa-build-manifest.json"),
      join(root, "packages", "core", "dist", "build-manifest.json"),
      join(root, "sdk", "typescript", "dist", "build-manifest.json"),
    ];
  }
  const parsed = [];
  for (let index = 0; index < args.length; index += 2) {
    if (args[index] !== "--output" || args[index + 1] === undefined) {
      throw new Error(
        "usage: generate-build-manifest.mjs [--output <path>]...",
      );
    }
    parsed.push(resolve(root, args[index + 1]));
  }
  return parsed;
}

export function packageVersion(root, path) {
  const value = JSON.parse(readFileSync(join(root, path), "utf8"));
  if (typeof value.version !== "string" || value.version.length === 0) {
    throw new Error(`${path} does not define a package version`);
  }
  return value.version;
}

export function pythonVersion(root, path) {
  const contents = readFileSync(join(root, path), "utf8");
  const match = /^version = "([^"]+)"$/m.exec(contents);
  if (match?.[1] === undefined) {
    throw new Error(`${path} does not define a project version`);
  }
  return match[1];
}

function component(root, name, version, paths) {
  return { name, version, digest: digestPaths(root, paths) };
}

function identity(root, id, paths) {
  return { id, digest: digestPaths(root, paths) };
}

export function digestPaths(root, paths) {
  const files = paths.flatMap((path) => filesAt(root, join(root, path))).sort();
  const hash = createHash("sha256");
  for (const file of files) {
    hash.update(relative(root, file).replaceAll("\\", "/"));
    hash.update("\0");
    hash.update(canonicalBytes(file));
    hash.update("\0");
  }
  return hash.digest("hex");
}

function filesAt(root, path) {
  const metadata = statSync(path);
  if (metadata.isFile()) {
    return [path];
  }
  return readdirSync(path, { withFileTypes: true })
    .filter(
      (entry) =>
        entry.name !== "__pycache__" &&
        entry.name !== ".DS_Store" &&
        !entry.name.endsWith(".pyc"),
    )
    .flatMap((entry) => filesAt(root, join(path, entry.name)));
}

function canonicalBytes(path) {
  const contents = readFileSync(path);
  if (!textExtensions.has(extname(path))) {
    return contents;
  }
  return Buffer.from(contents.toString("utf8").replace(/\r\n?/g, "\n"));
}
