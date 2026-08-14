import { build } from "esbuild";
import { fileURLToPath } from "node:url";

export default async function buildExecutable(): Promise<void> {
  await build({
    bundle: true,
    entryPoints: [fileURLToPath(new URL("../src/cli.ts", import.meta.url))],
    format: "esm",
    outfile: fileURLToPath(new URL("../dist/cli.js", import.meta.url)),
    platform: "node",
    target: "node22",
  });
}
