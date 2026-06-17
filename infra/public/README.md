# Public release setup (PyPI + npm)

Step-by-step runbooks for publishing `aurigin-protos` to the **public**
package registries — `pypi.org` for the Python wheel and `npmjs.com`
for the TypeScript scoped package.

This complements the existing `infra/aws/` runbook, which provisions
the **internal** CodeArtifact channel. The two channels coexist:

```
git tag v1.2.3 on main
   │
   ├─► publish-codeartifact.yml (auto)
   │      → CodeArtifact: aurigin-protos 1.2.3, @aurigin/protos 1.2.3
   │      → internal smoke test, consumer integration, etc.
   │
   └─► (later, after manual verification)
          publish-public.yml (manual dispatch, gated by `public-release` Environment)
             → input: version=1.2.3 (must already exist in CodeArtifact)
             → rebuilds at the v1.2.3 tag, signs with sigstore,
                attaches provenance, uploads to pypi.org + npmjs.com
```

The internal channel is the **release candidate** lane; the public
channel is the **promote** lane. A broken or experimental version
never reaches public users because no one ever clicks the dispatch
button for it.

## Account / identity model

| Identity | Lives in | Purpose |
|---|---|---|
| AWS publisher role (existing) | `shared` AWS account | Internal CodeArtifact publish — see `../aws/`. |
| **PyPI Trusted Publisher** | pypi.org project settings | Lets the `publish-public.yml` workflow upload to `pypi.org/project/aurigin-protos` via short-lived OIDC tokens. No static `PYPI_API_TOKEN`. |
| **npm OIDC publisher** | npmjs.com `@aurigin` org settings | Lets the same workflow upload `@aurigin/protos` with `--provenance` via short-lived OIDC tokens. No static `NPM_TOKEN`. |
| **GitHub Environment `public-release`** | this repo's settings | Required-reviewers gate on the public publish. The OIDC tokens above are only issued once a reviewer approves the manual dispatch. |

No long-lived secrets on either side. The full trust chain is GitHub
OIDC token → PyPI / npm verifies issuer/repo/workflow/environment →
issues short-lived publish credential → uploads complete.

## Trigger model

| Workflow | Trigger | Target |
|---|---|---|
| `publish-codeartifact.yml` (existing, unchanged) | Push `v*` tag on `main` | CodeArtifact |
| `publish-public.yml` (NEW) | **`workflow_dispatch` only**, with `version` input | pypi.org + npmjs.com |

`publish-public.yml` is deliberately **not tag-triggered**. Every
public release is an intentional click. The workflow:

1. Checks the input `version` against the `v<version>` tag on `main`
   and refuses if absent.
2. Pauses on the `public-release` Environment for reviewer approval.
3. Reuses the same `make generate` + build steps as
   `publish-codeartifact.yml`, but uploads to public registries
   instead of CodeArtifact.
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
| [02](02-pypi-trusted-publisher.md) | Configure PyPI Trusted Publisher for this repo | One-time per project on pypi.org. Pins issuer = GitHub Actions, repo = `Aurigin-ai/aurigin-protos`, workflow = `publish-public.yml`, environment = `public-release`. |
| [03](03-npm-oidc-publisher.md) | Configure npm OIDC publisher + `--provenance` for `@aurigin/protos` | One-time per package on npmjs.com. Same shape as step 02 — issuer/repo/workflow/environment claims. |
| [04](04-public-release-checklist.md) | Pre-flight checklist for repo visibility flip | LICENSE, SECURITY.md, CODEOWNERS, secret-scan history audit, name-squat re-check. Done **once**, before the very first public publish. |

## What lives where (after each step)

| Step | Produces | Where it lives |
|---|---|---|
| 01 | PyPI project ownership, npm scope ownership | pypi.org, npmjs.com (no GitHub config) |
| 02 | Trusted Publisher binding on `pypi.org/manage/project/aurigin-protos` | PyPI side; no secrets in GitHub |
| 03 | OIDC publish config on `npmjs.com/settings/aurigin/packages` | npm side; no secrets in GitHub |
| 04 | `LICENSE`, `SECURITY.md`, `CODEOWNERS`, GitHub Environment `public-release` with reviewers | This repo |

Notice: **zero new GitHub repository secrets**. The whole public
publish chain runs on short-lived OIDC tokens. The only secret-like
thing added is the *reviewers list* on the GitHub Environment, which
is access control, not credentials.

## Conventions

- Same as `../aws/README.md`: plain ASCII in descriptions, `<angle-brackets>`
  for placeholders, one atomic step per file, idempotent commands.
- Public-side steps mostly happen in **web UIs** (pypi.org,
  npmjs.com, GitHub Settings) rather than CLI, so each runbook
  includes screenshots-worth of click paths instead of `aws` /
  `gh` commands.
- The `publish-public.yml` workflow itself is **not** in this
  directory — it lives in `.github/workflows/` like every other
  workflow. This directory is the *infrastructure* the workflow
  assumes is in place.

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
