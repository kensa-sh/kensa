#!/usr/bin/env node

import { createInterface } from "node:readline";

import { KensaEngine } from "./engine.js";

export function runEngine(
  input: NodeJS.ReadableStream,
  output: NodeJS.WritableStream,
): void {
  const engine = new KensaEngine();
  const lines = createInterface({ input, crlfDelay: Number.POSITIVE_INFINITY });
  lines.on("line", (line) => {
    output.write(`${JSON.stringify(engine.processLine(line))}\n`);
  });
}

/* v8 ignore start -- executable wiring is covered by Python black-box tests */
if (process.argv[1] === new URL(import.meta.url).pathname) {
  runEngine(process.stdin, process.stdout);
}
/* v8 ignore stop */
