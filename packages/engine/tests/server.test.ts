import { PassThrough, Writable } from "node:stream";

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

  it("rejects when the protocol input fails", async () => {
    const input = new PassThrough();
    const output = new PassThrough();
    const completion = runEngine(input, output);

    input.destroy(new Error("input disconnected"));

    await expect(completion).rejects.toThrow("input disconnected");
  });

  it("rejects when flushing an output frame fails", async () => {
    const input = new PassThrough();
    const output = new Writable({
      write(_chunk, _encoding, callback) {
        callback(new Error("flush failed"));
      },
    });
    const completion = runEngine(input, output);
    input.end(
      JSON.stringify({
        id: "request-1",
        request: {
          type: "handshake",
          protocol_version: PROTOCOL_VERSION,
          client: "test",
        },
      }),
    );

    await expect(completion).rejects.toThrow("flush failed");
  });

  it("waits for each output frame to flush before processing the next", async () => {
    const input = new PassThrough();
    const writes: string[] = [];
    let activeWrites = 0;
    let maximumActiveWrites = 0;
    const output = new Writable({
      highWaterMark: 1,
      write(chunk: Buffer, _encoding, callback) {
        activeWrites += 1;
        maximumActiveWrites = Math.max(maximumActiveWrites, activeWrites);
        setImmediate(() => {
          writes.push(chunk.toString());
          activeWrites -= 1;
          callback();
        });
      },
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
          request: { type: "reset" },
        }),
      ].join("\n"),
    );

    await completion;

    expect(writes).toHaveLength(2);
    expect(maximumActiveWrites).toBe(1);
  });
});
