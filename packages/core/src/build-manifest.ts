import { z } from "zod";

import { KensaCoreError, parseInput } from "./errors.js";
import { canonicalJson, digestJson } from "./json.js";

const nonblankSchema = z.string().trim().min(1);
const digestSchema = z.string().regex(/^[0-9a-f]{64}$/);
const identitySchema = z.strictObject({
  id: nonblankSchema,
  digest: digestSchema,
});

function componentSchema<Name extends string>(name: Name) {
  return z.strictObject({
    name: z.literal(name),
    version: nonblankSchema,
    digest: digestSchema,
  });
}

const componentsSchema = z.strictObject({
  core: componentSchema("@kensa/core"),
  engine: componentSchema("kensa-engine"),
  sdks: z.strictObject({
    python: componentSchema("kensa"),
    typescript: componentSchema("@kensa/sdk"),
  }),
});
const releaseManifestDraftSchema = z.strictObject({
  schema_version: z.literal("kensa.build_manifest.v1"),
  release: nonblankSchema,
  components: componentsSchema,
  contracts: z.array(identitySchema).min(1),
  schemas: z.array(identitySchema).min(1),
  conformance: z.array(identitySchema).min(1),
});
const releaseManifestSchema = releaseManifestDraftSchema.extend({
  contract_digest: digestSchema,
  digest: digestSchema,
});

export type BuildIdentity = z.infer<typeof identitySchema>;
export type BuildComponents = z.infer<typeof componentsSchema>;
export type ReleaseManifestDraft = z.infer<typeof releaseManifestDraftSchema>;
export type ReleaseManifest = z.infer<typeof releaseManifestSchema>;

export async function buildReleaseManifest(
  input: unknown,
): Promise<ReleaseManifest> {
  const parsed = parseInput(
    releaseManifestDraftSchema,
    input,
    "release build manifest violates the core contract",
  );
  const canonical = {
    ...parsed,
    contracts: canonicalIdentities(parsed.contracts, "contracts"),
    schemas: canonicalIdentities(parsed.schemas, "schemas"),
    conformance: canonicalIdentities(parsed.conformance, "conformance"),
  };
  const contractDigest = await digestJson({
    contracts: canonical.contracts,
    schemas: canonical.schemas,
  });
  const withoutDigest = {
    ...canonical,
    contract_digest: contractDigest,
  };
  return { ...withoutDigest, digest: await digestJson(withoutDigest) };
}

export async function verifyReleaseManifest(
  input: unknown,
): Promise<ReleaseManifest> {
  const parsed = parseInput(
    releaseManifestSchema,
    input,
    "release build manifest violates the core contract",
  );
  const expected = await buildReleaseManifest({
    schema_version: parsed.schema_version,
    release: parsed.release,
    components: parsed.components,
    contracts: parsed.contracts,
    schemas: parsed.schemas,
    conformance: parsed.conformance,
  });
  if (canonicalJson(input) !== canonicalJson(expected)) {
    throw new KensaCoreError(
      "invalid_input",
      "release build manifest is not canonical",
    );
  }
  return expected;
}

function canonicalIdentities(
  identities: BuildIdentity[],
  label: string,
): BuildIdentity[] {
  const ordered = [...identities].sort((left, right) =>
    compareText(left.id, right.id),
  );
  for (let index = 1; index < ordered.length; index += 1) {
    if (ordered[index - 1]!.id === ordered[index]!.id) {
      throw new KensaCoreError(
        "invalid_input",
        `${label} contains duplicate identity ${ordered[index]!.id}`,
      );
    }
  }
  return ordered;
}

function compareText(left: string, right: string): number {
  return Number(left > right) - Number(left < right);
}
