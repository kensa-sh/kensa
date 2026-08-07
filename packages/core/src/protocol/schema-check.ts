import { generateProtocolSchemas } from "./schemas.js";
import { readFile, writeFile } from "node:fs/promises";

const root = new URL("../../../../schemas/v1/", import.meta.url);
const write = process.argv.includes("--write");
for (const [name, schema] of Object.entries(generateProtocolSchemas())) {
  const expected = `${JSON.stringify(schema, null, 2)}\n`;
  const target = new URL(`${name}.schema.json`, root);
  if (write) await writeFile(target, expected);
  else if ((await readFile(target, "utf8")) !== expected)
    throw new Error(`Schema drift: ${name}.schema.json`);
}
