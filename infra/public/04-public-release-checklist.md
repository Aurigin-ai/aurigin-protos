# 04 - Public release checklist (repo visibility flip)

The one-time pre-flight before the very first `publish-public.yml`
run **and** the repo's visibility flip from Private to Public. Steps
are independent — tick each off in any order — but every box must be
green before either action.

This is a checklist, not a tutorial. Each item links to the
authoritative source if you need detail.

## A. Repo hygiene

- [ ] **`LICENSE` file present and correct.**
  - For a proto library intended to be linked into both internal
    and external code, **Apache 2.0** is the safest default
    (compatible with GPL, includes patent grant). MIT is acceptable
    if simplicity matters more than the patent grant.
  - Verify: `ls LICENSE` returns a file; first line names the
    licence.

- [ ] **`README.md` reflects the public install path.**
  - Show both `pip install aurigin-protos` and
    `npm install @aurigin/protos` as the primary instructions.
  - Move the CodeArtifact-only instructions into a clearly-labelled
    "Internal Aurigin consumers" subsection lower down so external
    readers aren't confused.

- [ ] **`SECURITY.md` present.**
  - State the supported version policy (e.g. "latest minor of the
    current major").
  - State the disclosure address (e.g.
    `security@aurigin.ai`, or a private vuln-disclosure form via
    GitHub Security Advisories).
  - GitHub renders this file at `.../security/policy` automatically.

- [ ] **`CODEOWNERS` present.**
  - At least `* @Aurigin-ai/maintainers` so review approvals route
    to the right humans. Without it, GitHub treats *all* file
    owners as anyone-can-approve once branch protection is on.

- [ ] **`CONTRIBUTING.md` present (or in README).**
  - Concise: how to run `make generate`, run tests, the commit
    message style, the PR review expectation.
  - If you require a CLA, link to it.

- [ ] **`.github/ISSUE_TEMPLATE/` populated** with at least
  `bug_report.md` and `feature_request.md`. Optional but reduces
  triage cost as soon as the repo is public.

## B. Secret-scanning history audit

A private repo lets careless commits go unnoticed. Once public,
GitHub's secret scanner does retroactive sweeps within minutes and
any historic secret is exposed forever (git history is permanent).

- [ ] **Run `gitleaks` or `trufflehog` against full history.**
  ```bash
  # Quick path with no install:
  docker run --rm -v "$PWD":/repo zricethezav/gitleaks:latest detect \
    --source /repo --redact --report-format sarif --report-path /repo/gitleaks.sarif
  ```
  Review every finding. False positives are common (example tokens,
  fixtures); real findings require **history rewrite** (`git filter-repo`)
  *and* secret rotation *before* the visibility flip.

- [ ] **Enable GitHub secret scanning + push protection** in repo
  settings (Settings -> Code security and analysis). Free for public
  repos. Push protection blocks future commits containing detected
  secret formats.

- [ ] **Audit committed CodeArtifact URLs for embedded tokens.** Grep
  for `:_authToken=`, `pypi-`, `npm_`, raw IAM access key formats:
  ```bash
  git log -p --all | grep -E "_authToken=|pypi-[A-Za-z0-9_-]{32,}|AKIA[A-Z0-9]{16}" \
    | head -20
  ```

- [ ] **Audit example files for real credentials.** Especially
  `examples/python/Justfile` and any `.env.example` — confirm they
  use placeholders, not real values.

## C. Branch protection + environments

- [ ] **Branch protection on `main`:**
  - Require PR with at least 1 approving review.
  - Require status checks: `ci.yml` jobs that exist today.
  - Require linear history.
  - Restrict who can push directly (admins-only, or no one).
  - Block force pushes.

- [ ] **GitHub Environment `public-release` created** (Settings ->
  Environments -> New environment).
  - **Required reviewers:** at least 2 people from a small,
    explicit list. This is the manual gate that the public publish
    pauses on.
  - **Deployment branches and tags:** restrict to `main` and tag
    pattern `v*` so the environment can't be used from feature
    branches.
  - **Environment secrets:** none. The whole point of steps 02 + 03
    is that no static secrets exist.

- [ ] **Tag protection rule for `v*`** (Settings -> Tags). Only
  maintainers can push or move release tags.

## D. Pre-flight verification

- [ ] **Re-run name probes** (registries can be claimed at any time):
  ```bash
  curl -sI https://pypi.org/pypi/aurigin-protos/json | head -1   # 404 expected
  curl -sI https://registry.npmjs.org/@aurigin%2fprotos | head -1  # 404 expected
  ```
  If either now returns 200, **stop**. Someone has registered the
  name. Do not flip visibility — investigate, then either contact
  the registrar's name-dispute process or pick a different package
  name.

- [ ] **Dry-run the first public publish on a `0.0.1-rc1`**:
  ```bash
  git tag v0.1.0-rc1 && git push origin v0.1.0-rc1
  # Wait for publish-codeartifact.yml to land in CA, then:
  gh workflow run publish-public.yml -f version=0.1.0-rc1
  ```
  - Approve the `public-release` environment gate.
  - Confirm both packages appear at
    https://pypi.org/project/aurigin-protos/0.1.0rc1/ and
    https://www.npmjs.com/package/@aurigin/protos/v/0.1.0-rc1.
  - Confirm provenance / attestations show on both project pages.
  - Yank/deprecate the rc afterwards (don't leave 0.x.y-rc1 as
    "latest").

- [ ] **External-install smoke test from a clean Docker container**:
  ```bash
  docker run --rm python:3.11 sh -c \
    "pip install aurigin-protos==0.1.0rc1 && python -c 'import aurigin.deepfake_detection.v1.deepfake_detection_pb2 as x; print(x)'"
  docker run --rm node:22  sh -c \
    "npm pack @aurigin/protos@0.1.0-rc1 && tar -tzf aurigin-protos-0.1.0-rc1.tgz | head"
  ```
  No AWS credentials, no `.npmrc` setup — proves the external
  install path actually works.

## E. The flip

Only when every box above is green:

- [ ] **Settings -> General -> Danger Zone -> Change repository
  visibility -> Public.**
- [ ] **Verify GitHub's post-flip checklist** (it shows a summary
  of risks — review each).
