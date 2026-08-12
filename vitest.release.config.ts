import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["scripts/**/*.test.mjs"],
    coverage: {
      include: [
        "scripts/build-manifest.mjs",
        "scripts/generate-build-manifest.mjs",
        "scripts/process-platform.mjs",
      ],
      provider: "v8",
      thresholds: { 100: true },
    },
  },
});
