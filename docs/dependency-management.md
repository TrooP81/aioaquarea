# Dependency Management

Last reviewed: 2026-09-22.

## Authoritative inputs

| Surface | Declaration | Reproducible resolution | CI enforcement |
| --- | --- | --- | --- |
| Library Python | `pyproject.toml` | `Pipfile.lock` for Pipenv development environments | `library-checks.yml` installs from `pyproject.toml`, tests, lint, packages, and runs `pip-audit` |
| Optimizer Python | `heatpump-optimizer/pyproject.toml` | `heatpump-optimizer/constraints.txt` | `optimizer-ci.yml` and `heatpump-optimizer/Dockerfile` install with `-c constraints.txt` |
| Dashboard npm | `heatpump-optimizer/web/package.json` | `heatpump-optimizer/web/package-lock.json` | `npm ci`, production audit, lint, typecheck, build, and Playwright |
| Containers | Dockerfiles and Compose files | Immutable image digests | Compose configuration validation; application and web image builds |
| GitHub Actions | `.github/workflows/*.yml` | Reviewed action references | Dependabot monthly GitHub Actions updates |

`pyproject.toml` remains the publishable library metadata. Pipenv is retained
for reproducible local development on the supported Python 3.10 target; its
lock must be regenerated whenever `Pipfile` changes. The Pipenv graph renderer
can fail on legacy Windows console encoding, but `pipenv lock` and `pipenv
verify` are the supported integrity checks.

## Current audit inventory

- Python: root and optimizer audits are clean after resolving AnyIO through
  FastAPI `0.141.1`, Starlette `1.6.0`, HTTPX `0.28.1`, and AnyIO `4.15.1`.
- SoupSieve: the 2026-09-18 temporary exception is closed. Root metadata and
  Pipenv require `soupsieve>=2.9,<3`; optimizer constraints select `2.9.2`.
- npm: `npm audit --omit=dev --audit-level=high` reports zero vulnerabilities
  with the reviewed lockfile.
- Images: Python, Node, TimescaleDB, and Redis source references use verified
  digests. The TimescaleDB digest is shared by database and backup images so
  `pg_dump` and `pg_restore` stay aligned with the server major.
- Actions and Dependabot: Dependabot monitors root and optimizer pip,
  dashboard npm, optimizer Docker, and GitHub Actions. No unwaived audit
  exception is configured in CI.

## Optimizer lock workflow

`heatpump-optimizer/constraints.txt` is the portable, complete lock artifact.
It pins every runtime and development transitive dependency selected for the
extras used in CI. The optimizer project itself is deliberately not included:
local development, CI, and the image build install it editable from their
checked-out source. The `aioaquarea` VCS dependency remains in
`pyproject.toml`, pinned to a full commit SHA; Docker then replaces it with the
same-revision local source without resolving dependencies again.

`heatpump-optimizer/requirements.lock` is a legacy, non-authoritative resolver
record retained only for historical context. Do not use it for installation or
regeneration. `heatpump-optimizer/constraints.txt` is the sole authoritative
optimizer dependency source consumed by CI and Docker.

From `heatpump-optimizer`, regenerate it in an isolated tool environment:

```powershell
python -m pip install "pip-tools==7.5.0"
python -m piptools compile --all-extras --strip-extras --output-file constraints.txt pyproject.toml
(Get-Content constraints.txt | Where-Object { $_ -notmatch '^aioaquarea\s+@' }) | Set-Content constraints.txt
```

Install the locked development graph with:

```powershell
python -m pip install -c constraints.txt -e ".[all,dev]"
```

Regenerate the optimizer constraints only after reviewing the complete diff and
re-running its tests, migrations, image build, and `pip-audit`.