import { createInterface } from "node:readline";

import { KensaEngine } from "./engine.js";

export function runEngine(
  input: NodeJS.ReadableStream,
  output: NodeJS.WritableStream,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const engine = new KensaEngine();
    const lines = createInterface({
      input,
      crlfDelay: Number.POSITIVE_INFINITY,
    });
    const fail = (error: Error): void => {
      reject(error);
      lines.close();
    };
    output.once("error", fail);
    lines.on("line", (line) => {
      output.write(`${JSON.stringify(engine.processLine(line))}\n`);
    });
    lines.once("close", () => {
      output.off("error", fail);
      resolve();
    });
  });
}
