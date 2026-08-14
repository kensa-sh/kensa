import { platform } from "node:process";

export function requiresCommandShell(command, currentPlatform = platform) {
  return currentPlatform === "win32" && /\.(?:bat|cmd)$/i.test(command);
}
