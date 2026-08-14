import {
  chmodSync,
  mkdtempSync,
  readdirSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import { afterEach, describe, expect, test, vi } from "vitest";

import {
  main,
  npmExecutable,
  packageTarball,
  run,
  verificationSource,
  verifyNpmPackages,
} from "./verify-npm-packages.mjs";

const roots = [];
const script = join(
  dirname(fileURLToPath(import.meta.url)),
  "verify-npm-packages.mjs",
);

afterEach(() => {
  for (const root of roots.splice(0)) {
    rmSync(root, { recursive: true, force: true });
  }
});

describe("npm package verifier", () => {
  test("requires exactly one package tarball", () => {
    expect(() => packageTarball("/packages", "kensa-core-", () => [])).toThrow(
      "expected one kensa-core- package in /packages, found 0",
    );
    expect(
      packageTarball("/packages", "kensa-core-", () => [
        "notes.txt",
        "kensa-sdk-1.0.0.tgz",
        "kensa-core-1.0.0.tgz",
      ]),
    ).toBe(join("/packages", "kensa-core-1.0.0.tgz"));
  });

  test("selects the configured npm executable", () => {
    expect(npmExecutable({ KENSA_NPM_BINARY: "/tools/npm" }, "linux")).toBe(
      "/tools/npm",
    );
    expect(npmExecutable({}, "win32")).toBe("npm.cmd");
    expect(npmExecutable({}, "linux")).toBe("npm");
  });

  test("writes and verifies an isolated consumer", () => {
    const root = temporaryRoot("kensa-package-verifier-unit-");
    writeFileSync(join(root, "kensa-core-1.0.0.tgz"), "core");
    writeFileSync(join(root, "kensa-sdk-1.0.0.tgz"), "sdk");
    const runCommand = vi.fn();
    const writeFile = vi.fn();
    const removeDirectory = vi.fn();
    const stdout = { write: vi.fn() };

    verifyNpmPackages(
      root,
      operations({ runCommand, writeFile, removeDirectory }),
      stdout,
    );

    expect(runCommand).toHaveBeenCalledTimes(3);
    expect(runCommand.mock.calls[0][0]).toBe("test-npm");
    expect(runCommand.mock.calls[0][1]).toContain(
      join(root, "kensa-core-1.0.0.tgz"),
    );
    expect(runCommand.mock.calls[1][1]).toContain(
      join(root, "kensa-sdk-1.0.0.tgz"),
    );
    expect(runCommand.mock.calls[2][0]).toBe(process.execPath);
    expect(writeFile).toHaveBeenCalledTimes(2);
    expect(writeFile.mock.calls[1][1]).toContain(
      'import { runEvaluation } from "@kensa/sdk";',
    );
    expect(removeDirectory).toHaveBeenCalledWith("/temporary/consumer", {
      recursive: true,
      force: true,
    });
    expect(stdout.write).toHaveBeenCalledWith(
      "verified kensa-core-1.0.0.tgz and kensa-sdk-1.0.0.tgz as an external consumer\n",
    );
  });

  test("removes the consumer when verification fails", () => {
    const root = temporaryRoot("kensa-package-verifier-unit-");
    writeFileSync(join(root, "kensa-core-1.0.0.tgz"), "core");
    writeFileSync(join(root, "kensa-sdk-1.0.0.tgz"), "sdk");
    const failure = new Error("install failed");
    const removeDirectory = vi.fn();

    expect(() =>
      verifyNpmPackages(
        root,
        operations({
          removeDirectory,
          runCommand: () => {
            throw failure;
          },
        }),
        { write: vi.fn() },
      ),
    ).toThrow(failure);
    expect(removeDirectory).toHaveBeenCalledWith("/temporary/consumer", {
      recursive: true,
      force: true,
    });
  });

  test("returns a usage error when the package directory is missing", () => {
    const stderr = { write: vi.fn() };

    expect(main([], operations(), stderr, { write: vi.fn() })).toBe(2);
    expect(stderr.write).toHaveBeenCalledWith(
      "usage: verify-npm-packages.mjs <package-directory>\n",
    );
  });

  test("runs verification from the command entrypoint", () => {
    const root = temporaryRoot("kensa-package-verifier-unit-");
    writeFileSync(join(root, "kensa-core-1.0.0.tgz"), "core");
    writeFileSync(join(root, "kensa-sdk-1.0.0.tgz"), "sdk");

    expect(
      main([root], operations(), { write: vi.fn() }, { write: vi.fn() }),
    ).toBe(0);
  });

  test("propagates command launch and exit failures", () => {
    expect(run(process.execPath, ["--eval", ""], tmpdir())).toBeUndefined();

    expect(() => run("missing-kensa-executable", [], tmpdir())).toThrow();
    expect(() =>
      run(process.execPath, ["--eval", "process.exitCode = 7"], tmpdir()),
    ).toThrow(`${process.execPath} failed with exit status 7`);
    expect(() =>
      run(
        process.execPath,
        ["--eval", "process.kill(process.pid, 'SIGTERM')"],
        tmpdir(),
      ),
    ).toThrow(`${process.execPath} failed with exit status 1`);
  });

  test("generates the external consumer assertions", () => {
    const source = verificationSource();

    expect(source).toContain('import { startCase } from "@kensa/core";');
    expect(source).toContain('import { runEvaluation } from "@kensa/sdk";');
    expect(source).toContain('import.meta.resolve("@kensa/sdk/vitest")');
    expect(source).toContain("coreManifest.digest !== sdkManifest.digest");
  });

  test.each([0, 2])(
    "rejects %i core package tarballs as a subprocess",
    (count) => {
      const root = temporaryRoot("kensa-package-verifier-cli-");
      for (let index = 0; index < count; index += 1) {
        writeFileSync(join(root, `kensa-core-${index}.tgz`), "core");
      }
      writeFileSync(join(root, "kensa-sdk-1.0.0.tgz"), "sdk");

      const result = spawnSync(process.execPath, [script, root], {
        encoding: "utf8",
      });

      expect(result.status).not.toBe(0);
      expect(result.stderr).toContain("expected one kensa-core- package");
    },
  );

  test("cleans up the subprocess consumer after an install failure", () => {
    const root = temporaryRoot("kensa-package-verifier-cli-");
    writeFileSync(join(root, "kensa-core-1.0.0.tgz"), "core");
    writeFileSync(join(root, "kensa-sdk-1.0.0.tgz"), "sdk");
    const npmStub = failingNpm(root);
    const before = consumerDirectories();

    const result = spawnSync(process.execPath, [script, root], {
      encoding: "utf8",
      env: { ...process.env, KENSA_NPM_BINARY: npmStub },
    });

    expect(result.status).not.toBe(0);
    expect(consumerDirectories()).toEqual(before);
  });
});

function temporaryRoot(prefix) {
  const root = mkdtempSync(join(tmpdir(), prefix));
  roots.push(root);
  return root;
}

function operations(overrides = {}) {
  return {
    makeTemporaryDirectory: () => "/temporary/consumer",
    nodeExecutable: process.execPath,
    npmExecutable: "test-npm",
    readDirectory: readdirSync,
    removeDirectory: vi.fn(),
    runCommand: vi.fn(),
    temporaryDirectory: "/temporary",
    writeFile: vi.fn(),
    ...overrides,
  };
}

function consumerDirectories() {
  return readdirSync(tmpdir())
    .filter((name) => name.startsWith("kensa-npm-consumer-"))
    .sort();
}

function failingNpm(root) {
  if (process.platform === "win32") {
    const stub = join(root, "npm-stub.cmd");
    writeFileSync(stub, "@echo off\r\nexit /b 23\r\n");
    return stub;
  }

  const stub = join(root, "npm-stub");
  writeFileSync(stub, "#!/bin/sh\nexit 23\n");
  chmodSync(stub, 0o755);
  return stub;
}
