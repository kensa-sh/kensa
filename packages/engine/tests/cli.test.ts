import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const runEngine = vi.hoisted(() => vi.fn());

vi.mock("../src/server.js", () => ({ runEngine }));

describe("CLI entrypoint", () => {
  beforeEach(() => {
    vi.resetModules();
    runEngine.mockReset();
    process.exitCode = undefined;
  });

  afterEach(() => {
    vi.restoreAllMocks();
    process.exitCode = undefined;
  });

  it("starts the engine with process stdio", async () => {
    runEngine.mockResolvedValue(undefined);

    await import("../src/cli.js");

    expect(runEngine).toHaveBeenCalledWith(process.stdin, process.stdout);
  });

  it.each([
    [new Error("stream failed"), "stream failed"],
    ["failed", "unknown engine failure"],
  ])(
    "reports startup and stream failures on stderr",
    async (error, expected) => {
      runEngine.mockRejectedValue(error);
      const write = vi
        .spyOn(process.stderr, "write")
        .mockImplementation(() => true);

      await import("../src/cli.js");
      await Promise.resolve();

      expect(write).toHaveBeenCalledWith(`kensa-engine: ${expected}\n`);
      expect(process.exitCode).toBe(1);
    },
  );
});
