# Next 16.4.2 bundled Zod v3 — local Option A mitigation

Owner: **TrooP81** (Next upgrades, revalidation, rollback).

This is a narrowly pinned local mitigation, **not an official upstream fix,
general denial-of-service protection, application security clearance or release
approval**. The root `zod: 4.6.6` override does not affect Next's compiled v3 copy.
No dependency versions or production lock graph change.

`next-zod.json` records the exact source/digests, unique literal anchors and
design-supplied prior public-source review. That attribution is not a claim that
this implementation independently fetched those sources. The independently
reviewed repaired digest is a literal acceptance condition, never learned from
the installed target.

## Behavior and accepted invalid-input changes

After existing `too_big` issue creation, return frozen `INVALID` before spread,
enumeration or child parsing for:

* arrays exceeding an **existing** max or exact-length upper bound;
* sets exceeding an **existing** max/size;
* no-rest tuples exceeding their declared item count.

There are no arbitrary limits. Lower bounds, boundaries, unconstrained containers
and rest tuples (including `.rest(z.never())`) remain unchanged. Only oversized
invalid inputs change from dirty to aborted: child errors/work disappear,
refinements/super-refinements outside the container may no longer run, unions may
produce `invalid_union` instead of a dirty branch error, catch callbacks see fewer
issues, and intersections may omit a secondary merge error. Transforms/pipeline
stages that require valid input still do not run. Outer preprocessors and the
other intersection branch still run. Sync/async differential tests pin this
contract; do not rely on refinement side effects on invalid input.

## Install/build/runtime controls

`npm ci` and `npm ci --omit=dev` run a builtins-only postinstall patcher.
`--ignore-scripts` (or an equivalent lifecycle bypass) leaves an unpatched copy:
explicitly run `npm run patch:next-zod` and `npm run verify:next-zod` afterward.
Dev/build/start **script bodies** verify before dispatch, even when npm lifecycle
hooks are disabled. Invoking Next directly bypasses those script bodies and is
unsupported. The application cannot defend against an operator replacing these
controls, editing artifacts after verification or running arbitrary commands.

Build removes stale standalone output before Next builds, copies the exact
patched bundle **and full original LICENSE** into the fresh standalone artifact,
and verifies both. Docker copies that standalone output plus the verifier and
manifest, then verifies in the **final runner stage as appuser** after all copies.
CI additionally verifies the built final image offline before launching it.
`CMD ["node", "server.js"]` only consumes verified build artifacts; it performs
no mutation, installation or network verification at launch.

## Fail-closed application, interrupts and rollback

Only the exact Next version, original hash+anchors or repaired hash+anchors and
full LICENSE hash are accepted. Unknown/partially modified bundles fail closed.
Patching is idempotent. Symlink ancestors, symlink/hardlinked files, permissions
failures and target changes detected during the transaction are rejected.

The patcher creates an exclusive `index.cjs.bz-lock`, durable original
`index.cjs.bz-original`, and a unique same-directory temp file; fsyncs the files;
rechecks identity/bytes/mode/mtime; and atomically renames the temp over the target.
POSIX also fsyncs the directory. Windows uses Node's same-filesystem rename but
cannot directory-fsync. Cooperating writers are serialized; an administrator
must not concurrently mutate the installation or its ancestors. This is not a
defense against a hostile filesystem administrator or non-cooperating npm install.

On normal failure the owned lock/temp are removed; the original backup remains.
An abrupt kill/power loss may leave the lock/temp. Verification refuses a remaining
lock, including one left after the rename. **Do not automatically break a lock**:
TrooP81 must establish the recorded PID is no longer running, inspect bundle,
backup, version and LICENSE hashes, and archive evidence before removing only
that owned stale lock/temp. If identity is unknown, quarantine the disposable
installation and use a fresh locked install; do not guess or overwrite it.

`node scripts/patch-next-zod.mjs rollback` restores only the hash-verified original
backup from an exact patched state (or is a no-op on the exact original). Script
verification then deliberately blocks dev/build/start. Never restore HEAD over
the dirty source baseline. To abandon the mitigation, TrooP81 must recover the
recorded pre-change dirty source files, remove only the scoped newly added files,
rebuild/revalidate in isolation and obtain the required independent gates; no
production rollback or device operation is authorized by this document.

Any Next upgrade or changed hash fails closed and requires a newly reviewed
manifest/candidate plus the complete BZ-1..8 matrix. Do not update hashes merely
to make installation pass.

## Reachability and validation

Prior static AppSec review found config/source-export/build/analyzer callers,
not an attacker-controlled production HTTP path. **Tuples are included**: Next's
config schema contains tuple-valued config (`experimental.swcPlugins` entries,
Emotion `canonicalImport`/`styledBaseImport` settings), and app-segment config
contains header tuples parsed from source exports. These are developer-controlled,
and standalone config is serialized before
runtime's validation early return. Dev/MCP callers remain dev-only. The API
catch-all forwards raw bytes and does not parse Zod. This is static analysis,
not proof of absence of a production exploit or a runtime exploit test.

`npm run test:next-zod` covers BZ-1/2 identities and exact states, BZ-3 parser
boundaries/no-work sentinels, BZ-4 compositions and fatal Next config,
BZ-5 artifact notices, and BZ-7 faults/lifecycle/rollback. CI adds BZ-6 dual
Node20/npm10 and Node24/npm11 clean installs/audits/lint/typecheck/build, production
installs and lifecycle bypass. BZ-5 final image and BZ-8 isolation/cleanup must
also be evidenced in the run report. Independent Test Engineer, code Quality
Reviewer and Release Gate are required before completion; none are waived here.
