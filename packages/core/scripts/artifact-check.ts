import { readFile, writeFile } from "node:fs/promises";
import { canonicalProtocolJson } from "../src/protocol/canonical.js";
import { canonicalJson } from "../src/protocol/canonical-json.js";
import { generateProtocolSchemas } from "../src/protocol/schemas.js";

type FixtureEntry = Readonly<{ path: string }>;
type FixtureManifest = Readonly<{ fixtures: readonly FixtureEntry[] }>;

const schemaRoot = new URL("../../../schemas/v1/", import.meta.url);
const fixtureRoot = new URL(
  "../../../fixtures/conformance/v1/",
  import.meta.url,
);
const write = process.argv.includes("--write");

async function checkArtifact(target: URL, expected: string): Promise<void> {
  if (write) {
    await writeFile(target, expected);
    return;
  }
  if ((await readFile(target, "utf8")) !== expected)
    throw new Error(`Artifact drift: ${target.pathname}`);
}

for (const [name, schema] of Object.entries(generateProtocolSchemas()))
  await checkArtifact(
    new URL(`${name}.schema.json`, schemaRoot),
    canonicalJson(schema),
  );

const manifestTarget = new URL("manifest.json", fixtureRoot);
const manifestValue = JSON.parse(
  await readFile(manifestTarget, "utf8"),
) as FixtureManifest;
await checkArtifact(manifestTarget, canonicalJson(manifestValue));

for (const fixture of manifestValue.fixtures) {
  const target = new URL(fixture.path, fixtureRoot);
  if (fixture.path.startsWith("valid/")) {
    const value = JSON.parse(await readFile(target, "utf8")) as unknown;
    await checkArtifact(target, canonicalProtocolJson(value));
  }
}
