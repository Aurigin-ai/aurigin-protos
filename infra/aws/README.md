# AWS infrastructure setup (internal CodeArtifact channel)

Step-by-step runbooks for the AWS-side prerequisites that
`aurigin-protos` relies on for its **internal** publish channel. The
existing `.github/workflows/publish-codeartifact.yml` workflow
depends on every resource described here.

For the **public** publish channel (pypi.org + npmjs.com), see
[`../public/`](../public/README.md). The two channels are
complementary, not alternatives — see "Two-channel model" below.

Think of this directory as a **restore manual**: if everything in
AWS were lost tomorrow, running these steps in order in fresh
accounts would put it back. Each file is a single, atomic operation
with the exact CLI commands, a "verify it worked" check, and notes
on common pitfalls.

## What this repo publishes

`aurigin-protos` ships two artifacts from the same generated
sources, both via the internal CodeArtifact channel:

| Artifact | Registry | Package coordinate |
|---|---|---|
| `aurigin-protos` (PyPI wheel) | AWS CodeArtifact (`pypi` format) | `aurigin-protos` |
| `@aurigin/protos` (npm tarball) | AWS CodeArtifact (`npm` format) | `@aurigin/protos` |

## Two-channel model (A3 hybrid)

```
gh workflow run release.yml -f version=1.2.3
   │
   │  tags main as v1.2.3, creates a GitHub Release,
   │  then dispatches all three publishers below.
   │
   ├─► publish-codeartifact.yml (manual dispatch, this directory's runbooks)
   │      → CodeArtifact (internal)
   │      → consumed by internal repos: aurigin-router, backend-app, ...
   │
   └─► publish-pypi.yml + publish-npm.yml (manual dispatch, ../public/ runbooks)
          → pypi.org + npmjs.com (external)
          → consumed by anyone with pip/npm
```

CodeArtifact is the **release-candidate lane**: every tag lands here
first, internal CI consumes from here, broken versions never have
to reach public users. The public registries are the **promote lane**:
a human clicks "Run workflow" only when a CA version has proved good.

## Account model

`aurigin-protos` uses a **single-account** publish setup: the
**`shared`** account holds the CodeArtifact domain + repository and
the publisher IAM role that GitHub Actions assumes to upload both
artifacts.

| Account | Purpose |
|---|---|
| `shared` | Holds the **CodeArtifact** domain + repository for both `aurigin-protos` (PyPI) and `@aurigin/protos` (npm), plus the **publisher IAM role** that GitHub Actions assumes to publish. |

This is the same CodeArtifact domain that `softbinding` (and any
future Aurigin Python/JS package) publishes into.

## Environment model

`aurigin-protos` is library code — no `dev` / `prod` runtime split.
Releases are cut from `main` via semver-tagged commits (`vX.Y.Z`).

| Branch / trigger | Effect |
|---|---|
| Push to `main` | No publish; CI lint/test only. |
| Manual workflow dispatch (`release.yml`) | Tags `main` as `vX.Y.Z`, creates a GitHub Release with auto-generated notes, and dispatches `publish-codeartifact.yml` + `publish-pypi.yml` + `publish-npm.yml` for the same version. |
| Manual workflow dispatch (`publish-codeartifact.yml`) | Publishes (or re-publishes) `aurigin-protos==X.Y.Z` and `@aurigin/protos@X.Y.Z` to CodeArtifact. Idempotent. |
| Manual workflow dispatch (`publish-pypi.yml` / `publish-npm.yml`, see `../public/`) | Promotes a CA-resident version to pypi.org / npmjs.com. |

## Order of operations

Run these in order on a fresh setup. Steps are idempotent — re-running
should be a no-op if the resource already exists.

### Shared account (publish-side)

| # | Step | Why |
|---|---|---|
| [01](01-oidc-provider-shared.md) | Create the GitHub Actions OIDC provider | One-time per AWS account; lets GitHub workflows assume IAM roles via short-lived OIDC tokens, no static keys. Likely already exists from another Aurigin repo's setup. |
| [02](02-publisher-role-and-codeartifact.md) | Create the publisher role + CodeArtifact domain/repo | The role the publish workflow assumes to upload both wheel and tarball. CodeArtifact domain + repository shared across all Aurigin language packages. |

## Conventions

- All commands assume you've configured the AWS CLI for the target
  account (`aws sts get-caller-identity` confirms which account).
- Region is set per command — CodeArtifact lives in `eu-west-1`.
- Placeholders use `<angle-brackets>`. Replace before pasting.
- **Shared variables persist via `/tmp/aurigin-protos-<scope>.env`**.
  Each step that creates persistent state writes a sourceable file.
  `<ACCOUNT>_ID` is deliberately re-derived from
  `aws sts get-caller-identity` each time so you can't accidentally
  operate on the wrong account.
- **AWS resource descriptions use plain ASCII only** — no em-dashes
  or smart quotes. AWS validates with regex
  `[\u0009\u000A\u000D\u0020-\u007E\u00A1-\u00FF]*` and rejects
  anything in the Unicode General Punctuation block.
- **Clean up at the end of each step** — `/tmp/` scratch files are
  removed once values are recorded in GitHub Settings.

## What lives where (after each step)

The values produced by these steps end up in GitHub Settings:

| Step | Produces | Lands in GitHub as |
|---|---|---|
| 01 | OIDC provider ARN (shared) | Trust-policy reference; no GitHub config. |
| 02 | Publisher role ARN | `secrets.AWS_ROLE_TO_ASSUME` (repo secret) |
| 02 | CodeArtifact coordinates | `vars.AWS_REGION`, `vars.AURIGIN_CA_DOMAIN`, `vars.AURIGIN_CA_DOMAIN_OWNER`, `vars.AURIGIN_CA_REPO` (repo variables) |

These names match `.github/workflows/publish-codeartifact.yml`
exactly — the workflow has been live for some time, so step 02
mostly documents what already exists rather than creating from
scratch.

Non-sensitive values use `vars.*`. Only the publisher role ARN is a
secret (and that's by long-standing GitHub convention, not because
the ARN itself is sensitive).

## Out of scope (for now)

- **Per-environment accounts** — library publish has no runtime
  split.
- **Cosign signing** of CA artifacts — the public channel
  (`../public/`) handles supply-chain attestations via npm
  `--provenance` and PyPI PEP 740. Internal artifacts re-use the
  same signed builds when promoted.
- **Multi-region CodeArtifact** — single region (`eu-west-1`).
- **Migrating away from CodeArtifact** — see `../public/README.md`
  for why we keep both channels.
