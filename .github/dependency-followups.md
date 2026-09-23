# Dependency Follow-ups

## SOUP-001

- Waiver: [DEP-EXC-2026-09-18-SOUPSIEVE.md](dependency-exceptions/DEP-EXC-2026-09-18-SOUPSIEVE.md)
- Owner: Carlos J. Aliaga
- Status: Closed 2026-09-22; waiver retired for releases after `aioaquarea 1.0.11` and `heatpump-optimizer 0.13.6` on 2026-09-23
- Resolution: Enforced `soupsieve>=2.9,<3` in root package metadata and Pipenv; optimizer constraints resolve `soupsieve==2.9.2`; both CI audits run without ignores.