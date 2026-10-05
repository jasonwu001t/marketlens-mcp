#!/usr/bin/env bash
# Download Alpaca's current OpenAPI specs into the provider and print their
# sha256, for the routine in ADDING_A_CAPABILITY.md. Run by hand (make
# sync-alpaca-specs); never in CI. It changes tracked files: review the diff,
# classify every change, then update the pinned hashes in
# tests/alpaca/test_parity.py deliberately.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
specs="$here/../src/marketlens_mcp/providers/alpaca/specs"
base="https://docs.alpaca.markets/openapi"

for name in trading-api.json market-data-api.json; do
  tmp="$(mktemp)"
  curl --fail --silent --show-error --location --proto '=https' "$base/$name" -o "$tmp"
  python3 -c 'import json, sys; json.load(open(sys.argv[1]))' "$tmp"  # refuse anything that is not JSON
  mv "$tmp" "$specs/$name"
done

echo "Specs written to $specs. sha256 (pin these in tests/alpaca/test_parity.py):"
if command -v sha256sum >/dev/null 2>&1; then
  (cd "$specs" && sha256sum trading-api.json market-data-api.json)
else
  (cd "$specs" && shasum -a 256 trading-api.json market-data-api.json)
fi
echo "Next: git diff $specs, then follow ADDING_A_CAPABILITY.md step 2."
