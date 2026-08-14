import { expect, test, type TestOptions } from "vitest";

import {
  runEvaluation,
  type Complete,
  type EvaluationDefinition,
  type EvaluationVerdict,
} from "./index.js";

type Awaitable<T> = T | PromiseLike<T>;

export interface KensaTestOptions {
  expectedVerdict?: EvaluationVerdict;
  verify?: (result: Complete) => Awaitable<void>;
  vitest?: TestOptions;
}

export type KensaTest = (
  name: string,
  definition: EvaluationDefinition,
  options?: KensaTestOptions,
) => void;

export interface KensaTestDependencies {
  register: (
    name: string,
    options: TestOptions,
    handler: () => Promise<void>,
  ) => void;
  assertEqual: (actual: unknown, expected: unknown) => void;
}

export function createKensaTest(
  dependencies: KensaTestDependencies,
): KensaTest {
  return (name, definition, options = {}) => {
    dependencies.register(name, options.vitest ?? {}, async () => {
      const result = await runEvaluation(definition);
      dependencies.assertEqual(
        result.verdict,
        options.expectedVerdict ?? "pass",
      );
      await options.verify?.(result);
    });
  };
}

export const kensaTest = createKensaTest({
  register: (name, options, handler) => {
    test(name, options, handler);
  },
  assertEqual: (actual, expected) => {
    expect(actual).toBe(expected);
  },
});
