import { spawn } from "node:child_process";
import { once } from "node:events";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import {
  PROTOCOL_VERSION,
  responseEnvelopeSchema,
  type ResponseEnvelope,
} from "../src/index.js";

describe("bundled executable", () => {
  it("survives bad frames, completes work, and shuts down cleanly", async () => {
    const executable = fileURLToPath(
      new URL("../dist/cli.js", import.meta.url),
    );
    const child = spawn(process.execPath, [executable], {
      stdio: ["pipe", "pipe", "pipe"],
    });
    const frames: ResponseEnvelope[] = [];
    const readers: Array<(frame: ResponseEnvelope) => void> = [];
    const stderr: Buffer[] = [];
    let pending = "";
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk: string) => {
      pending += chunk;
      const lines = pending.split("\n");
      pending = lines.pop() ?? "";
      for (const line of lines) {
        const frame = responseEnvelopeSchema.parse(JSON.parse(line));
        const reader = readers.shift();
        if (reader === undefined) {
          frames.push(frame);
        } else {
          reader(frame);
        }
      }
    });
    child.stderr.on("data", (chunk: Buffer) => stderr.push(chunk));

    const exchange = async (request: string): Promise<ResponseEnvelope> => {
      const response = new Promise<ResponseEnvelope>((resolve) => {
        const frame = frames.shift();
        if (frame === undefined) {
          readers.push(resolve);
        } else {
          resolve(frame);
        }
      });
      child.stdin.write(`${request}\n`);
      return response;
    };

    await expect(exchange("{")).resolves.toMatchObject({
      id: null,
      ok: false,
      failure: { code: "invalid_message" },
    });
    await expect(
      exchange(
        JSON.stringify({
          id: "bad-version",
          request: {
            type: "handshake",
            protocol_version: "future",
            client: "black-box-test",
          },
        }),
      ),
    ).resolves.toMatchObject({
      id: "bad-version",
      ok: false,
      failure: { code: "version_mismatch" },
    });
    expect(child.exitCode).toBeNull();
    await expect(
      exchange(
        JSON.stringify({
          id: "handshake",
          request: {
            type: "handshake",
            protocol_version: PROTOCOL_VERSION,
            client: "black-box-test",
          },
        }),
      ),
    ).resolves.toMatchObject({
      id: "handshake",
      ok: true,
      response: { type: "handshake" },
    });
    await expect(
      exchange(
        JSON.stringify({
          id: "start",
          request: {
            type: "start_case",
            evaluation_id: "black-box-eval",
            case: { id: "case-1", input: null, metadata: {} },
          },
        }),
      ),
    ).resolves.toMatchObject({
      id: "start",
      ok: true,
      response: { type: "action", action: "invoke_agent" },
    });
    await expect(
      exchange(
        JSON.stringify({
          id: "cancel",
          request: {
            type: "cancel",
            evaluation_id: "black-box-eval",
            reason: "stopped",
          },
        }),
      ),
    ).resolves.toMatchObject({
      id: "cancel",
      ok: true,
      response: { type: "result", evaluation: { phase: "cancelled" } },
    });
    child.stdin.end();
    const [exitCode] = await once(child, "close");

    expect(exitCode).toBe(0);
    expect(pending).toBe("");
    expect(readers).toHaveLength(0);
    expect(frames).toHaveLength(0);
    expect(Buffer.concat(stderr).toString()).toBe("");
  });
});
