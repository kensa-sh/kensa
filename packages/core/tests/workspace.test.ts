import { globSync, readFileSync, readdirSync } from "node:fs";
import { describe, expect, it } from "vitest";

const root = new URL("../../../", import.meta.url);
const read = (path: string) => readFileSync(new URL(path, root), "utf8");

describe("TypeScript workspace", () => {
  it("contains exactly one active package and empty reserved paths", () => {
    expect(
      globSync(
        [
          "packages/*/package.json",
          "sdks/typescript/packages/*/package.json",
          "web/package.json",
        ],
        { cwd: root.pathname },
      ).sort(),
    ).toEqual(["packages/core/package.json"]);
    for (const directory of [
      "packages/cli",
      "packages/server",
      "sdks/python",
      "sdks/typescript/packages/sdk",
      "sdks/typescript/packages/vitest",
      "web",
    ])
      expect(readdirSync(new URL(`${directory}/`, root))).toEqual([".gitkeep"]);
  });

  it("pins the required workspace tools and strict compiler options", () => {
    const rootPackage = JSON.parse(read("package.json")) as {
      packageManager: string;
      engines: { node: string };
      scripts: Readonly<Record<string, string>>;
    };
    expect(rootPackage.packageManager).toBe("pnpm@10.28.2");
    expect(rootPackage.engines.node).toBe(">=24");
    expect(read(".nvmrc")).toBe("24\n");
    for (const script of [
      "build",
      "format:check",
      "lint",
      "schema:check",
      "test",
      "test:coverage",
      "typecheck",
    ])
      expect(rootPackage.scripts).toHaveProperty(script);
    const tsconfig = JSON.parse(read("tsconfig.base.json")) as {
      compilerOptions: Readonly<Record<string, unknown>>;
    };
    for (const option of [
      "strict",
      "noUncheckedIndexedAccess",
      "exactOptionalPropertyTypes",
      "noImplicitOverride",
      "noFallthroughCasesInSwitch",
      "noImplicitReturns",
      "useUnknownInCatchVariables",
      "verbatimModuleSyntax",
      "isolatedModules",
    ])
      expect(tsconfig.compilerOptions[option]).toBe(true);
    expect(tsconfig.compilerOptions.skipLibCheck).toBe(false);
  });

  it("keeps the platform core free of Node and future runtime behavior", () => {
    const sourceFiles = globSync("packages/core/src/**/*.ts", {
      cwd: root.pathname,
    });
    const source = sourceFiles.map(read).join("\n");
    expect(source).not.toMatch(/from ["']node:/);
    expect(source).not.toMatch(/duckdb|cloudflare|child_process/i);
  });

  it("adds every TypeScript gate while preserving Python CI", () => {
    const workflow = read(".github/workflows/ci.yml");
    for (const command of [
      "pnpm install --frozen-lockfile",
      "pnpm format:check",
      "pnpm lint",
      "pnpm typecheck",
      "pnpm test:coverage",
      "pnpm build",
      "pnpm schema:check",
      'coverage run -m pytest -q -m "not live"',
      "uv run ruff check .",
      "uv run ruff format --check .",
      "uv run ty check",
      "uv build",
    ])
      expect(workflow).toContain(command);
    const dependabot = read(".github/dependabot.yml");
    expect(dependabot).toContain('package-ecosystem: "npm"');
    expect(dependabot).toContain('directory: "/"');
    const pullRequestTemplate = read(".github/PULL_REQUEST_TEMPLATE.md");
    for (const command of [
      "pnpm format:check",
      "pnpm lint",
      "pnpm typecheck",
      "pnpm test:coverage",
      "pnpm build",
    ])
      expect(pullRequestTemplate).toContain(command);
  });

  it("retains the active Python package paths", () => {
    expect(read("pyproject.toml")).toContain('name = "kensa"');
    expect(
      globSync("src/kensa/**/*.py", { cwd: root.pathname }).length,
    ).toBeGreaterThan(0);
    expect(
      globSync("tests/**/*.py", { cwd: root.pathname }).length,
    ).toBeGreaterThan(0);
    expect(read("uv.lock").length).toBeGreaterThan(0);
  });
});
