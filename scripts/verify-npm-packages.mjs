import { mkdtempSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, join, resolve } from "node:path";
import { spawnSync } from "node:child_process";

const packageDirectory = process.argv[2];
if (packageDirectory === undefined) {
  process.stderr.write("usage: verify-npm-packages.mjs <package-directory>\n");
  process.exit(2);
}

const directory = resolve(packageDirectory);
const core = packageTarball(directory, "kensa-core-");
const sdk = packageTarball(directory, "kensa-sdk-");
const consumer = mkdtempSync(join(tmpdir(), "kensa-npm-consumer-"));

try {
  writeFileSync(
    join(consumer, "package.json"),
    `${JSON.stringify({ name: "kensa-package-verification", private: true, type: "module" })}\n`,
  );
  const installArguments = [
    "install",
    "--ignore-scripts",
    "--no-audit",
    "--no-fund",
  ];
  run(npmExecutable(), [...installArguments, core]);
  run(npmExecutable(), [...installArguments, sdk]);
  writeFileSync(join(consumer, "verify.mjs"), verificationSource());
  run(process.execPath, ["verify.mjs"]);
} finally {
  rmSync(consumer, { recursive: true, force: true });
}

process.stdout.write(
  `verified ${basename(core)} and ${basename(sdk)} as an external consumer\n`,
);

function packageTarball(directory, prefix) {
  const matches = readdirSync(directory)
    .filter((name) => name.startsWith(prefix) && name.endsWith(".tgz"))
    .map((name) => join(directory, name));
  if (matches.length !== 1) {
    throw new Error(
      `expected one ${prefix} package in ${directory}, found ${matches.length}`,
    );
  }
  return matches[0];
}

function npmExecutable() {
  return (
    process.env.KENSA_NPM_BINARY ??
    (process.platform === "win32" ? "npm.cmd" : "npm")
  );
}

function run(command, arguments_) {
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

function verificationSource() {
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
