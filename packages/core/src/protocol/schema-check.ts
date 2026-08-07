import { generateProtocolSchemas } from "./schemas.js";
import { writeFile } from "node:fs/promises";

const root = new URL("../../../../schemas/v1/", import.meta.url);
for (const [name, schema] of Object.entries(generateProtocolSchemas()))
  await writeFile(
    new URL(`${name}.schema.json`, root),
    `${JSON.stringify(schema, null, 2)}\n`,
  );
