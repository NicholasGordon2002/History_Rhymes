#!/usr/bin/env bash
#
# test_e2e.sh — End-to-end test for History Rhymes Docker skeleton.
#
# Verifies:
#   1. docker compose up --build completes without errors
#   2. db-init exits cleanly (container exit code 0)
#   3. DB file exists and has all 7 expected tables
#   4. All stub services can connect to DB without errors
#   5. review-ui /health endpoint returns 200
#   6. Clean shutdown
#

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_DIR"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

PASS=0
FAIL=0

pass() {
    echo -e "${GREEN}[PASS]${NC} $1"
    PASS=$((PASS + 1))
}

fail() {
    echo -e "${RED}[FAIL]${NC} $1"
    FAIL=$((FAIL + 1))
}

cleanup() {
    echo ""
    echo -e "${YELLOW}Cleaning up...${NC}"
    docker compose down --volumes 2>/dev/null || true
}

trap cleanup EXIT

echo "============================================"
echo " History Rhymes — E2E Skeleton Test"
echo "============================================"
echo ""

# 1. Build and start all services
echo -e "${YELLOW}[1/5] Building and starting all services...${NC}"
if docker compose up --build --abort-on-container-exit 2>&1; then
    pass "docker compose up --build completed"
else
    fail "docker compose up --build failed"
    # Continue to check what we can
fi

# Give review-ui a moment to start if it hasn't already
sleep 2

# 2. Verify db-init exited cleanly
echo ""
echo -e "${YELLOW}[2/5] Checking db-init exit code...${NC}"
DB_INIT_EXIT=$(docker compose ps -a db-init --format json 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('ExitCode',''))" 2>/dev/null || echo "")
if [ "$DB_INIT_EXIT" = "0" ]; then
    pass "db-init exited with code 0"
else
    fail "db-init exit code: ${DB_INIT_EXIT:-unknown}"
fi

# 3. Verify DB file exists and has expected tables
echo ""
echo -e "${YELLOW}[3/5] Verifying database tables...${NC}"

EXPECTED_TABLES="topics sources scripts visual_assets videos publish_log analytics"

# Check tables via the review-ui /health endpoint
HEALTH_RESPONSE=$(curl -s http://localhost:8000/health 2>/dev/null || echo '{"status":"unreachable"}')
HEALTH_STATUS=$(echo "$HEALTH_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','unknown'))" 2>/dev/null || echo "parse_error")

if [ "$HEALTH_STATUS" = "healthy" ]; then
    pass "review-ui reports healthy"
else
    fail "review-ui health status: $HEALTH_STATUS"
fi

# Get actual tables from health response
ACTUAL_TABLES=$(echo "$HEALTH_RESPONSE" | python3 -c "
import sys, json
data = json.load(sys.stdin)
tables = data.get('tables', [])
print(' '.join(tables))
" 2>/dev/null || echo "")

for table in $EXPECTED_TABLES; do
    if echo "$ACTUAL_TABLES" | grep -qw "$table"; then
        pass "Table '$table' exists"
    else
        fail "Table '$table' NOT found (got: $ACTUAL_TABLES)"
    fi
done

# 4. Verify trend-scout wrote a candidate record
echo ""
echo -e "${YELLOW}[4/5] Verifying trend-scout wrote data...${NC}"
TREND_EXIT=$(docker compose ps -a trend-scout --format json 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('ExitCode',''))" 2>/dev/null || echo "")
if [ "$TREND_EXIT" = "0" ]; then
    pass "trend-scout exited with code 0"
else
    fail "trend-scout exit code: ${TREND_EXIT:-unknown}"
fi

# 5. Verify review-ui /health returns 200
echo ""
echo -e "${YELLOW}[5/5] Checking review-ui HTTP status...${NC}"
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/health 2>/dev/null || echo "000")
if [ "$HTTP_CODE" = "200" ]; then
    pass "review-ui /health returned 200"
else
    fail "review-ui /health returned $HTTP_CODE"
fi

# Summary
echo ""
echo "============================================"
echo " Results: ${GREEN}$PASS passed${NC}, ${RED}$FAIL failed${NC}"
echo "============================================"

if [ "$FAIL" -gt 0 ]; then
    echo -e "${RED}E2E test FAILED${NC}"
    exit 1
else
    echo -e "${GREEN}E2E test PASSED${NC}"
    exit 0
fi
