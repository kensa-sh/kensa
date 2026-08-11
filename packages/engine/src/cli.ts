#!/usr/bin/env node

import { runEngine } from "./server.js";

void runEngine(process.stdin, process.stdout).catch((error: unknown) => {
  const message =
    error instanceof Error ? error.message : "unknown engine failure";
  process.stderr.write(`kensa-engine: ${message}\n`);
  process.exitCode = 1;
});
