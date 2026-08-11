import { z } from "zod";

import { KensaCoreError, parseInput } from "./errors.js";
import {
  canonicalJson,
  digestJson,
  jsonValueSchema,
  type JsonValue,
} from "./json.js";
import {
  bindInspectCandidates,
  parseInspectQueue,
  type CandidateEvidence,
} from "./mining.js";

const slugSchema = z
  .string()
  .trim()
  .regex(/^[a-z0-9][a-z0-9-]{0,63}$/);
const nonblankSchema = z.string().trim().min(1);
const digestSchema = z.string().regex(/^[0-9a-f]{64}$/);
const repositoryPathSchema = nonblankSchema.refine(isRepositoryPath, {
  message: "path must be repository-relative without parent traversal",
});
const criterionSchema = z.strictObject({
  id: slugSchema,
  description: nonblankSchema,
  kind: z.enum(["assertion", "judge"]),
});
const candidateEvidenceSchema = z.strictObject({
  identity: z.strictObject({
    schema_version: z.literal("kensa.source_identity.v1"),
    kind: z.literal("trace"),
    provider: nonblankSchema,
    source_id: z.string().regex(/^trace_[0-9a-f]{24}$/),
    digest: digestSchema,
  }),
  record_digest: digestSchema,
});
const protectionCaseSchema = z.strictObject({
  id: slugSchema,
  input: jsonValueSchema,
  criteria: z.array(criterionSchema).min(1),
  source: z.strictObject({
    candidate_id: slugSchema,
    candidate_digest: digestSchema,
    evidence: z.array(candidateEvidenceSchema).min(1),
  }),
});
const protectionBindingsSchema = z.strictObject({
  eval: z.strictObject({
    framework: z.literal("pytest"),
    path: repositoryPathSchema.refine(
      (path) => /(?:^|\/)tests\/evals\/test_[^/]+\.py$/.test(path),
      { message: "pytest binding must name a tests/evals/test_*.py file" },
    ),
    entrypoint: nonblankSchema,
  }),
  workflow: z.strictObject({
    provider: z.literal("github-actions"),
    path: repositoryPathSchema.refine(
      (path) => /^\.github\/workflows\/[^/]+\.ya?ml$/.test(path),
      { message: "workflow path must name a root GitHub Actions YAML file" },
    ),
    job: nonblankSchema,
  }),
  application: z.strictObject({
    name: nonblankSchema,
    environment: nonblankSchema,
  }),
});
const protectionSuiteSchema = z.strictObject({
  schema_version: z.literal("kensa.protection.v1"),
  id: slugSchema,
  name: nonblankSchema,
  bindings: protectionBindingsSchema,
  cases: z.array(protectionCaseSchema).min(1),
  digest: digestSchema,
});
const caseDraftSchema = z.strictObject({
  candidate_id: slugSchema,
  input: jsonValueSchema,
  criteria: z.array(criterionSchema).min(1),
});
const bootstrapInputSchema = z.strictObject({
  id: slugSchema,
  name: nonblankSchema,
  queue: z.unknown(),
  evidence: z.unknown(),
  cases: z.array(caseDraftSchema).min(1),
  bindings: protectionBindingsSchema,
});

export type ProtectionCriterion = z.infer<typeof criterionSchema>;
export type ProtectionBindings = z.infer<typeof protectionBindingsSchema>;

export interface ProtectionCase {
  id: string;
  input: JsonValue;
  criteria: ProtectionCriterion[];
  source: {
    candidate_id: string;
    candidate_digest: string;
    evidence: CandidateEvidence[];
  };
}

export interface ProtectionSuite {
  schema_version: "kensa.protection.v1";
  id: string;
  name: string;
  bindings: ProtectionBindings;
  cases: ProtectionCase[];
  digest: string;
}

