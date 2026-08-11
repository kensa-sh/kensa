import { readFile, readdir } from "node:fs/promises";

const sourceDirectory = new URL("../../packages/core/src/", import.meta.url);
const forbiddenImports = [];
const forbiddenGlobals = [];

async function sourceFiles(directory) {
  const files = [];
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const url = new URL(
      entry.name + (entry.isDirectory() ? "/" : ""),
      directory,
    );
    if (entry.isDirectory()) {
      files.push(...(await sourceFiles(url)));
    } else if (entry.name.endsWith(".ts")) {
      files.push(url);
    }
  }
  return files;
}

for (const file of await sourceFiles(sourceDirectory)) {
  const name = file.href.slice(sourceDirectory.href.length);
  const source = await readFile(file, "utf8");
  for (const match of source.matchAll(/from\s+["']([^"']+)["']/g)) {
    const specifier = match[1];
    if (specifier !== "zod" && !specifier.startsWith(".")) {
      forbiddenImports.push(`${name}: ${specifier}`);
    }
  }
  for (const name of ["Buffer", "Bun", "Deno", "process"]) {
    if (new RegExp(`\\b${name}\\b`).test(source)) {
      forbiddenGlobals.push(
        `${file.href.slice(sourceDirectory.href.length)}: ${name}`,
      );
    }
  }
}

if (forbiddenImports.length > 0 || forbiddenGlobals.length > 0) {
  throw new Error(
    `@kensa/core contains platform-specific dependencies:\n${[
      ...forbiddenImports,
      ...forbiddenGlobals,
    ].join("\n")}`,
  );
}

const originalCrypto = globalThis.crypto;
const originalTextEncoder = globalThis.TextEncoder;
Object.defineProperty(globalThis, "crypto", {
  configurable: true,
  value: undefined,
});
Object.defineProperty(globalThis, "TextEncoder", {
  configurable: true,
  value: undefined,
});

try {
  const core = await import("../../packages/core/dist/index.js");
  if (core.canonicalJson({ b: 2, a: 1 }) !== '{"a":1,"b":2}') {
    throw new Error("built core failed without platform globals");
  }
  await core.digestJson({ safe: true }).then(
    () => {
      throw new Error("digest unexpectedly succeeded without platform globals");
    },
    (error) => {
      if (error?.code !== "unsupported_platform") {
        throw error;
      }
    },
  );
} finally {
  Object.defineProperty(globalThis, "crypto", {
    configurable: true,
    value: originalCrypto,
  });
  Object.defineProperty(globalThis, "TextEncoder", {
    configurable: true,
    value: originalTextEncoder,
  });
}
