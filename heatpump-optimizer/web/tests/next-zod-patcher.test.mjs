import assert from "node:assert/strict";
import { chmodSync, copyFileSync, existsSync, linkSync, mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { spawnSync } from "node:child_process";
import { test } from "node:test";
import { apply, manifest, prepareStandalone, sha256, stateOf, verify, webRoot } from "../scripts/patch-next-zod.mjs";

const installed = readFileSync(join(webRoot, manifest.target));
const original = stateOf(installed) === "original" ? installed : Buffer.from(
  manifest.anchors.reduce((text, anchor) => text.replace(anchor.new, anchor.old), installed.toString()),
);
assert.equal(sha256(original), manifest.original);
const license = readFileSync(join(webRoot, "node_modules/next/dist/compiled/zod/LICENSE"));
function fixture(t) {
  const root = mkdtempSync(join(tmpdir(), "bz-patcher-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const target = join(root, manifest.target);
  mkdirSync(dirname(target), { recursive: true });
  writeFileSync(target, original);
  writeFileSync(join(dirname(target), "LICENSE"), license);
  writeFileSync(join(root, "node_modules/next/package.json"), JSON.stringify({ version: manifest.nextVersion }));
  return { root, target };
}

test("BZ-2 original -> exact patched, idempotent, rollback -> exact original", (t) => {
  const { root, target } = fixture(t);
  assert.deepEqual(apply(root), { state: "patched", changed: true });
  assert.equal(verify(root), manifest.patched);
  assert.equal(sha256(readFileSync(target)), manifest.patched);
  assert.deepEqual(apply(root), { state: "patched", changed: false });
  assert.deepEqual(apply(root, { rollback: true }), { state: "original", changed: true });
  assert.deepEqual(readFileSync(target), original);
  assert.throws(() => verify(root), /unpatched/);
  assert.deepEqual(apply(root, { rollback: true }), { state: "original", changed: false });
});

test("BZ-2 unknown, partial, version, license and backup mismatches fail closed", (t) => {
  for (const mutation of ["unknown", "partial", "version", "license", "backup"]) {
    const { root, target } = fixture(t);
    if (mutation === "unknown") writeFileSync(target, Buffer.concat([original, Buffer.from(" ")]));
    if (mutation === "partial") writeFileSync(target, original.toString().replace(manifest.anchors[0].old, manifest.anchors[0].new));
    if (mutation === "version") writeFileSync(join(root, "node_modules/next/package.json"), '{"version":"16.4.3"}');
    if (mutation === "license") writeFileSync(join(dirname(target), "LICENSE"), "changed");
    if (mutation === "backup") writeFileSync(`${target}.bz-original`, "changed");
    const before = readFileSync(target);
    assert.throws(() => apply(root), /mismatch|unknown/);
    assert.deepEqual(readFileSync(target), before);
    assert.equal(existsSync(`${target}.bz-lock`), false);
  }
});

test("BZ-2 supplied candidate mismatch fails closed even on an already patched target", (t) => {
  const { root, target } = fixture(t);
  const candidate = join(root, "candidate.cjs");
  writeFileSync(candidate, "not the reviewed candidate");
  assert.throws(() => apply(root, { candidatePath: candidate }), /unknown/);
  assert.deepEqual(readFileSync(target), original);
  apply(root);
  const patched = readFileSync(target);
  assert.throws(() => apply(root, { candidatePath: candidate }), /unknown/);
  assert.deepEqual(readFileSync(target), patched);
});

test("BZ-7 lock, race, interrupted preparation and rollback corruption fail closed", (t) => {
  const { root, target } = fixture(t);
  writeFileSync(`${target}.bz-lock`, "another writer or interrupted transaction");
  assert.throws(() => apply(root), /EEXIST/);
  assert.equal(readFileSync(`${target}.bz-lock`, "utf8"), "another writer or interrupted transaction");
  rmSync(`${target}.bz-lock`);
  assert.throws(() => apply(root, { beforeReplace() { throw new Error("simulated interruption"); } }), /interruption/);
  assert.deepEqual(readFileSync(target), original);
  const raced = Buffer.concat([original, Buffer.from(" ")]);
  assert.throws(() => apply(root, { beforeReplace() { writeFileSync(target, raced); } }), /changed during/);
  assert.deepEqual(readFileSync(target), raced); // Never overwrite another writer's bytes.
  writeFileSync(target, original);
  apply(root);
  writeFileSync(`${target}.bz-original`, "corrupt");
  const patched = readFileSync(target);
  assert.throws(() => apply(root, { rollback: true }), /backup mismatch/);
  assert.deepEqual(readFileSync(target), patched);
  writeFileSync(`${target}.bz-lock`, "interrupted after rename");
  assert.throws(() => verify(root), /lock present/);
});

test("BZ-7 readonly permissions, symlink/hardlink targets and linked ancestors refused", (t) => {
  const { root, target } = fixture(t);
  chmodSync(target, 0o444);
  try { assert.throws(() => apply(root), /writable/); } finally { chmodSync(target, 0o644); }
  if (process.platform !== "win32") {
    chmodSync(dirname(target), 0o555);
    try { assert.throws(() => apply(root), /writable/); } finally { chmodSync(dirname(target), 0o755); }
  }
  const outside = join(root, "outside.cjs");
  copyFileSync(target, outside);
  rmSync(target);
  linkSync(outside, target);
  assert.throws(() => apply(root), /hard-linked/);
  rmSync(target);
  // Windows requires elevated privileges for file symlinks; Linux CI must exercise it.
  if (process.platform !== "win32") {
    symlinkSync(outside, target);
    assert.throws(() => apply(root), /symlink/);
    rmSync(target);
    writeFileSync(target, original);
    const zod = dirname(target);
    const moved = join(root, "external-zod");
    mkdirSync(moved);
    copyFileSync(target, join(moved, "index.cjs"));
    copyFileSync(join(zod, "LICENSE"), join(moved, "LICENSE"));
    rmSync(zod, { recursive: true });
    symlinkSync(moved, zod);
    assert.throws(() => apply(root), /symlink/);
  }
  assert.deepEqual(readFileSync(outside), original);
});

test("BZ-5 fresh standalone copy includes exact bundle and LICENSE; tampering rejected", (t) => {
  const { root } = fixture(t);
  apply(root);
  const standalone = join(root, ".next/standalone");
  mkdirSync(join(standalone, "node_modules/next"), { recursive: true });
  writeFileSync(join(standalone, "node_modules/next/package.json"), JSON.stringify({ version: manifest.nextVersion }));
  prepareStandalone(root);
  assert.equal(verify(standalone), manifest.patched);
  const artifactLicense = join(standalone, "node_modules/next/dist/compiled/zod/LICENSE");
  assert.deepEqual(readFileSync(artifactLicense), license);
  writeFileSync(artifactLicense, "missing notice");
  assert.throws(() => verify(standalone), /LICENSE mismatch/);
});

test("BZ-7 lifecycle bypass: dev/build/start body refuses original before Next dispatch", (t) => {
  const { root, target } = fixture(t);
  mkdirSync(join(root, "scripts"));
  mkdirSync(join(root, "patches"));
  for (const name of ["patch-next-zod.mjs", "run-next.mjs"]) {
    copyFileSync(join(webRoot, "scripts", name), join(root, "scripts", name));
  }
  copyFileSync(join(webRoot, "patches/next-zod.json"), join(root, "patches/next-zod.json"));
  mkdirSync(join(root, "node_modules/next/dist/bin"), { recursive: true });
  writeFileSync(join(root, "node_modules/next/dist/bin/next"), 'throw new Error("NEXT_DISPATCHED");');
  for (const command of ["dev", "build", "start"]) {
    const result = spawnSync(process.execPath, [join(root, "scripts/run-next.mjs"), command], {
      encoding: "utf8", timeout: 10000, env: { ...process.env, npm_config_ignore_scripts: "true" },
    });

    assert.equal(result.status, 1, result.stderr);
    assert.match(result.stderr, /unpatched bundle/);
    assert.doesNotMatch(result.stderr, /NEXT_DISPATCHED/);
    assert.deepEqual(readFileSync(target), original);
  }
});

test("BZ-5 build cannot accept stale standalone output when Next creates no fresh artifact", (t) => {
  const { root } = fixture(t);
  apply(root);
  mkdirSync(join(root, "scripts"));
  mkdirSync(join(root, "patches"));
  for (const name of ["patch-next-zod.mjs", "run-next.mjs"]) {
    copyFileSync(join(webRoot, "scripts", name), join(root, "scripts", name));
  }
  copyFileSync(join(webRoot, "patches/next-zod.json"), join(root, "patches/next-zod.json"));
  mkdirSync(join(root, "node_modules/next/dist/bin"), { recursive: true });
  writeFileSync(join(root, "node_modules/next/dist/bin/next"), "process.exit(0);");
  const stale = join(root, ".next/standalone");
  mkdirSync(join(stale, "node_modules/next"), { recursive: true });
  writeFileSync(join(stale, "node_modules/next/package.json"), JSON.stringify({ version: manifest.nextVersion }));
  prepareStandalone(root);
  assert.equal(verify(stale), manifest.patched);
  const result = spawnSync(process.execPath, [join(root, "scripts/run-next.mjs"), "build"], {
    encoding: "utf8", timeout: 10000,
  });
  assert.equal(result.status, 1, result.stdout + result.stderr);
  assert.match(result.stderr, /ENOENT/);
  assert.equal(existsSync(stale), false);
});
