# Run the full E2E test suite (backend + frontend).
# Requires Docker for test database/redis.

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location (Split-Path -Parent $ScriptDir)

Write-Host "=== Starting test infrastructure ===" -ForegroundColor Cyan
docker compose -f docker-compose.test.yml up -d --wait

Write-Host ""
Write-Host "=== Running backend E2E tests ===" -ForegroundColor Cyan
$env:DATABASE_URL = "postgresql+asyncpg://heatpump:heatpump_test@localhost:5433/heatpump_test"
$env:REDIS_URL = "redis://localhost:6380/0"
python -m pytest tests/e2e/ -v --tb=short
$BackendExit = $LASTEXITCODE

Write-Host ""
Write-Host "=== Running frontend E2E tests ===" -ForegroundColor Cyan
Push-Location web
npx playwright test --reporter=list
$FrontendExit = $LASTEXITCODE
Pop-Location

Write-Host ""
Write-Host "=== Stopping test infrastructure ===" -ForegroundColor Cyan
docker compose -f docker-compose.test.yml down

if ($BackendExit -ne 0 -or $FrontendExit -ne 0) {
    Write-Host ""
    Write-Host "X Some tests failed (backend=$BackendExit, frontend=$FrontendExit)" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "All E2E tests passed" -ForegroundColor Green
