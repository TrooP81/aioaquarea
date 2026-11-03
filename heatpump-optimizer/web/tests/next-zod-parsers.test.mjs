import assert from "node:assert/strict";
import { readFileSync, mkdtempSync, writeFileSync, rmSync, mkdirSync } from "node:fs";
import Module from "node:module";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import { test } from "node:test";
import { manifest, sha256, stateOf, webRoot } from "../scripts/patch-next-zod.mjs";

const installed = readFileSync(join(webRoot, manifest.target));
let oldText = installed.toString();
if (stateOf(installed) === "patched") {
  for (const anchor of manifest.anchors) oldText = oldText.replace(anchor.new, anchor.old);
}
assert.equal(sha256(oldText), manifest.original);
let newText = oldText;
for (const anchor of manifest.anchors) newText = newText.replace(anchor.old, anchor.new);
assert.equal(sha256(newText), manifest.patched);
function load(text, name) {
  const mod = new Module(join(webRoot, name));
  mod.filename = join(webRoot, name);
  mod.paths = Module._nodeModulePaths(webRoot);
  mod._compile(text, mod.filename);
  return mod.exports.z;
}
const versions = { original: load(oldText, "bz-original.cjs"), patched: load(newText, "bz-patched.cjs") };
const outcome = (result) => result.success ? { success: true, data: result.data } : { success: false, issues: result.error.issues };
const parse = (schema, input, async) => async ? schema.safeParseAsync(input) : schema.safeParse(input);
const shapes = [
  { name: "array.max", make: (z, element) => z.array(element).max(2, "upper"), input: (n) => Array(n).fill(1) },
  { name: "array.length", make: (z, element) => z.array(element).length(2, "exact"), input: (n) => Array(n).fill(1) },
  { name: "set.max", make: (z, element) => z.set(element).max(2, "upper"), input: (n) => new Set(Array.from({ length: n }, (_, i) => i)) },
  { name: "set.size", make: (z, element) => z.set(element).size(2, "exact"), input: (n) => new Set(Array.from({ length: n }, (_, i) => i)) },
  { name: "tuple", make: (z, element) => z.tuple([element, element]), input: (n) => Array(n).fill(1) },
];