- [ ] **Announce internally** before announcing externally. Some
  internal automations may break in subtle ways when the repo is
  public (e.g. workflows that assumed `secrets.GITHUB_TOKEN` had
  access to private metadata that's now public).
- [ ] **Watch CI for 24h.** A public repo gets crawled
  immediately; ensure no workflow is rate-limited or behaving
  differently because of the new traffic shape.

## F. Post-flip ongoing

These aren't pre-flight, but they kick in the moment the repo is
public:

- **Dependabot alerts** auto-enabled. Triage promptly — public
  vulnerabilities draw fast scanning.
- **Code scanning (CodeQL)** worth enabling. Free for public.
- **Sponsorship / discussions / wiki** — turn off anything you're
  not ready to staff.
- **Issue template responses time** — public issues are visible to
  everyone, including potential users evaluating the project. A
  stale issue list signals "abandoned."
- **Yanking strategy.** Document who has authority to yank a PyPI
  release / deprecate an npm version, and the criteria. The
  criteria should be public so users trust the process.

## Common pitfalls

- **Tightening branch protection *after* the flip is harder.**
  Anyone watching the repo when protection lands sees the
  unprotected window. Do it before.
- **`CODEOWNERS` matched against a deleted team.** GitHub silently
  ignores entries pointing at non-existent teams. Verify the team
  `@Aurigin-ai/maintainers` (or whichever you name) actually exists
  before relying on it for review enforcement.
- **PyPI does not allow project deletion**, only yanking. Anything
  you publish — even an accidental rc1 — leaves a permanent URL.
  Treat the first public publish with the same caution as a prod
  release.
- **npm deprecation is not unpublish.** `npm unpublish` only works
  within 72 hours of first publish. After that, only `npm deprecate`
  (which adds a warning to installers but doesn't remove the
  tarball).
- **Skipping the rc dry-run.** First public publish hitting a real
  version number is a one-way door. Use an rc / pre-release to
  exercise the whole pipeline against real registries before the
  first GA.
