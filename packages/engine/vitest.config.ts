import { defineConfig } from "vitest/config";
import { fileURLToPath } from "node:url";

export default defineConfig({
  resolve: {
    alias: {
      "@kensa/core": fileURLToPath(
        new URL("../core/src/index.ts", import.meta.url),
      ),
    },
  },
  test: {
    globalSetup: ["./tests/global-setup.ts"],
    coverage: {
      include: ["src/**/*.ts"],
      provider: "v8",
      thresholds: { 100: true },
    },
  },
});
