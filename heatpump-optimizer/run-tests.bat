@echo off
cd /d "%~dp0"

echo === Starting test infrastructure ===
docker compose -f docker-compose.test.yml up -d --wait
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: Failed to start test containers
    exit /b 1
)

echo.
echo === Running backend E2E tests ===
set DATABASE_URL=postgresql+asyncpg://heatpump:heatpump_test@localhost:5433/heatpump_test
set REDIS_URL=redis://localhost:6380
set PRICE_PROVIDER=entsoe
set ENTSOE_TOKEN=test-token
set AQUAREA_USERNAME=test
set AQUAREA_PASSWORD=test

call d:\apps\panasonic\.venv\Scripts\activate.bat
python -m pytest tests/e2e/ -v
set TEST_RESULT=%ERRORLEVEL%

echo.
echo === Stopping test infrastructure ===
docker compose -f docker-compose.test.yml down -v

echo.
if %TEST_RESULT% EQU 0 (
    echo ALL TESTS PASSED
) else (
    echo SOME TESTS FAILED (exit code: %TEST_RESULT%)
)
exit /b %TEST_RESULT%
