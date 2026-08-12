import { createHash } from "node:crypto";
import { chmodSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { arch, platform } from "node:process";
import { spawnSync } from "node:child_process";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const pythonRoot = join(root, "sdk", "python");
const targets = {
  "darwin-arm64": {
    bun: "bun-darwin-arm64",
    executable: "kensa-engine",
    wheel: "py3-none-macosx_11_0_arm64",
  },
  "darwin-x64": {
    bun: "bun-darwin-x64-baseline",
    executable: "kensa-engine",
    wheel: "py3-none-macosx_10_15_x86_64",
  },
  "linux-arm64": {
    bun: "bun-linux-arm64",
    executable: "kensa-engine",
    wheel: "py3-none-manylinux_2_17_aarch64",
  },
  "linux-x64": {
    bun: "bun-linux-x64-baseline",
    executable: "kensa-engine",
    wheel: "py3-none-manylinux_2_17_x86_64",
  },
  "win32-x64": {
    bun: "bun-windows-x64-baseline",
    executable: "kensa-engine.exe",
    wheel: "py3-none-win_amd64",
  },
};

const requestedTarget = process.argv[2] ?? `${platform}-${arch}`;
const target = targets[requestedTarget];
if (target === undefined) {
  process.stderr.write(
    `unsupported engine target ${requestedTarget}; expected ${Object.keys(targets).join(", ")}\n`,
  );
  process.exit(2);
}

const buildDirectory = join(pythonRoot, "build", "engine", requestedTarget);
const executable = join(buildDirectory, target.executable);
const metadata = join(buildDirectory, "engine-build.json");
const buildManifest = join(buildDirectory, "build-manifest.json");
mkdirSync(buildDirectory, { recursive: true });

const bunBinary = process.env.BUN_BINARY ?? "bun";
const expectedBunVersion = readFileSync(
  join(root, ".bun-version"),
  "utf8",
).trim();
const actualBunVersion = capture(bunBinary, ["--version"]);
if (actualBunVersion !== expectedBunVersion) {
  process.stderr.write(
    `Bun ${expectedBunVersion} is required; found ${actualBunVersion}\n`,
  );
  process.exit(2);
}

const pnpmBinary =
  process.env.PNPM_BINARY ?? (platform === "win32" ? "pnpm.cmd" : "pnpm");
run(pnpmBinary, ["--filter", "@kensa/core", "build"]);
run(process.execPath, [
  join(root, "scripts", "generate-build-manifest.mjs"),
  "--output",
  buildManifest,
]);
run(bunBinary, [
  "build",
  "--compile",
  `--target=${target.bun}`,
  join(root, "packages", "engine", "src", "cli.ts"),
  "--outfile",
  executable,
]);
if (platform !== "win32") {
  chmodSync(executable, 0o755);
}

const descriptor = {
  schema_version: "kensa.engine_build.v1",
  target: requestedTarget,
  bun_target: target.bun,
  executable: relative(pythonRoot, executable),
  wheel_tag: target.wheel,
  sha256: createHash("sha256").update(readFileSync(executable)).digest("hex"),
  build_manifest: relative(pythonRoot, buildManifest),
  build_manifest_sha256: createHash("sha256")
    .update(readFileSync(buildManifest))
    .digest("hex"),
};
writeFileSync(metadata, `${JSON.stringify(descriptor, null, 2)}\n`);

run(
  process.env.UV_BINARY ?? "uv",
  ["build", "--package", "kensa", "--wheel", "--out-dir", "dist"],
  { KENSA_ENGINE_BUILD: metadata },
);

function run(command, args, environment = {}) {
  const result = spawnSync(command, args, {
    cwd: root,
    env: { ...process.env, ...environment },
    stdio: "inherit",
  });
  if (result.error !== undefined) {
    throw result.error;
  }
  if (result.status !== 0) {
    process.exit(result.status ?? 1);
  }
}

function capture(command, args) {
  const result = spawnSync(command, args, {
    cwd: root,
    encoding: "utf8",
  });
  if (result.error !== undefined) {
    throw result.error;
  }
  if (result.status !== 0) {
    process.stderr.write(result.stderr);
    process.exit(result.status ?? 1);
  }
  return result.stdout.trim();
}
