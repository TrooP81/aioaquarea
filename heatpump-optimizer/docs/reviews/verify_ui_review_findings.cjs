/* Offline source-expression probes; this file does not start a browser or server. */
"use strict";

const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const crypto = require("node:crypto");
const { execFileSync } = require("node:child_process");
const appRoot = path.resolve(__dirname, "../..");
const ts = require(path.join(appRoot, "web/node_modules/typescript"));
const React = require(path.join(appRoot, "web/node_modules/react"));
const { renderToStaticMarkup } = require(path.join(appRoot, "web/node_modules/react-dom/server"));

function read(relative) {
  return fs.readFileSync(path.join(appRoot, relative), "utf8");
}

function runTypeScript(source, filename, context = {}) {
  const js = ts.transpileModule(source, {
    fileName: filename,
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText;
  const sandbox = vm.createContext({ exports: {}, ...context });
  vm.runInContext(js, sandbox, { timeout: 1000 });
  return sandbox;
}

const resetPath = "web/components/ResetDataCard.tsx";
const resetSource = read(resetPath);
const resetTree = ts.createSourceFile(resetPath, resetSource, ts.ScriptTarget.Latest, true);
const resetFunction = resetTree.statements.find((node) =>
  ts.isFunctionDeclaration(node) && node.name?.text === "resetErrorMessage");
const payloads = [
  { detail: { code: "unresolved_safety_reverts", action_id: 11 } },
  { detail: { code: "ml_lifecycle_unavailable" } },
  { detail: "Plain error" },
  { detail: { unexpected: "payload" } },
];
let resetResult;
if (resetFunction) {
  const context = runTypeScript(resetFunction.getText(resetTree), resetPath);
  const messages = payloads.map((payload) => context.resetErrorMessage(payload));
  const rendered = messages.map((message) => renderToStaticMarkup(React.createElement("p", null, message)));
  resetResult = { current_source_remedy_present: true, messages, rendered_without_error: rendered.length === messages.length };
} else {
  let oldError = null;
  try { renderToStaticMarkup(React.createElement("p", null, payloads[0].detail)); }
  catch (error) { oldError = error.message; }
  resetResult = { current_source_remedy_present: false, original_render_error: oldError };
}

const constantPath = "web/lib/constants.ts";
const labels = runTypeScript(read(constantPath), constantPath).exports;
const labelResult = {
  degraded: labels.forecastDisplayStatusLabel("degraded"),
  reference_sensor: labels.reasonLabel("reference_sensor"),
  forecast_unavailable_or_legacy: labels.reasonLabel("forecast_unavailable_or_legacy"),
};

const pricePath = "web/components/ExplanationTimeline.tsx";
const priceExpression = "pricePoints[pricePoints.length - 1].price";
if (!read(pricePath).includes(priceExpression)) throw new Error("Reinspect F10: its source expression changed");
const pricePoints = [
  { timestamp: Date.parse("2026-10-12T12:00:00Z"), price: 2.21 },
  { timestamp: Date.parse("2026-10-13T12:00:00Z"), price: 1.71 },
];
const priceSelected = vm.runInNewContext(priceExpression, { pricePoints }, { timeout: 1000 });

const tablePath = "web/components/TimelineDataTable.tsx";
const tableSource = read(tablePath);
const tableTree = ts.createSourceFile(tablePath, tableSource, ts.ScriptTarget.Latest, true);
let matchingCall;
function visit(node) {
  if (ts.isCallExpression(node) && ts.isPropertyAccessExpression(node.expression)
    && node.expression.expression.getText(tableTree) === "actions" && node.expression.name.text === "find") matchingCall = node;
  ts.forEachChild(node, visit);
}
visit(tableTree);
if (!matchingCall) throw new Error("Reinspect F11: its matching expression changed");
const actions = [{ id: 101, scheduled_ts: "2026-10-12T12:07:00Z", executed_at: null }];
const rowTimes = ["11:40", "11:45", "11:50", "11:55", "12:00", "12:05"];
const matching = rowTimes.map((value) => {
  const point = { timestamp: Date.parse(`2026-10-12T${value}:00Z`) };
  const result = vm.runInNewContext(matchingCall.getText(tableTree), { actions, point }, { timeout: 1000 });
  return { row_utc: value, selected_action_id: result?.id ?? null };
});

const sourceFiles = [resetPath, constantPath, pricePath, tablePath];
const evidence = {
  captured_at_utc: new Date().toISOString(),
  head: execFileSync("git", ["rev-parse", "HEAD"], { cwd: appRoot, encoding: "utf8" }).trim(),
  verification_kind: "offline_source_expressions_and_React_render_no_browser",
  source_sha256: Object.fromEntries(sourceFiles.map((name) => [name, crypto.createHash("sha256").update(read(name)).digest("hex")])),
  results: {
    F05: resetResult,
    F09: { ...labelResult, defect_reproduced: Object.values(labelResult).some((value) => value.startsWith("Unrecognized status")) },
    F10: { selected_caption_price: priceSelected, price_at_now: 2.21, defect_reproduced: priceSelected !== 2.21 },
    F11: { matching, defect_reproduced: matching.filter((row) => row.selected_action_id === 101).length > 1 },
  },
};
const outputIndex = process.argv.indexOf("--output");
if (outputIndex !== -1) {
  if (!process.argv[outputIndex + 1]) throw new Error("--output needs a file path");
  const output = path.resolve(process.argv[outputIndex + 1]);
  fs.mkdirSync(path.dirname(output), { recursive: true });
  fs.writeFileSync(output, JSON.stringify(evidence, null, 2) + "\n");
  process.stdout.write(`Evidence written to ${output}\n`);
} else {
  process.stdout.write(JSON.stringify(evidence, null, 2) + "\n");
}
