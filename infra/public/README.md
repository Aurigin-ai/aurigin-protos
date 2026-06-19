# Public release setup (PyPI + npm)

Step-by-step runbooks for publishing `aurigin-protos` to the **public**
package registries — `pypi.org` for the Python wheel and `npmjs.com`
for the TypeScript scoped package.

This complements the existing `infra/aws/` runbook, which provisions
the **internal** CodeArtifact channel. The two channels coexist:

```
gh workflow run release.yml -f version=1.2.3
   │
   │  tags main as v1.2.3, creates a GitHub Release, then dispatches
   │  all three publish workflows with the same `version` input.
   │
   ├─► publish-codeartifact.yml (manual dispatch, no env gate)
   │      → CodeArtifact: aurigin-protos 1.2.3, @aurigin/protos 1.2.3
   │      → internal smoke test, consumer integration, etc.
   │
   └─► publish-pypi.yml + publish-npm.yml (manual dispatch, run in the
          `public-release` GitHub Environment so the OIDC token carries
          an `environment` claim that matches each Trusted Publisher
          binding — no required reviewers, no approval gate)
          → rebuild at the v1.2.3 tag, attach sigstore provenance / PEP 740
             attestations, upload to pypi.org + npmjs.com
```

CodeArtifact is the **release-candidate** lane; the public channels
are the **promote** lane. A broken or experimental version never
reaches public users because no one dispatches `release.yml` for it.

## Account / identity model

| Identity | Lives in | Purpose |
|---|---|---|
| AWS publisher role (existing) | `shared` AWS account | Internal CodeArtifact publish — see `../aws/`. |
| **PyPI Trusted Publisher** | pypi.org project settings | Lets the `publish-pypi.yml` workflow upload to `pypi.org/project/aurigin-protos` via short-lived OIDC tokens. No static `PYPI_API_TOKEN`. |
| **npm OIDC publisher** | npmjs.com `@aurigin` org settings | Lets `publish-npm.yml` upload `@aurigin/protos` with `--provenance` via short-lived OIDC tokens. No static `NPM_TOKEN`. |
| **GitHub Environment `public-release`** | this repo's settings | Exists solely so the OIDC token carries an `environment` claim that matches the two Trusted Publisher bindings above. No required reviewers — does not gate the workflow. |

No long-lived secrets on either side. The full trust chain is GitHub
OIDC token → PyPI / npm verifies issuer/repo/workflow/environment →
issues short-lived publish credential → uploads complete.

## Trigger model

| Workflow | Trigger | Target |
|---|---|---|
| `release.yml`             | **`workflow_dispatch` only**, with `version` input | tags, GitHub Release, and dispatches the three below |
| `publish-codeartifact.yml` | **`workflow_dispatch` only**, with `version` input | CodeArtifact |
| `publish-pypi.yml`         | **`workflow_dispatch` only**, with `version` input | pypi.org |
| `publish-npm.yml`          | **`workflow_dispatch` only**, with `version` input | npmjs.com |

All four publish/release workflows are deliberately **not tag-triggered**. Every
public release is an intentional click. The workflow:

1. Checks the input `version` against the `v<version>` tag on `main`
   and refuses if absent.
2. Runs in the `public-release` GitHub Environment so the OIDC token
   carries an `environment` claim matching the two Trusted Publisher
   bindings. No required reviewers, no approval gate.
3. Reuses the same `make generate` + build steps as
   `publish-codeartifact.yml`, but uploads to its respective public
   registry instead of CodeArtifact.
4. Attaches provenance:
   - **npm:** built-in `--provenance` flag (sigstore-backed
     attestation of the build environment + git ref).
   - **PyPI:** `attestations: true` on `pypa/gh-action-pypi-publish@release/v1`
     (PEP 740 attestations via sigstore).

## Order of operations

Run these in order. Each step is idempotent; re-running should be a
no-op if the resource already exists.

| # | Step | Why |
|---|---|---|
| [01](01-reserve-names.md) | Reserve / verify ownership of `aurigin-protos` on PyPI and `@aurigin` org on npm | Squatting is cheap; do this before any other public-side work. Also covers what to do if a name is already taken. |
| [02](02-pypi-trusted-publisher.md) | Configure PyPI Trusted Publisher for this repo | One-time per project on pypi.org. Pins issuer = GitHub Actions, repo = `Aurigin-ai/aurigin-protos`, workflow = `publish-pypi.yml`, environment = `public-release`. |
| [03](03-npm-oidc-publisher.md) | Configure npm OIDC publisher + `--provenance` for `@aurigin/protos` | One-time per package on npmjs.com. Same shape as step 02 — issuer/repo/workflow/environment claims. |
| [04](04-public-release-checklist.md) | Pre-flight checklist for repo visibility flip | LICENSE, SECURITY.md, CODEOWNERS, secret-scan history audit, name-squat re-check. Done **once**, before the very first public publish. |

## What lives where (after each step)

| Step | Produces | Where it lives |
|---|---|---|
| 01 | PyPI project ownership, npm scope ownership | pypi.org, npmjs.com (no GitHub config) |
| 02 | Trusted Publisher binding on `pypi.org/manage/project/aurigin-protos` | PyPI side; no secrets in GitHub |
| 03 | OIDC publish config on `npmjs.com/settings/aurigin/packages` | npm side; no secrets in GitHub |
| 04 | `LICENSE`, `SECURITY.md`, `CODEOWNERS`, GitHub Environment `public-release` (no reviewers), branch + tag protection rules | This repo |

Notice: **zero new GitHub repository secrets**. The whole public
publish chain runs on short-lived OIDC tokens. The `public-release`
GitHub Environment is OIDC-claim infrastructure, not a credential
store and not an approval gate.

## Conventions

- Same as `../aws/README.md`: plain ASCII in descriptions, `<angle-brackets>`
  for placeholders, one atomic step per file, idempotent commands.
- Public-side steps mostly happen in **web UIs** (pypi.org,
  npmjs.com, GitHub Settings) rather than CLI, so each runbook
  includes screenshots-worth of click paths instead of `aws` /
  `gh` commands.
- The `publish-pypi.yml` / `publish-npm.yml` workflows themselves are
  **not** in this directory — they live in `.github/workflows/` like
  every other workflow. This directory is the *infrastructure* the
  workflows assume is in place.

## Out of scope (for now)

- **Removing the internal CodeArtifact channel.** Both channels stay.
  CodeArtifact remains the source of truth for internal consumers
  (faster cache, no public-internet dependency, allows pre-release
  versions that never reach pypi.org).
- **Cosign container signing.** No container artifacts here; only
  language packages.
- **SLSA level 3 attestations.** Provenance (level 2) is included
  via npm `--provenance` and PyPI PEP 740 attestations. Going to
  level 3 requires reusable workflows in an isolated repo — defer
  until a downstream consumer actually demands it.
- **Yanking / unpublishing strategy.** PyPI allows yanking (hides
  from `pip install` but URL stays for reproducibility); npm allows
  `npm unpublish` only within 72 hours, deprecate after. Document
  if/when first incident requires it.
