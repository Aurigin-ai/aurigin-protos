# 03 - Configure npm OIDC Trusted Publisher + provenance

Wire `npmjs.com` to accept uploads of `@aurigin/protos` from the
`publish-npm.yml` workflow via short-lived OIDC tokens, with
build provenance attached via sigstore. After this step, no
`NPM_TOKEN` or other static credential exists.

**Where:** npmjs.com (web UI) for the trust config; GitHub Actions
for the publish itself.
**Account:** the `@aurigin` org from step [01](01-reserve-names.md).
**Idempotent:** Yes — the npm config form rejects exact duplicates.

## Prerequisites

- Step [01](01-reserve-names.md) complete (`@aurigin` org exists,
  account 2FA on with "Authorization and writes" level).
- npm Trusted Publishers feature available on the org. Generally
  available since 2024; if the **Publishing access** panel below is
  missing, the account is on an old plan — open a support ticket or
  fall back to the granular-token path in **B** below.

## A. Recommended: Trusted Publisher (no token ever exists)

npm's Trusted Publishers mirror PyPI's model — pre-configure the
binding once, then every publish uses a short-lived OIDC token. As of
2025, npm supports configuring a Trusted Publisher for a package that
**does not yet exist**, so the first publish creates the package and
binds it atomically.

1. Sign in at https://www.npmjs.com.
2. Go to **Account settings -> Packages -> Configure publishing**
   (path may also appear under the `aurigin` org settings at
   https://www.npmjs.com/settings/aurigin/packages).
3. Click **"Add a trusted publisher"**.
4. Fill in:

   | Field | Value |
   |---|---|
   | Package name | `@aurigin/protos` |
   | Provider | GitHub Actions |
   | Repository owner | `Aurigin-ai` |
   | Repository name | `aurigin-protos` |
   | Workflow filename | `publish-npm.yml` |
   | Environment | `public-release` |

5. Tick **"Require provenance for publishes from this publisher"**.
   Equivalent to `--provenance` on `npm publish`; attaches a
   sigstore-backed attestation linking the tarball to the build
   environment, commit SHA, and workflow ref.
6. Click **Add publisher**.

## B. Fallback: granular access token (if Trusted Publishers unavailable)

Only do this if **A** is not selectable. Tokens go stale, get leaked,
and require rotation; the whole point of this runbook is to never
have one.

1. Generate a **granular access token** at
   https://www.npmjs.com/settings/<user>/tokens/granular-access-tokens/new:

   - **Token name:** `aurigin-protos-publish-temp`
   - **Expiration:** 30 days (force migration to A within that
     window)
   - **Permissions -> Packages and scopes:**
     - Read and write
     - Restrict to selected: `@aurigin/protos`
   - **Permissions -> Organizations:** No access
   - **Allowed IP ranges:** GitHub Actions runner IPs (use
     `gh api meta --jq .actions[]`) or leave open and accept the
     wider attack surface

2. Store it as a repo secret (no env scoping — the token grants
   full publish, so the env wouldn't add anything):

   ```bash
   gh secret set NPM_TOKEN --body "<token>" \
     --repo Aurigin-ai/aurigin-protos
   ```

3. Calendar a reminder for `expiration - 7 days` to migrate to **A**.
4. Once **A** is configured, delete the token at the URL above and
   remove the secret with `gh secret delete NPM_TOKEN`.

## Verify

As with PyPI, the only true verification is a successful publish.
Until `publish-npm.yml` exists and runs once, check:

1. https://www.npmjs.com/settings/aurigin/packages — the trusted
   publisher row shows the package `@aurigin/protos` and all six
   fields exactly as configured.
2. After first successful publish:
   ```bash
   npm view @aurigin/protos
   # Expect: latest version, dist.tarball URL, dist.integrity
   npm view @aurigin/protos --json | jq '.dist."npm-signature", .dist.attestations'
   # Expect: signature present, attestations object with
   # "provenance" and "publish" entries.
   ```
3. On https://www.npmjs.com/package/@aurigin/protos a green
   **"Built and signed on GitHub Actions"** badge appears next to
   the version.

## What `publish-npm.yml` needs on the workflow side

```yaml
permissions:
  id-token: write   # required by npm OIDC
  contents: read

jobs:
  publish-npm:
    runs-on: ubuntu-latest
    environment: public-release    # MUST match trusted-publisher config
    steps:
      - uses: actions/checkout@v5
        with: { ref: v${{ inputs.version }} }
      - uses: actions/setup-node@v5
        with:
          node-version: '22'
          registry-url: 'https://registry.npmjs.org'   # forces public registry, not CodeArtifact
      - uses: bufbuild/buf-setup-action@v1
      - run: make generate
      - name: Stamp version into gen/ts/package.json
        working-directory: gen/ts
        run: npm pkg set version="${{ inputs.version }}"
      - name: Build TS
        working-directory: gen/ts
        run: |
          npm install --no-audit --no-fund
          npm run build
      - name: Publish to npm with provenance
        working-directory: gen/ts
        run: npm publish --provenance --access public
        # No NODE_AUTH_TOKEN / NPM_TOKEN env - the OIDC token from
        # actions/setup-node + the trusted publisher config is
        # everything npm needs.
```

`--access public` is required on the first publish of a scoped
package; safe (and no-op) on subsequent publishes.

## Common pitfalls

- **`always-auth` from CodeArtifact `.npmrc`.** If a stray
  `.npmrc` left over from CodeArtifact development lives in
  `gen/ts/` (or higher up), `npm publish` may pick up its
  `_authToken=${CODEARTIFACT_AUTH_TOKEN}` line and try to publish
  to CodeArtifact instead of npmjs.com. Add `.npmrc` to the
  workflow's `actions/checkout` cleanup, or ensure
  `actions/setup-node` writes a fresh one (it does, with
  `registry-url`).
- **Provenance requires public registry.** `--provenance` only
  works against `https://registry.npmjs.org`. If `registry-url` is
  missing and the runner inherits a CodeArtifact endpoint, publish
  fails with a confusing "provenance not supported by this registry."
- **`--access public` missing on first publish.** Scoped packages
  default to private. First publish without `--access public` fails
  with `402 Payment Required` (because private requires a paid plan).
- **Workflow filename / environment claim mismatch.** Same as PyPI's
  pitfall — npm matches the exact filename and environment name.
  Rename one without the other and publishes fail with `EOIDC`.
- **2FA level on the org owner.** If the org owner account has 2FA
  level "Authorization only" rather than "Authorization and writes",
  the org's Trusted Publisher config save will refuse with a vague
  permissions error.
- **Provenance + force-pushed branches.** Provenance pins to a
  commit SHA. If the tag `vX.Y.Z` is force-moved to a different
  commit after the first publish, a re-publish records a different
  SHA in the attestation. Downstream verification tools may flag
  this as suspicious. Treat published versions as immutable.
