import { describe, expect, it } from "vitest";
import {
  caseIdSchema,
  checkResultIdSchema,
  evalRunIdSchema,
  invalidJsonPath,
  invocationIdSchema,
  isJsonObject,
  isJsonValue,
  nonBlankStringSchema,
  safeIntegerSchema,
  spanIdSchema,
  timestampSchema,
  traceIdSchema,
  u32Schema,
} from "../src/index.js";

describe("protocol primitives", () => {
  it.each([
    [evalRunIdSchema, "run_018f2f2e-8c70-7c31-8c70-123456789abc"],
    [invocationIdSchema, "inv_018f2f2e-8c70-7c31-8c70-123456789abc"],
    [checkResultIdSchema, "chk_018f2f2e-8c70-7c31-8c70-123456789abc"],
    [traceIdSchema, "0123456789abcdef0123456789abcdef"],
    [spanIdSchema, "0123456789abcdef"],
    [caseIdSchema, " checkout case "],
    [nonBlankStringSchema, " value "],
  ])("accepts a valid branded or nonblank string", (schema, value) => {
    expect(schema.safeParse(value).success).toBe(true);
  });

  it.each([
    [evalRunIdSchema, "run_018f2f2e-8c70-6c31-8c70-123456789abc"],
    [evalRunIdSchema, "run_018f2f2e-8c70-7c31-7c70-123456789abc"],
    [evalRunIdSchema, "RUN_018f2f2e-8c70-7c31-8c70-123456789abc"],
    [invocationIdSchema, "inv_bad"],
    [checkResultIdSchema, "chk_bad"],
    [traceIdSchema, "00000000000000000000000000000000"],
    [traceIdSchema, "0123456789ABCDEF0123456789ABCDEF"],
    [spanIdSchema, "0000000000000000"],
    [spanIdSchema, "0123456789ABCDEG"],
    [caseIdSchema, " \t\n "],
    [nonBlankStringSchema, ""],
  ])("rejects an invalid branded or blank string", (schema, value) => {
    expect(schema.safeParse(value).success).toBe(false);
  });

  it.each([
    "0001-01-01T00:00:00.000Z",
    "2000-02-29T23:59:59.999Z",
    "2024-02-29T01:02:03.004Z",
    "9999-12-31T23:59:59.999Z",
  ])("accepts canonical timestamps", (value) => {
    expect(timestampSchema.safeParse(value).success).toBe(true);
  });

  it.each([
    "0000-01-01T00:00:00.000Z",
    "2023-02-29T00:00:00.000Z",
    "1900-02-29T00:00:00.000Z",
    "2026-13-01T00:00:00.000Z",
    "2026-01-32T00:00:00.000Z",
    "2026-01-01T24:00:00.000Z",
    "2026-01-01T00:60:00.000Z",
    "2026-01-01T00:00:60.000Z",
    "2026-01-01t00:00:00.000Z",
    "2026-01-01T00:00:00.000z",
    "2026-01-01T00:00:00Z",
    "2026-01-01T00:00:00.00Z",
    "2026-01-01T00:00:00.0000Z",
    "2026-01-01T00:00:00.000+00:00",
    " 2026-01-01T00:00:00.000Z",
  ])("rejects invalid or noncanonical timestamps", (value) => {
    expect(timestampSchema.safeParse(value).success).toBe(false);
  });

  it.each([0, 1, Number.MAX_SAFE_INTEGER])(
    "accepts safe nonnegative integers",
    (value) => {
      expect(safeIntegerSchema.safeParse(value).success).toBe(true);
    },
  );

  it.each([-1, 1.5, Number.MAX_SAFE_INTEGER + 1, Infinity, NaN])(
    "rejects unsafe protocol integers",
    (value) => {
      expect(safeIntegerSchema.safeParse(value).success).toBe(false);
    },
  );

  it.each([0, 1, 4_294_967_295])("accepts U32 values", (value) => {
    expect(u32Schema.safeParse(value).success).toBe(true);
  });

  it.each([-1, 1.5, 4_294_967_296])("rejects values outside U32", (value) => {
    expect(u32Schema.safeParse(value).success).toBe(false);
  });

  it("accepts recursive JSON values and repeated references", () => {
    const shared = { value: 1 };
    const value = { array: [null, true, "x", 1.5, shared], shared };
    expect(isJsonValue(value)).toBe(true);
    expect(isJsonObject(value)).toBe(true);
    expect(isJsonObject([])).toBe(false);
    expect(invalidJsonPath(value)).toBeNull();
  });

  it("rejects every unsupported JavaScript value with its path", () => {
    const cycle: unknown[] = [];
    cycle.push(cycle);
    const sparse = Array<unknown>(1);
    const inherited = Object.create({ inherited: true }) as object;
    const symbolKey = { good: true };
    Object.defineProperty(symbolKey, Symbol("secret"), { value: true });
    const hidden = { good: true };
    Object.defineProperty(hidden, "hidden", { value: true });
    const accessor = {};
    Object.defineProperty(accessor, "value", {
      enumerable: true,
      get: () => 1,
    });
    const arrayProperty: unknown[] = [];
    Object.defineProperty(arrayProperty, "extra", { value: true });
    const outOfRangeArrayProperty: unknown[] = [true];
    Object.defineProperty(outOfRangeArrayProperty, "4294967295", {
      value: true,
      enumerable: true,
    });
    const arrayAccessor: unknown[] = [];
    Object.defineProperty(arrayAccessor, "0", {
      enumerable: true,
      get: () => {
        throw new Error("accessor must not run");
      },
    });
    const hiddenArrayItem: unknown[] = [];
    Object.defineProperty(hiddenArrayItem, "0", { value: true });
    const arraySymbol: unknown[] = [];
    Object.defineProperty(arraySymbol, Symbol("extra"), { value: true });
    class Value {}
    const cases: readonly [unknown, readonly (string | number)[]][] = [
      [undefined, []],
      [1n, []],
      [Symbol("value"), []],
      [() => true, []],
      [NaN, []],
      [Infinity, []],
      [Number.MAX_SAFE_INTEGER + 1, []],
      [cycle, [0]],
      [sparse, [0]],
      [new Date(), []],
      [new Map(), []],
      [new Set(), []],
      [new Value(), []],
      [inherited, []],
      [symbolKey, ["<symbol>"]],
      [hidden, ["hidden"]],
      [accessor, ["value"]],
      [arrayProperty, ["extra"]],
      [outOfRangeArrayProperty, ["4294967295"]],
      [arrayAccessor, [0]],
      [hiddenArrayItem, [0]],
      [arraySymbol, ["<symbol>"]],
      [{ nested: undefined }, ["nested"]],
    ];
    for (const [value, path] of cases) {
      expect(invalidJsonPath(value)).toEqual(path);
      expect(isJsonValue(value)).toBe(false);
    }
  });
});
