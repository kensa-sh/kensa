import { createInterface } from "node:readline";
import type { Readable, Writable } from "node:stream";

import { KensaEngine } from "./engine.js";

export function runEngine(input: Readable, output: Writable): Promise<void> {
  const engine = new KensaEngine();
  const lines = createInterface({
    input,
    crlfDelay: Number.POSITIVE_INFINITY,
  });

  let rejectInput!: (error: Error) => void;
  const inputFailure = new Promise<never>((_resolve, reject) => {
    rejectInput = reject;
  });
  let rejectOutput!: (error: Error) => void;
  const outputFailure = new Promise<never>((_resolve, reject) => {
    rejectOutput = reject;
  });
  const onInputError = (error: Error): void => {
    rejectInput(error);
    lines.close();
  };
  input.once("error", onInputError);
  const onOutputError = (error: Error): void => {
    rejectOutput(error);
    lines.close();
  };
  output.once("error", onOutputError);

  const consume = async (): Promise<void> => {
    for await (const line of lines) {
      await writeFrame(output, `${JSON.stringify(engine.processLine(line))}\n`);
    }
  };

  return Promise.race([consume(), inputFailure, outputFailure]).finally(() => {
    input.off("error", onInputError);
    output.off("error", onOutputError);
    lines.close();
    engine.reset();
  });
}

function writeFrame(output: Writable, frame: string): Promise<void> {
  return new Promise((resolve, reject) => {
    output.write(frame, (error?: Error | null) => {
      if (error) {
        reject(error);
      } else {
        resolve();
      }
    });
  });
}
