import { PassThrough } from "node:stream";

import { describe, expect, it } from "vitest";

import { PROTOCOL_VERSION, runEngine } from "../src/index.js";

describe("runEngine", () => {
  it("processes multiple frames and a final unterminated frame before clean shutdown", async () => {
    const input = new PassThrough();
    const output = new PassThrough();
    let body = "";
    output.setEncoding("utf8");
    output.on("data", (chunk: string) => {
      body += chunk;
    });

    const completion = runEngine(input, output);
    input.end(
      [
        JSON.stringify({
          id: "request-1",
          request: {
            type: "handshake",
            protocol_version: PROTOCOL_VERSION,
            client: "test",
          },
        }),
        JSON.stringify({
          id: "request-2",
          request: {
            type: "start_case",
            evaluation_id: "eval-1",
            case: { id: "case-1", input: null, metadata: {} },
          },
        }),
      ].join("\n"),
    );
    await completion;

    const frames = body
      .trim()
      .split("\n")
      .map((line) => JSON.parse(line));
    expect(frames).toHaveLength(2);
    expect(frames[0]).toMatchObject({ id: "request-1", ok: true });
    expect(frames[1]).toMatchObject({ id: "request-2", ok: true });
  });

  it("rejects and closes its reader when the protocol output fails", async () => {
    const input = new PassThrough();
    const output = new PassThrough();
    const completion = runEngine(input, output);

    output.destroy(new Error("parent disconnected"));

    await expect(completion).rejects.toThrow("parent disconnected");
  });
});
