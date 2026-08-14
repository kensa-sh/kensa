import { rmSync } from "node:fs";
import { dirname, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const packageRoot = resolve(process.cwd());
const allowed = new Set([
  resolve(root, "packages/core"),
  resolve(root, "packages/engine"),
  resolve(root, "sdk/typescript"),
]);
if (!allowed.has(packageRoot)) {
  throw new Error(
    `refusing to clean dist outside a Kensa package: ${relative(root, packageRoot)}`,
  );
}
rmSync(resolve(packageRoot, "dist"), { recursive: true, force: true });