for (const async of [false, true]) {
  test(`BZ-3 sync=${!async}: boundary/lower/oversized issues and zero element work`, async () => {
    for (const shape of shapes) {
      for (const size of [0, 1, 2, 3]) {
        const results = {};
        const counts = {};
        for (const [version, z] of Object.entries(versions)) {
          let count = 0;
          const element = z.preprocess((x) => { count++; return x; }, z.number());
          const schema = shape.make(z, element);
          results[version] = outcome(await parse(schema, shape.input(size), async));
          counts[version] = count;
        }
        assert.deepEqual(results.patched, results.original, `${shape.name} ${size}`);
        if (size === 3) {
          assert.equal(counts.patched, 0, shape.name);
          assert.ok(counts.original > 0, shape.name);
          assert.equal(results.patched.issues[0].code, "too_big");
        } else assert.equal(counts.patched, counts.original, shape.name);
      }
    }
  });

  test(`BZ-3 sync=${!async}: oversized guards run before spread/enumeration/getters`, async () => {
    const z = versions.patched;
    for (const shape of shapes) {
      const value = shape.input(3);
      Object.defineProperty(value, Symbol.iterator, { value() { throw new Error("ITERATED"); } });
      if (Array.isArray(value)) Object.defineProperty(value, "0", { get() { throw new Error("ELEMENT_READ"); } });
      const result = await parse(shape.make(z, z.any()), value, async);
      assert.equal(result.success, false);
      assert.equal(result.error.issues[0].code, "too_big");
      assert.equal(result.error.issues.length, 1);
    }
  });

  test(`BZ-3 sync=${!async}: unconstrained, min, rest and rest(never) unchanged`, async () => {
    for (const factory of [
      (z) => z.array(z.number()), (z) => z.array(z.number()).min(2),
      (z) => z.array(z.number()).length(1).max(0),
      (z) => z.tuple([z.number()]).rest(z.number()),
      (z) => z.tuple([z.number()]).rest(z.never()),
      (z) => z.tuple([]), (z) => z.array(z.number()).max(0),
      (z) => z.set(z.number()), (z) => z.set(z.number()).min(2),
      (z) => z.set(z.number()).max(0),
    ]) {
      for (const input of [[], [1], [1, 2], ["bad", 2], new Set(), new Set([1, 2])]) {
        const old = outcome(await parse(factory(versions.original), input, async));
        const patched = outcome(await parse(factory(versions.patched), input, async));
        // Contradictory upper bounds intentionally omit child errors; use valid elements
        // for issue preservation and assert invalid element omission separately below.
        if (Array.isArray(input) && input[0] === "bad" && old.issues?.some((x) => x.code === "too_big")) {
          assert.equal(patched.success, false);
          assert.deepEqual(patched.issues, old.issues.filter((x) => x.path.length === 0));
        } else assert.deepEqual(patched, old);
      }
    }
  });

  test(`BZ-4 sync=${!async}: approved oversized-invalid composition contract`, async () => {
    for (const shape of shapes) {
      for (const composition of ["refine", "superRefine", "union", "catch", "transform", "intersection", "pipeline", "preprocess"]) {
        const observed = {};
        for (const [version, z] of Object.entries(versions)) {
          let elements = 0, effects = 0, preprocessing = 0, later = 0;
          const element = z.preprocess((x) => { elements++; return x; }, z.number());
          const base = shape.make(z, element);
          let schema;
          const effect = () => { effects++; return true; };
          if (composition === "refine") schema = base.refine(effect);
          if (composition === "superRefine") schema = base.superRefine(() => { effects++; });
          if (composition === "union") schema = z.union([base, z.literal("other")]);
          if (composition === "catch") schema = base.catch((ctx) => { effects++; return ctx.error.issues.map((x) => x.code); });
          if (composition === "transform") schema = base.transform((x) => { effects++; return x; });
          if (composition === "intersection") schema = z.intersection(base, z.any().superRefine(() => { later++; }));
          if (composition === "pipeline") schema = base.pipe(z.any().superRefine(() => { later++; }));
          if (composition === "preprocess") schema = z.preprocess((x) => { preprocessing++; return x; }, base);
          const result = outcome(await parse(schema, shape.input(3), async));
          observed[version] = { result, elements, effects, preprocessing, later };
        }
        const old = observed.original, patched = observed.patched;
        assert.ok(old.elements > 0);
        assert.equal(patched.elements, 0);
        if (["refine", "superRefine"].includes(composition)) {
          assert.equal(old.effects, 1);
          assert.equal(patched.effects, 0); // dirty -> aborted is explicitly approved.
        } else assert.equal(patched.effects, old.effects);
        assert.equal(patched.later, old.later);
        assert.equal(patched.preprocessing, old.preprocessing);
        if (composition === "union") {
          assert.equal(old.result.issues[0].code, "too_big");
          assert.equal(patched.result.issues[0].code, "invalid_union");
          assert.equal(patched.result.issues[0].unionErrors[0].issues[0].code, "too_big");
        } else if (composition === "intersection" && (shape.name.startsWith("set.") || shape.name === "tuple")) {
          // Original dirty Set results cannot merge; a no-rest tuple's truncated
          // output also cannot merge with the longer input. Aborted skips merge.
          assert.deepEqual(old.result.issues.map((x) => x.code), ["too_big", "invalid_intersection_types"]);
          assert.deepEqual(patched.result.issues.map((x) => x.code), ["too_big"]);
          assert.deepEqual(patched.result.issues[0], old.result.issues[0]);
        } else assert.deepEqual(patched.result, old.result);
      }
    }
  });

  test(`BZ-4 sync=${!async}: valid/boundary compositions remain identical`, async () => {
    for (const shape of shapes) {
      for (const compose of [
        (z, base) => base.refine(() => true),
        (z, base) => base.superRefine(() => {}),
        (z, base) => z.union([base, z.literal("other")]),
        (z, base) => base.catch("fallback"),
        (z, base) => base.transform((x) => x),
        (z, base) => z.intersection(base, z.any()),
        (z, base) => base.pipe(z.any()),
        (z, base) => z.preprocess((x) => x, base),
      ]) {
        const results = {};
        for (const [version, z] of Object.entries(versions)) {
          results[version] = outcome(await parse(compose(z, shape.make(z, z.number())), shape.input(2), async));
        }
        assert.deepEqual(results.patched, results.original, shape.name);
      }
    }
  });

  test(`BZ-4 sync=${!async}: catch receives fewer child issues; async child/effects are skipped only oversized`, async () => {
    for (const [version, z] of Object.entries(versions)) {
      const result = await parse(z.array(z.number()).max(1).catch((ctx) => ctx.error.issues.map((x) => x.code)), ["bad", "bad"], async);
      assert.deepEqual(result.data, version === "original" ? ["too_big", "invalid_type", "invalid_type"] : ["too_big"]);
    }
    if (async) {
      let count = 0, effect = 0;
      const z = versions.patched;
      const schema = z.array(z.number().refine(async () => { count++; return true; })).max(1)
        .refine(async () => { effect++; return true; });
      assert.equal((await schema.safeParseAsync([1, 2])).success, false);
      assert.equal(count, 0);
      assert.equal(effect, 0);
      assert.equal((await schema.safeParseAsync([1])).success, true);
      assert.equal(count, 1);
      assert.equal(effect, 1);
    }
  });
}

test("BZ-4 actual Next CLI: fatal invalid image config exits nonzero", (t) => {
  const root = mkdtempSync(join(tmpdir(), "bz-next-config-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  mkdirSync(join(root, "app"));
  writeFileSync(join(root, "app/page.js"), "export default function Page(){return null}");
  writeFileSync(join(root, "package.json"), '{"private":true}');
  writeFileSync(join(root, "next.config.js"), `module.exports={images:{domains:${JSON.stringify(Array(51).fill("invalid.example"))}}};`);
  const result = spawnSync(process.execPath, [join(webRoot, "node_modules/next/dist/bin/next"), "build", root], {
    cwd: root, encoding: "utf8", timeout: 20000, env: { ...process.env, NEXT_TELEMETRY_DISABLED: "1" },
  });
  assert.equal(result.error, undefined);
  assert.notEqual(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stdout + result.stderr, /images|domains/);
  assert.match(result.stdout + result.stderr, /Invalid next.config|Invalid configuration|Invalid options|exceed|50/i);
});
