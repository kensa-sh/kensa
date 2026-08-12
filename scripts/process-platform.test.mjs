import { describe, expect, it } from "vitest";

import { requiresCommandShell } from "./process-platform.mjs";

describe("process platform handling", () => {
  it("uses a shell for Windows command shims", () => {
    expect(requiresCommandShell("pnpm.cmd", "win32")).toBe(true);
    expect(requiresCommandShell("build.BAT", "win32")).toBe(true);
  });

  it("spawns native Windows executables directly", () => {
    expect(requiresCommandShell("bun.exe", "win32")).toBe(false);
  });

  it("spawns commands directly on Unix", () => {
    expect(requiresCommandShell("pnpm.cmd", "linux")).toBe(false);
  });
});
