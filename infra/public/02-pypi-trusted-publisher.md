# 02 - Configure PyPI Trusted Publisher

Wire `pypi.org` to accept uploads from the `publish-public.yml`
GitHub Actions workflow via short-lived OIDC tokens. After this step,
no `PYPI_API_TOKEN` or any other static credential exists anywhere —
the trust is purely "this workflow, in this repo, gated by this
environment."

**Where:** pypi.org (web UI).
**Account:** the PyPI account from step [01](01-reserve-names.md).
**Idempotent:** Yes — the form rejects exact duplicates with a clear
error, never creates a second binding.

## Prerequisites

- Step [01](01-reserve-names.md) complete (PyPI account exists, 2FA on).
- The `publish-public.yml` workflow filename agreed (this runbook
  assumes `publish-public.yml`; change consistently below if you pick
  a different name).
- The GitHub Environment `public-release` will exist by the time
  `publish-public.yml` runs (created in step [04](04-public-release-checklist.md)).
  PyPI does not validate the environment name at config time, only at
  upload time, so you can wire this step first.

## A. Configure a Pending Publisher (project does not yet exist)

This is the path when `aurigin-protos` has never been published to
PyPI before — which is the case here. PyPI creates the project on
the first OIDC upload and binds it to the publisher you configure now.

1. Sign in at https://pypi.org.
2. Go to **Account settings -> Publishing**:
   https://pypi.org/manage/account/publishing/
3. Scroll to **"Add a new pending publisher"**.
4. Fill in:

   | Field | Value |
   |---|---|
   | PyPI Project Name | `aurigin-protos` |
   | Owner | `Aurigin-ai` |
   | Repository name | `aurigin-protos` |
   | Workflow name | `publish-public.yml` |
   | Environment name | `public-release` |

5. Click **Add**.

The pending publisher is now live. The very next time
`publish-public.yml` runs in the `public-release` environment on
`Aurigin-ai/aurigin-protos`, PyPI will:

- Validate the OIDC token's `iss` (GitHub), `repository`,
  `workflow_ref`, and `environment` claims against the binding above.
- Create the project `aurigin-protos` if it doesn't exist.
- Assign the configured PyPI account as the project's sole owner.
- Accept the upload.

## B. Configure a Trusted Publisher (project already exists)

Use this path only if you've previously seed-published the project
via A2 in step 01.

1. Sign in at https://pypi.org.
2. Go to **Project -> Manage -> Publishing**:
   https://pypi.org/manage/project/aurigin-protos/settings/publishing/
3. Under **"Add a new publisher"**, select **GitHub**.
4. Fill in the same five fields as in A above.
5. Click **Add**.
6. If you created an API token during the seed publish, **delete it
   now** at https://pypi.org/manage/account/token/. Leaving it
   around is the single biggest supply-chain risk this whole setup
   is meant to eliminate.

## Verify

The only way to truly verify a Trusted Publisher works is to run the
workflow once. Until step 03 + step 04 + `publish-public.yml` are in
place, settle for a config check:

1. Reload https://pypi.org/manage/account/publishing/ — confirm the
   pending publisher row shows all five fields exactly as configured.
2. Reload — confirm the row survives a refresh (PyPI sometimes
   silently rejects malformed input).
3. After `publish-public.yml` exists and runs successfully once,
   re-check https://pypi.org/manage/project/aurigin-protos/settings/publishing/
   — the pending publisher will have been "consumed" and converted
   to a regular Trusted Publisher tied to the now-existing project.

## What `publish-public.yml` needs on the workflow side

For PyPI's OIDC validation to succeed, the publish step in the
workflow needs:

```yaml
permissions:
  id-token: write   # required to mint the OIDC token
  contents: read

jobs:
  publish-pypi:
    runs-on: ubuntu-latest
    environment: public-release    # MUST match the trusted-publisher config
    steps:
      - uses: actions/checkout@v5
        with: { ref: v${{ inputs.version }} }
      - uses: actions/setup-python@v6
        with: { python-version: '3.11' }
      - uses: bufbuild/buf-setup-action@v1
      - run: make generate
      - name: Build sdist + wheel
        working-directory: gen/py
        run: |
          python -m pip install --upgrade build
          python -m build
      - name: Publish to PyPI (Trusted Publisher + PEP 740 attestations)
        uses: pypa/gh-action-pypi-publish@release/v1
        with:
          packages-dir: gen/py/dist
          attestations: true
```

No `password:` / `api-token:` field. The action reads the OIDC token
automatically. `attestations: true` is the supply-chain signal — PyPI
records sigstore-backed PEP 740 attestations alongside the upload,
which downstream installers (pip 24.2+) can verify.

## Common pitfalls

- **Workflow filename mismatch.** PyPI matches on the exact filename,
  not the workflow `name:` field. If the file is `publish-public.yml`,
  the config must say `publish-public.yml`. Rename one without the
  other and uploads fail with "OIDC token claims do not match".
- **Environment claim missing.** If `publish-public.yml` doesn't set
  `environment: public-release` on the job, the OIDC token has no
  `environment` claim and PyPI rejects the upload. Always set it.
- **Wrong repo name capitalisation.** PyPI's claim check is
  case-sensitive on `owner/repo`. Use `Aurigin-ai/aurigin-protos`
  exactly as it appears in the GitHub URL.
- **Pending publisher consumed on the wrong project.** If
  `publish-public.yml` (for whatever reason) uploads a *different*
  project name on its first run, the pending publisher gets consumed
  and bound to that wrong project. PyPI cannot un-bind. Sanity-check
  the wheel filename before the workflow's first dispatch.
- **2FA disabled.** PyPI requires 2FA on the project owner account
  for any upload, including OIDC. Disabling 2FA on the publisher
  account silently breaks all future publishes. Don't.
- **Forked PRs cannot trigger publish.** Trusted Publishers are
  scoped to the source repository's workflow runs. Forks (and PRs
  from forks) cannot mint a valid token, which is the entire
  point — but means you can't dry-run the publish from a fork.
