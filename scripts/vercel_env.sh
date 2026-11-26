#!/usr/bin/env bash
# Push the non-empty values from .env into the linked Vercel project (production + preview).
# Secrets never go into vercel.json (it is committed to a public repo); they live in Vercel's encrypted env store.
# Usage: npm i -g vercel && vercel link && ./scripts/vercel_env.sh
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "No .env file; copy .env.example to .env and fill it in first." >&2; exit 1; }

# DB_PATH is chosen automatically on Vercel (/tmp); everything else is copied as is.
while IFS= read -r line || [ -n "$line" ]; do
  [[ "$line" =~ ^[A-Z_][A-Z0-9_]*= ]] || continue
  key="${line%%=*}"; value="${line#*=}"
  # Same parsing as python-dotenv for simple values: drop surrounding quotes, no inline comments.
  value="${value%\"}"; value="${value#\"}"; value="${value%\'}"; value="${value#\'}"
  [ -n "$value" ] && [ "$key" != "DB_PATH" ] || continue
  for target in production preview; do
    vercel env rm "$key" "$target" --yes >/dev/null 2>&1 || true
    printf '%s' "$value" | vercel env add "$key" "$target" >/dev/null
  done
  echo "set $key"
done < .env
echo "Done. Deploy with: vercel --prod"
