import { spawnSync } from "node:child_process";
import { mkdtempSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const dependencies = {
  makeTemporaryDirectory: mkdtempSync,
  nodeExecutable: process.execPath,
  npmExecutable: npmExecutable(process.env, process.platform),
  readDirectory: readdirSync,
  removeDirectory: rmSync,
  runCommand: run,
  temporaryDirectory: tmpdir(),
  writeFile: writeFileSync,
};

export function main(arguments_, operations, stderr, stdout) {
  const packageDirectory = arguments_[0];
  if (packageDirectory === undefined) {
    stderr.write("usage: verify-npm-packages.mjs <package-directory>\n");
    return 2;
  }

  verifyNpmPackages(packageDirectory, operations, stdout);
  return 0;
}

export function verifyNpmPackages(packageDirectory, operations, stdout) {
  const directory = resolve(packageDirectory);
  const core = packageTarball(
    directory,
    "kensa-core-",
    operations.readDirectory,
  );
  const sdk = packageTarball(directory, "kensa-sdk-", operations.readDirectory);
  const consumer = operations.makeTemporaryDirectory(
    join(operations.temporaryDirectory, "kensa-npm-consumer-"),
  );

  try {
    operations.writeFile(
      join(consumer, "package.json"),
      `${JSON.stringify({ name: "kensa-package-verification", private: true, type: "module" })}\n`,
    );
    const installArguments = [
      "install",
      "--ignore-scripts",
      "--no-audit",
      "--no-fund",
    ];
    operations.runCommand(
      operations.npmExecutable,
      [...installArguments, core],
      consumer,
    );
    operations.runCommand(
      operations.npmExecutable,
      [...installArguments, sdk],
      consumer,
    );
    operations.writeFile(join(consumer, "verify.mjs"), verificationSource());
    operations.runCommand(operations.nodeExecutable, ["verify.mjs"], consumer);
  } finally {
    operations.removeDirectory(consumer, { recursive: true, force: true });
  }

  stdout.write(
    `verified ${basename(core)} and ${basename(sdk)} as an external consumer\n`,
  );
}

export function packageTarball(directory, prefix, readDirectory) {
  const matches = readDirectory(directory)
    .filter((name) => name.startsWith(prefix) && name.endsWith(".tgz"))
    .map((name) => join(directory, name));
  if (matches.length !== 1) {
    throw new Error(
      `expected one ${prefix} package in ${directory}, found ${matches.length}`,
    );
  }
  return matches[0];
}

export function npmExecutable(environment, platform) {
  return (
    environment.KENSA_NPM_BINARY ?? (platform === "win32" ? "npm.cmd" : "npm")
  );
}

export function run(command, arguments_, consumer) {
  const result = spawnSync(command, arguments_, {
    cwd: consumer,
    encoding: "utf8",
    env: { ...process.env, npm_config_cache: join(consumer, ".npm-cache") },
    stdio: "pipe",
  });
  if (result.error !== undefined) {
    throw result.error;
  }
  if (result.status !== 0) {
    process.stderr.write(result.stdout);
    process.stderr.write(result.stderr);
    throw new Error(`${command} failed with exit status ${result.status ?? 1}`);
  }
}

export function verificationSource() {
  return `
import coreManifest from "@kensa/core/build-manifest.json" with { type: "json" };
import sdkManifest from "@kensa/sdk/build-manifest.json" with { type: "json" };
import { startCase } from "@kensa/core";
import { runEvaluation } from "@kensa/sdk";

const trace = {
  spans: [],
  agent_runs: [],
  tools: [],
  tool_calls: [],
  incomplete: false,
  incomplete_reason: null,
  duration_ms: 0,
  cost_usd: null,
  known_cost_usd: null,
  cost_available: false,
  llm_turns: 0,
};
const result = await runEvaluation({
  case: { id: "package-check", input: null, metadata: {} },
  observe: () => ({ output: "ok", output_recorded: true, trace, failure: null }),
  check: () => ({ id: "package", outcome: "satisfied", failure: null }),
});

if (startCase({ id: "core-check", input: null, metadata: {} }).case.id !== "core-check") {
  throw new Error("@kensa/core evaluation export failed");
}
if (result.verdict !== "pass") {
  throw new Error("@kensa/sdk evaluation failed");
}
if (!import.meta.resolve("@kensa/sdk/vitest").endsWith("/dist/vitest.js")) {
  throw new Error("@kensa/sdk/vitest export is unavailable");
}
if (coreManifest.digest !== sdkManifest.digest) {
  throw new Error("package build manifests do not match");
}
`;
}

/* v8 ignore next 3 -- exercised through subprocess tests */
if (process.argv[1] === fileURLToPath(import.meta.url)) {
  process.exitCode = main(
    process.argv.slice(2),
    dependencies,
    process.stderr,
    process.stdout,
  );
}
