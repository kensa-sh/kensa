import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const packageFiles = [
  "packages/core/package.json",
  "packages/engine/package.json",
  "sdk/typescript/package.json",
];
const check = process.argv[2] === "--check";
const version = process.argv[check ? 3 : 2];
if (
  version === undefined ||
  !/^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/.test(version)
) {
  process.stderr.write(
    "usage: set-typescript-version.mjs [--check] <major.minor.patch>\n",
  );
  process.exit(2);
}

for (const relativePath of packageFiles) {
  const path = join(root, relativePath);
  const value = JSON.parse(readFileSync(path, "utf8"));
  if (check) {
    if (value.version !== version) {
      process.stderr.write(
        `${relativePath} is ${value.version}; expected ${version}\n`,
      );
      process.exitCode = 1;
    }
  } else {
    value.version = version;
    writeFileSync(path, `${JSON.stringify(value, null, 2)}\n`);
  }
}
