#!/usr/bin/env bash
# Publish the TypeScript package to GitHub Packages.
#
# GitHub Packages npm registry requires the package scope to match the
# repo owner. The source `package.json` is `@aurigin/protos` (CodeArtifact-
# friendly), so we rewrite it to `@<owner>/protos` at publish time.
#
# Required env:
#   GITHUB_TOKEN  PAT or workflow token with `write:packages`
#   GITHUB_REPO   "<owner>/<repo>", e.g. "Aurigin-ai/aurigin-protos"
#   GITHUB_TAG    Release tag, e.g. "v0.1.0"

set -euo pipefail

: "${GITHUB_TOKEN:?Set GITHUB_TOKEN}"
: "${GITHUB_REPO:?Set GITHUB_REPO, e.g. Aurigin-ai/aurigin-protos}"
: "${GITHUB_TAG:?Set GITHUB_TAG, e.g. v0.1.0}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/gen/ts"

OWNER_LC=$(echo "${GITHUB_REPO%%/*}" | tr '[:upper:]' '[:lower:]')
VERSION="${GITHUB_TAG#v}"

# Snapshot package.json so we can revert after publish.
cp package.json package.json.bak
trap 'mv package.json.bak package.json' EXIT

npm pkg set name="@${OWNER_LC}/protos"
npm pkg set version="${VERSION}"
npm pkg set publishConfig.registry="https://npm.pkg.github.com"

# Auth via per-call .npmrc (created in this dir, removed on exit).
cat > .npmrc <<EOF
//npm.pkg.github.com/:_authToken=${GITHUB_TOKEN}
@${OWNER_LC}:registry=https://npm.pkg.github.com
EOF
trap 'mv package.json.bak package.json; rm -f .npmrc' EXIT

npm run build
npm publish
