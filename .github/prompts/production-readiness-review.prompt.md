---
description: Review code changes, selected files, or a diff for production readiness.
argument-hint: "Provide the change, selected files, or diff plus acceptance criteria."
---

Review the supplied code changes, selected files, or diff as an independent, read-only production-readiness gate. Do not edit files, commit, deploy, or make live device calls.

Ground every conclusion in repository evidence and the [repository instructions](../copilot-instructions.md). Inspect the relevant diff and surrounding code, and verify every stated acceptance criterion.

Prioritize correctness, security, reliability, backward compatibility, test coverage, and operational or deployment risks. Pay particular attention to unsafe device operations, async lifecycle and cancellation behavior, authentication and secret handling, rate limits, data integrity, API contracts, migrations, configuration, rollback behavior, and monitoring. Run narrowly targeted read-only checks when they can confirm or refute a suspected issue. For every check run, record the exact command and its result. Label every required check that is unavailable or not run as `NOT_RUN`; any required `NOT_RUN` check requires `STATUS: BLOCKED`. Do not approve based only on another summary or passing tests.

List findings first, ordered by severity:

```text
[SEVERITY] Title
Evidence: file and symbol or command result
Impact: observable failure or risk
Required change: concrete completion condition
```

Then report acceptance-criteria coverage, reviewed scope (changes, files, and acceptance criteria), command/result test evidence and gaps, assumptions, and separate assessments for correctness, security, reliability, compatibility, maintainability, and operations. Evaluate and explain production readiness in this narrative body.

Finish in this exact order:

1. The review report.
2. The shared artifact contract, with exactly these four machine-readable fields in this order:
	- `STATUS`: `PASS`, `CHANGES_REQUIRED`, or `BLOCKED`
	- `EVIDENCE`: findings, exact commands/results, and every `NOT_RUN` required check
	- `RISKS`: unresolved risks and required changes
	- `NEXT`: follow-up checks or required actions

Map the review outcome only through `STATUS`: `STATUS: PASS` means the review passed and the change can proceed to the Release Gate; it does not mean the change is already production-ready. `STATUS: CHANGES_REQUIRED` means the review found unresolved material findings, and `STATUS: BLOCKED` means required evidence cannot be obtained. Any required check marked `NOT_RUN` requires `STATUS: BLOCKED`. Do not add a separate final verdict line.
