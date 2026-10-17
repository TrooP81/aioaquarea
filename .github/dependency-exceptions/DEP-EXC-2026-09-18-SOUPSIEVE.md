# DEP-EXC-2026-09-18-SOUPSIEVE

- **Status:** Retired on 2026-09-23; Remediated on 2026-09-22
- **Approved:** 2026-09-18 by the user for the current local-only release
- **Owner:** Repository maintainer (Carlos J. Aliaga)
- **Package:** `soupsieve` 2.8.4
- **Advisories:** CVE-2026-85999 and CVE-2026-86000
- **GHSA aliases:** None known at approval time
- **Fixed version:** `>=2.9.0`
- **Closure:** `soupsieve>=2.9,<3` is enforced by root package metadata and
	Pipenv; the optimizer's reviewed constraints resolve `soupsieve==2.9.2`.
- **Approved through releases:** `aioaquarea 1.0.11` and `heatpump-optimizer 0.13.6` only
- **Tracked removal:** [SOUP-001](../dependency-followups.md#soup-001)

## Historical scope

This former waiver was valid only for root manifest version `1.0.11` and
optimizer manifest version `0.13.6`. It is retired for `aioaquarea 1.0.12`
and `heatpump-optimizer 0.13.7`; no audit suppression is authorized for this
or future releases. The `pip-audit` ignores were removed on 2026-09-22 after a
clean audit of the fixed resolution.

## Scope and reachability

This exception permits only the two listed `pip-audit` findings for the current
local-only release. The sole BeautifulSoup use in `aioaquarea/auth.py` parses
hidden inputs with `find_all`; it does not call `select`, `select_one`, or any
CSS-selector API. The vulnerable CSS-selector path is therefore unreachable
from application-owned Python code at approval time.

## Remediation evidence

The root project and `heatpump-optimizer` dependency resolutions enforce
`soupsieve>=2.9,<3`. The CI audit runs without ignores. The release workflow
generates an SBOM for the optimizer. This record is retained for advisory
traceability and must not be reused as an audit suppression.