#!/usr/bin/env bash
# Runs the test suites the PM cares about and saves human-readable artifacts
# that the slide deck and notebook reference. Safe to re-run.
#
# Usage:  bash demo/run_tests.sh
#
# Skips the opt-in real-e2e suite unless RUN_REAL_E2E=1 is set, because that
# tier requires live Atlas + S3 credentials.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
ART="$HERE/artifacts"
mkdir -p "$ART"

cd "$ROOT"

echo "==> Unit + integration + (mocked) e2e with coverage"
pytest \
    tests/unit tests/integration tests/e2e \
    --ignore=tests/e2e/test_real_e2e.py \
    --cov=deepagents_mongodb_fs \
    --cov-report=term \
    --cov-report=xml:"$ART/coverage.xml" \
    -q | tee "$ART/test_summary.txt"

echo
echo "==> Coverage headline"
python - <<'PY' | tee "$ART/coverage_summary.txt"
import xml.etree.ElementTree as ET
from pathlib import Path
root = ET.parse(Path("demo/artifacts/coverage.xml")).getroot()
rate = float(root.attrib.get("line-rate", "0")) * 100
print(f"Line coverage: {rate:.1f}%")
PY

if [[ "${RUN_REAL_E2E:-0}" == "1" ]]; then
    echo
    echo "==> Real-e2e suite (live Atlas + S3, with Mongo command logging)"
    MONGO_LOG_FILE="$ART/mongo_e2e.log" \
        pytest tests/e2e/test_real_e2e.py -m real_e2e -v -s \
        | tee "$ART/real_e2e_summary.txt"

    # Excerpt for the slides (first 80 lines is enough to show shape)
    if [[ -f "$ART/mongo_e2e.log" ]]; then
        head -n 80 "$ART/mongo_e2e.log" > "$ART/mongo_e2e_excerpt.log" || true
        echo "Mongo log excerpt → $ART/mongo_e2e_excerpt.log"
    fi
else
    echo
    echo "(Skipping real-e2e. Set RUN_REAL_E2E=1 to include.)"
fi

echo
echo "Done. Artifacts:"
ls -1 "$ART"
