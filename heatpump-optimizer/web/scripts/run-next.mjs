import { spawn } from "node:child_process";
import { existsSync, lstatSync, rmSync } from "node:fs";
import { join } from "node:path";
import { prepareStandalone, verify, webRoot } from "./patch-next-zod.mjs";

const [command, ...args] = process.argv.slice(2);
if (!["dev", "build", "start"].includes(command)) throw new Error("Expected dev, build or start");
// In the script body, not a pre* hook: npm --ignore-scripts cannot bypass this check.
verify();
if (command === "build") {
  // Never verify stale standalone output from an earlier successful build.
  const output = join(webRoot, ".next");
  if (existsSync(output) && (!lstatSync(output).isDirectory() || lstatSync(output).isSymbolicLink())) {
    throw new Error("BZ-OPTION-A: .next must be a real build directory");
  }
  rmSync(join(webRoot, ".next/standalone"), { recursive: true, force: true });
}
const child = spawn(process.execPath, [join(webRoot, "node_modules/next/dist/bin/next"), command, ...args], {
  cwd: webRoot, env: process.env, stdio: "inherit",
});
for (const signal of ["SIGTERM", "SIGINT"]) process.on(signal, () => child.kill(signal));
child.on("error", (error) => { console.error(error); process.exitCode = 1; });
child.on("exit", (code, signal) => {
  if (code !== 0 || signal) {
    process.exitCode = code || 1;
    return;
  }
  try {
    if (command === "build") prepareStandalone();
    process.exitCode = 0;
  } catch (error) {
    console.error(error);
    process.exitCode = 1;
  }
});