export async function bootstrapProtectionSuite(
  input: unknown,
): Promise<ProtectionSuite> {
  const parsed = parseInput(
    bootstrapInputSchema,
    input,
    "protection suite bootstrap violates the core contract",
  );
  const queue = parseInspectQueue(parsed.queue);
  const approvedItems = queue.items.filter(
    (item) => item.status === "approved",
  );
  if (approvedItems.length === 0) {
    throw new KensaCoreError(
      "invalid_input",
      "protection suite requires at least one approved inspect item",
    );
  }
  const candidates = await bindInspectCandidates(
    { ...queue, items: approvedItems },
    parsed.evidence,
  );
  const approved = candidates.candidates;
  const drafts = new Map<string, (typeof parsed.cases)[number]>();
  for (const draft of parsed.cases) {
    if (drafts.has(draft.candidate_id)) {
      throw new KensaCoreError(
        "invalid_input",
        `protection suite contains duplicate case ${draft.candidate_id}`,
      );
    }
    drafts.set(draft.candidate_id, draft);
  }
  const approvedIds = new Set(approved.map((candidate) => candidate.item.id));
  const unknown = [...drafts.keys()].filter((id) => !approvedIds.has(id));
  const missing = [...approvedIds].filter((id) => !drafts.has(id));
  if (unknown.length > 0 || missing.length > 0) {
    throw new KensaCoreError(
      "invalid_input",
      protectionCoverageMessage(unknown, missing),
    );
  }
  const cases = approved.map((candidate) => {
    const draft = drafts.get(candidate.item.id)!;
    return canonicalCase({
      id: candidate.item.id,
      input: draft.input,
      criteria: draft.criteria,
      source: {
        candidate_id: candidate.item.id,
        candidate_digest: candidate.digest,
        evidence: candidate.evidence,
      },
    });
  });
  cases.sort((left, right) => compareText(left.id, right.id));
  return suiteWithDigest({
    schema_version: "kensa.protection.v1",
    id: parsed.id,
    name: parsed.name,
    bindings: parsed.bindings,
    cases,
  });
}

export async function verifyProtectionSuite(
  input: unknown,
): Promise<ProtectionSuite> {
  const suite = parseInput(
    protectionSuiteSchema,
    input,
    "protection suite violates the core contract",
  );
  const caseIds = new Set<string>();
  const cases = suite.cases.map((item) => {
    if (caseIds.has(item.id)) {
      throw new KensaCoreError(
        "invalid_input",
        `protection suite contains duplicate case ${item.id}`,
      );
    }
    caseIds.add(item.id);
    if (item.id !== item.source.candidate_id) {
      throw new KensaCoreError(
        "invalid_input",
        `protection case ${item.id} contradicts its candidate source`,
      );
    }
    return canonicalCase(item);
  });
  cases.sort((left, right) => compareText(left.id, right.id));
  const expected = await suiteWithDigest({
    schema_version: "kensa.protection.v1",
    id: suite.id,
    name: suite.name,
    bindings: suite.bindings,
    cases,
  });
  if (canonicalJson(input) !== canonicalJson(expected)) {
    throw new KensaCoreError(
      "invalid_input",
      "protection suite is not canonical",
    );
  }
  return expected;
}

function canonicalCase(input: ProtectionCase): ProtectionCase {
  const criteria = [...input.criteria].sort((left, right) =>
    compareText(left.id, right.id),
  );
  rejectDuplicateIds(criteria, `protection case ${input.id} criteria`);
  const evidence = [...input.source.evidence].sort((left, right) =>
    compareText(left.identity.source_id, right.identity.source_id),
  );
  rejectDuplicateIds(
    evidence.map((item) => ({ id: item.identity.source_id })),
    `protection case ${input.id} evidence`,
  );
  return {
    ...input,
    criteria,
    source: { ...input.source, evidence },
  };
}

async function suiteWithDigest(
  value: Omit<ProtectionSuite, "digest">,
): Promise<ProtectionSuite> {
  return { ...value, digest: await digestJson(value) };
}

function rejectDuplicateIds(items: Array<{ id: string }>, label: string): void {
  const ids = new Set<string>();
  for (const item of items) {
    if (ids.has(item.id)) {
      throw new KensaCoreError(
        "invalid_input",
        `${label} contains duplicate ID ${item.id}`,
      );
    }
    ids.add(item.id);
  }
}

function protectionCoverageMessage(
  unknown: string[],
  missing: string[],
): string {
  const problems = [
    unknown.length > 0
      ? `unapproved cases: ${unknown.sort(compareText).join(", ")}`
      : null,
    missing.length > 0
      ? `missing approved cases: ${missing.sort(compareText).join(", ")}`
      : null,
  ].filter((problem): problem is string => problem !== null);
  return `protection suite coverage mismatch (${problems.join("; ")})`;
}

function isRepositoryPath(value: string): boolean {
  return (
    !value.startsWith("/") &&
    !value.includes("\\") &&
    !value.includes("\0") &&
    !value.split("/").some((segment) => segment === ".." || segment === "")
  );
}

function compareText(left: string, right: string): number {
  return Number(left > right) - Number(left < right);
}
