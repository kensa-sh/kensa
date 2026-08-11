import { PassThrough } from "node:stream";

import { describe, expect, it } from "vitest";

import { PROTOCOL_VERSION, runEngine } from "../src/index.js";

describe("runEngine", () => {
  it("writes one framed response for each framed request", async () => {
    const input = new PassThrough();
    const output = new PassThrough();
    let body = "";
    output.setEncoding("utf8");
    output.on("data", (chunk: string) => {
      body += chunk;
    });
    const complete = new Promise<void>((resolve) => output.on("end", resolve));

    runEngine(input, output);
    input.end(
      `${JSON.stringify({
        id: "request-1",
        request: { type: "handshake", protocol_version: PROTOCOL_VERSION, client: "test" },
      })}\n`,
    );
    input.on("close", () => output.end());
    await complete;

    expect(body.trim().split("\n")).toHaveLength(1);
    expect(JSON.parse(body)).toMatchObject({ id: "request-1", ok: true });
  });
});
