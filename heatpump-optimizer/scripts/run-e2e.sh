#!/usr/bin/env bash
# Run the full E2E test suite (backend + frontend).
# Requires Docker for test database/redis.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== Starting test infrastructure ==="
docker compose -f docker-compose.test.yml up -d --wait

echo ""
echo "=== Running backend E2E tests ==="
DATABASE_URL="postgresql+asyncpg://heatpump:heatpump_test@localhost:5433/heatpump_test" \
REDIS_URL="redis://localhost:6380/0" \
python -m pytest tests/e2e/ -v --tb=short "$@"

BACKEND_EXIT=$?

echo ""
echo "=== Running frontend E2E tests ==="
cd web
npx playwright test --reporter=list
FRONTEND_EXIT=$?

cd "$SCRIPT_DIR"

echo ""
echo "=== Stopping test infrastructure ==="
docker compose -f docker-compose.test.yml down

if [ $BACKEND_EXIT -ne 0 ] || [ $FRONTEND_EXIT -ne 0 ]; then
    echo ""
    echo "❌ Some tests failed (backend=$BACKEND_EXIT, frontend=$FRONTEND_EXIT)"
    exit 1
fi

echo ""
echo "✅ All E2E tests passed"
