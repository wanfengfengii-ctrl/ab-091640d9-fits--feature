#!/bin/sh
# One-shot verification: unit tests -> build -> FITS API smoke test.
# Any failure aborts the script and propagates a non-zero exit code.
set -eu

cd "$(dirname "$0")/.."

PYTHON=$(command -v python || command -v python3)

echo "[verify] 1/3 running unit tests"
"$PYTHON" -m unittest discover -s tests -t . -v

echo "[verify] 2/3 building (byte-compiling all sources)"
"$PYTHON" -m compileall -q fits_cutout tests verify

echo "[verify] 3/3 running FITS API smoke test against ${APP_BASE_URL:-http://127.0.0.1:8000}"
"$PYTHON" verify/smoke.py

echo "[verify] ALL CHECKS PASSED"
