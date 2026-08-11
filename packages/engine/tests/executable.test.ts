import { spawn } from "node:child_process";
import { once } from "node:events";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { PROTOCOL_VERSION, responseEnvelopeSchema } from "../src/index.js";

describe("bundled executable", () => {
  it("serves the protocol over stdio and exits when stdin closes", async () => {
    const executable = fileURLToPath(
      new URL("../dist/cli.js", import.meta.url),
    );
    const child = spawn(process.execPath, [executable], {
      stdio: ["pipe", "pipe", "pipe"],
    });
    const stdout: Buffer[] = [];
    const stderr: Buffer[] = [];
    child.stdout.on("data", (chunk: Buffer) => stdout.push(chunk));
    child.stderr.on("data", (chunk: Buffer) => stderr.push(chunk));

    child.stdin.end(
      `${JSON.stringify({
        id: "request-1",
        request: {
          type: "handshake",
          protocol_version: PROTOCOL_VERSION,
          client: "black-box-test",
        },
      })}\n`,
    );
    const [exitCode] = await once(child, "close");

    expect(exitCode).toBe(0);
    expect(Buffer.concat(stderr).toString()).toBe("");
    const response = responseEnvelopeSchema.parse(
      JSON.parse(Buffer.concat(stdout).toString()),
    );
    expect(response).toMatchObject({
      id: "request-1",
      ok: true,
      response: { type: "handshake" },
    });
  });
});
