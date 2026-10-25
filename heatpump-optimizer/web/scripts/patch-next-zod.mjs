import {
  accessSync, closeSync, constants, copyFileSync, existsSync, fsyncSync, lstatSync,
  mkdirSync, openSync, readFileSync, realpathSync, renameSync, statSync,
  unlinkSync, writeFileSync, chmodSync,
} from "node:fs";
import { createHash, randomUUID } from "node:crypto";
import { dirname, join, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

export const webRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
export const manifest = JSON.parse(readFileSync(new URL("../patches/next-zod.json", import.meta.url)));
export const sha256 = (bytes) => createHash("sha256").update(bytes).digest("hex");
const fail = (message) => { throw new Error(`BZ-OPTION-A: ${message}`); };

// No environment-controlled destination and no arbitrary target filename.
function regularPath(root, relative, directory = false) {
  root = resolve(root);
  if (realpathSync(root) !== root) fail("root must be a real directory, not a link");
  let path = root;
  const parts = relative.split("/");
  for (let i = 0; i < parts.length; i++) {
    path = join(path, parts[i]);
    const info = lstatSync(path);
    if (info.isSymbolicLink()) fail(`symlink refused: ${path}`);
    const isDirectory = i < parts.length - 1 || directory;
    if (isDirectory ? !info.isDirectory() : !info.isFile() || info.nlink !== 1) {
      fail(`non-regular or hard-linked path refused: ${path}`);
    }
  }
  if (!path.startsWith(root + sep)) fail("path escaped root");
  return path;
}

function identity(root) {
  const pkg = regularPath(root, "node_modules/next/package.json");
  if (JSON.parse(readFileSync(pkg)).version !== manifest.nextVersion) fail("Next version mismatch");
  const target = regularPath(root, manifest.target);
  const license = regularPath(root, "node_modules/next/dist/compiled/zod/LICENSE");
  if (sha256(readFileSync(license)) !== manifest.license) fail("LICENSE mismatch");
  return target;
}

function anchors(bytes, state) {
  const text = bytes.toString("utf8");
  for (const anchor of manifest.anchors) {
    const present = state === "original" ? anchor.old : anchor.new;
    const absent = state === "original" ? anchor.new : anchor.old;
    if (text.split(present).length !== 2 || text.includes(absent)) fail(`${anchor.id} anchor mismatch`);
  }
}

export function stateOf(bytes) {
  const digest = sha256(bytes);
  const state = digest === manifest.original ? "original" : digest === manifest.patched ? "patched" : null;
  if (!state) fail(`unknown/partial bundle digest ${digest}`);
  anchors(bytes, state);
  return state;
}

export function verify(root = webRoot) {
  const target = identity(root);
  if (stateOf(readFileSync(target)) !== "patched") fail("unpatched bundle; run npm run patch:next-zod");
  // An interrupted writer must not be silently treated as a completed transaction.
  if (existsSync(`${target}.bz-lock`)) fail("transaction lock present; inspect recovery instructions");
  return manifest.patched;
}

// Mutation is serialised by an exclusive lock. Never remove another writer's lock.
// Backup and temp are in the target directory; rename is a same-filesystem atomic replace.
export function apply(root = webRoot, { rollback = false, candidatePath, beforeReplace } = {}) {
  const target = identity(root);
  const parent = dirname(target);
  const initial = lstatSync(target);
  if (!(initial.mode & 0o222) || !(statSync(parent).mode & 0o222) ||
      (process.platform !== "win32" && !(statSync(parent).mode & 0o111))) {
    fail("target and directory must be writable/searchable");
  }
  accessSync(target, constants.W_OK);
  accessSync(parent, constants.W_OK);
  const lock = `${target}.bz-lock`;
  const lockFd = openSync(lock, "wx", 0o600);
  let temp;
  try {
    writeFileSync(lockFd, JSON.stringify({ pid: process.pid, operation: rollback ? "rollback" : "apply" }));
    fsyncSync(lockFd);
    const original = readFileSync(target);
    const state = stateOf(original);
    let suppliedCandidate;
    if (candidatePath) {
      const info = lstatSync(candidatePath);
      if (!info.isFile() || info.isSymbolicLink() || info.nlink !== 1) fail("candidate is not regular");
      suppliedCandidate = readFileSync(candidatePath);
      if (stateOf(suppliedCandidate) !== "patched") fail("candidate mismatch");
    }
    const desired = rollback ? "original" : "patched";
    if (state === desired) return { state, changed: false };
    const backup = `${target}.bz-original`;
    let output;
    if (rollback) {
      regularPath(root, `${manifest.target}.bz-original`);
      output = readFileSync(backup);
      if (sha256(output) !== manifest.original) fail("rollback backup mismatch");
    } else {
      if (candidatePath) {
        // Initial implementation consumes the independent candidate bytes verbatim.
        output = suppliedCandidate;
      } else {
        // Clean installs use only fixed unique literals and the independently reviewed digest.
        let text = original.toString("utf8");
        for (const anchor of manifest.anchors) text = text.replace(anchor.old, anchor.new);
        output = Buffer.from(text);
      }
      if (stateOf(output) !== "patched") fail("candidate mismatch");
      if (existsSync(backup)) {
        regularPath(root, `${manifest.target}.bz-original`);
        if (sha256(readFileSync(backup)) !== manifest.original) fail("existing backup mismatch");
      } else {
        const fd = openSync(backup, "wx", initial.mode & 0o777);
        try { writeFileSync(fd, original); fsyncSync(fd); } finally { closeSync(fd); }
      }
    }
    if (sha256(output) !== (rollback ? manifest.original : manifest.patched)) fail("output digest mismatch");
    temp = `${target}.bz-tmp-${randomUUID()}`;
    const fd = openSync(temp, "wx", initial.mode & 0o777);
    try {
      writeFileSync(fd, output);
      chmodSync(temp, initial.mode & 0o777);
      fsyncSync(fd);
    } finally { closeSync(fd); }
    // Test seam models a concurrent/failed writer, never exposed by the CLI.
    beforeReplace?.();
    if (identity(root) !== target) fail("identity changed during transaction");
    const current = lstatSync(target);
    if (current.ino !== initial.ino || current.dev !== initial.dev ||
        current.mode !== initial.mode || current.mtimeMs !== initial.mtimeMs ||
        !readFileSync(target).equals(original)) fail("target changed during transaction");
    if (sha256(readFileSync(temp)) !== sha256(output)) fail("temporary output changed");
    renameSync(temp, target);
    temp = undefined;
    // POSIX directory fsync makes the rename durable. Windows does not support it.
    if (process.platform !== "win32") {
      const dirFd = openSync(parent, constants.O_RDONLY);
      try { fsyncSync(dirFd); } finally { closeSync(dirFd); }
    }
    if (stateOf(readFileSync(target)) !== desired) fail("post-replacement verification failed");
    return { state: desired, changed: true };
  } finally {
    if (temp && existsSync(temp)) unlinkSync(temp);
    closeSync(lockFd);
    unlinkSync(lock);
  }
}

export function prepareStandalone(root = webRoot) {
  verify(root);
  const standalone = regularPath(root, ".next/standalone", true);
  const destination = join(standalone, "node_modules/next/dist/compiled/zod");
  // Trace omissions are repaired only inside the fresh build's standalone artifact.
  // Refuse pre-existing symlink ancestors rather than copy through them.
  for (const relative of ["node_modules", "node_modules/next", "node_modules/next/dist", "node_modules/next/dist/compiled", "node_modules/next/dist/compiled/zod"]) {
    const path = join(standalone, relative);
    if (existsSync(path)) regularPath(standalone, relative, true);
    else mkdirSync(path);
  }
  for (const name of ["index.cjs", "LICENSE"]) {
    const output = join(destination, name);
    if (existsSync(output)) regularPath(standalone, `node_modules/next/dist/compiled/zod/${name}`);
    copyFileSync(join(root, "node_modules/next/dist/compiled/zod", name), output);
  }
  verify(standalone);
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const [command, ...args] = process.argv.slice(2);
    if (command === "verify" && args.length === 0) console.log("BZ verified", verify());
    else if (command === "verify-artifact" && args.length === 1) console.log("BZ artifact verified", verify(resolve(args[0])));
    else if (command === "apply" && args.length === 0) console.log("BZ", apply());
    else if (command === "apply-candidate" && args.length === 1) console.log("BZ", apply(webRoot, { candidatePath: resolve(args[0]) }));
    else if (command === "rollback" && args.length === 0) console.log("BZ", apply(webRoot, { rollback: true }));
    else fail("usage: apply | apply-candidate FILE | verify | verify-artifact ROOT | rollback");
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}
