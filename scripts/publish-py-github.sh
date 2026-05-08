#!/usr/bin/env bash
# Build the Python package and upload wheel + sdist to a GitHub Release.
#
# GitHub Packages does NOT have a Python registry, so we ship Python
# artefacts as Release assets instead. Consumers install with:
#
#   pip install https://github.com/<owner>/<repo>/releases/download/v<x.y.z>/aurigin_protos-<x.y.z>-py3-none-any.whl
#
# Required env:
#   GH_TOKEN      GitHub PAT (or rely on `gh auth login` having run)
#   GITHUB_TAG    Release tag, e.g. "v0.1.0"
#   GITHUB_REPO   "<owner>/<repo>", e.g. "Aurigin-ai/aurigin-protos"

set -euo pipefail

: "${GITHUB_TAG:?Set GITHUB_TAG, e.g. v0.1.0}"
: "${GITHUB_REPO:?Set GITHUB_REPO, e.g. Aurigin-ai/aurigin-protos}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/gen/py"

# Stamp the version into pyproject.toml so the wheel filename matches the tag.
VERSION="${GITHUB_TAG#v}"
python -c "
import pathlib, re
p = pathlib.Path('pyproject.toml')
p.write_text(re.sub(r'(?m)^version = .*', f'version = \"$VERSION\"', p.read_text()))
"

python -m build

if gh release view "$GITHUB_TAG" --repo "$GITHUB_REPO" >/dev/null 2>&1; then
  gh release upload "$GITHUB_TAG" dist/* --repo "$GITHUB_REPO" --clobber
else
  gh release create "$GITHUB_TAG" dist/* --repo "$GITHUB_REPO" \
    --title "$GITHUB_TAG" --generate-notes
fi
