import { expect, it } from "vitest";
import { SCHEMA_VERSION, DOCUMENT_KINDS } from "../src/index.js";
it("exposes protocol v1", () => {
  expect(SCHEMA_VERSION).toBe("kensa.protocol.v1");
  expect(DOCUMENT_KINDS).toHaveLength(4);
});
