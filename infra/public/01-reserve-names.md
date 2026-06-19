# 01 - Reserve names on PyPI and npm

Claim ownership of `aurigin-protos` on PyPI and the `@aurigin` scope
on npmjs.com **before** wiring up the publish workflows in steps
02 / 03. Squatting on these names is cheap for an attacker
(seconds + a free account) and irreversible for us — once a name is
taken, our only option is the per-registry name-dispute process,
which is slow and not guaranteed.

**Verified available at runbook authoring time** (re-check before
executing):

```bash
curl -sI https://pypi.org/pypi/aurigin-protos/json | head -1
# Expect: HTTP/2 404 - name is free
curl -sI https://registry.npmjs.org/@aurigin%2fprotos | head -1
# Expect: HTTP/2 404 - name is free
```

**Idempotent:** Yes — re-running these steps after the first time is
either a no-op or a confirmation of ownership.

## Prerequisites

- An email address you control that you're willing to bind to an
  external account (PyPI and npm both verify by email).
- A TOTP authenticator app (PyPI **requires** 2FA for upload; npm
  strongly recommends it). Use a shared org password manager so the
  TOTP seed isn't tied to one person.

## A. PyPI side

PyPI has no "reserve a name without publishing" API — a project on
PyPI is implicitly created on its first successful upload. Two ways
to claim the name:

### A1. Recommended: Pending Publisher (no first publish needed)

PyPI's [Pending Publishers](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)
feature lets you pre-configure a Trusted Publisher binding for a
project that does not yet exist. On the first OIDC upload, PyPI
creates the project and atomically assigns ownership to your account.

This means the "name reservation" actually happens in step 02 (where
the Pending Publisher is configured), and step 01 reduces to just
**creating the PyPI account that will own the project**.

What to do here:

1. Sign up at https://pypi.org/account/register/. Use a shared
   org-controlled email (e.g. `pypi-publisher@aurigin.ai`) so the
   account survives individual offboarding.
2. Verify the email.
3. Enable 2FA (TOTP). Required for any upload as of 2024.
4. Record the username — step 02 needs it (the Pending Publisher is
   created under that account).

Skip to "B. npm side". The actual name claim happens via OIDC in
step 02; the first time `publish-pypi.yml` runs, PyPI creates
`aurigin-protos` and binds it to this account.

### A2. Fallback: seed-publish a 0.0.0 placeholder

Only do this if you cannot use A1 (e.g. the Pending Publisher flow
fails for an organisational policy reason). It introduces a static
API token, which we then have to delete after step 02 migrates the
project to OIDC.

```bash
# After signing up and enabling 2FA, generate an upload-scoped API
# token at https://pypi.org/manage/account/token/
export TWINE_USERNAME=__token__
export TWINE_PASSWORD=pypi-<the-token>

# Build a deliberately yanked placeholder:
mkdir -p /tmp/pypi-placeholder/aurigin_protos
cat > /tmp/pypi-placeholder/pyproject.toml <<'EOF'
[build-system]
requires = ["setuptools>=65"]
build-backend = "setuptools.build_meta"

[project]
name = "aurigin-protos"
version = "0.0.0"
description = "Placeholder - real releases via the Aurigin-ai/aurigin-protos GitHub repo"
requires-python = ">=3.10"
EOF
touch /tmp/pypi-placeholder/aurigin_protos/__init__.py

(cd /tmp/pypi-placeholder && uv build && uvx twine upload dist/*)

# Immediately yank so it never appears in pip resolution:
#   https://pypi.org/manage/project/aurigin-protos/release/0.0.0/
# Click "Options" -> "Yank".

# Then in step 02, switch this project from API-token publishing to
# Trusted Publisher, and DELETE the token at
# https://pypi.org/manage/account/token/
```

## B. npm side

npm's model is the inverse of PyPI: **scopes are explicitly owned by
organisations**, and creating the org reserves the scope immediately.
No placeholder publish needed.

### B1. Create the `@aurigin` organisation

1. Sign in / sign up at https://www.npmjs.com/. Use the same
   shared org-controlled email as PyPI for symmetry.
2. Enable 2FA on the account (Settings -> Account -> Two-Factor
   Authentication; pick "Authorization and writes").
3. Go to https://www.npmjs.com/org/create.
4. **Organisation name:** `aurigin`.
5. **Plan:** Free (sufficient for public packages — paid plans are
   only required for private packages on npm).
6. Add at least one additional org owner (so the org doesn't lock
   to a single account).

The moment the org exists, `@aurigin/*` is reserved — no one else
can publish under that scope.

### B2. Confirm scope ownership

```bash
curl -s https://registry.npmjs.org/-/org/aurigin/user | head -c 400
# Expect a JSON object listing org members. A 404 means the org
# wasn't created (or the name differs).
```

### B3. Defensively reserve `@aurigin-ai`

The org-name variant `aurigin-ai` (matching the GitHub org
`Aurigin-ai`) is the single most likely typosquat target. Hold it
ourselves so no one else can. Free, 2 minutes.

1. Go to https://www.npmjs.com/org/create.
2. **Organisation name:** `aurigin-ai`.
3. **Plan:** Free.
4. Add the same second owner as B1.
5. **Do not publish anything under this scope.** It exists purely
   to deny the name to a squatter. The real publishing scope is
   `@aurigin` (B1).
6. Optionally, on the org's profile page
   (https://www.npmjs.com/settings/aurigin-ai/profile), add a
   one-line description pointing readers at the canonical scope:
   `Reserved. Canonical Aurigin packages live under @aurigin.`

### B4. Confirm both scopes

```bash
for org in aurigin aurigin-ai; do
  echo "=== @${org} ==="
  curl -s "https://registry.npmjs.org/-/org/${org}/user" | head -c 200
  echo
done
```

Both should return JSON listing org members. A 404 on either means
that org wasn't created (or the name differs by case — npm org names
are case-sensitive in URLs).

## C. Defensive names (optional, both registries)

Both PyPI and npm normalise punctuation in names (PyPI normalises
`aurigin_protos`, `aurigin.protos`, and `Aurigin-Protos` all to
`aurigin-protos`), so we don't need to register variants there.

What we **cannot** automatically claim:

| Confusable name | Registry | Action |
|---|---|---|
| `aurigin-proto` (singular) | PyPI / npm | Optionally publish a yanked 0.0.0 placeholder under each. Skip unless we've seen squatting attempts on adjacent names. |
| `aurigin_protos` as an npm package | npm | npm does not normalise underscores in unscoped names. If concerned, publish a placeholder under a single account (no scope) pointing at the real `@aurigin/protos`. |
| `@aurigin-ai/protos` | npm | Already covered by B3 — the `@aurigin-ai` scope is reserved. |

The variants above cost ~5 min each and are mostly cargo cult unless
squatting actually happens. Skip by default — the canonical scope
`@aurigin` (B1) plus the lookalike scope `@aurigin-ai` (B3) cover
the realistic attack surface.

## Verify

```bash
# PyPI account exists, 2FA on:
#   Visit https://pypi.org/manage/account/ - "Account email"
#   shows verified, "Two factor authentication" shows enabled.

# Both npm orgs exist and you're an owner of each:
for org in aurigin aurigin-ai; do
  echo "=== @${org} ==="
  npm org ls "${org}"
done
# Expect: each org lists members including your username with role 'owner'.

# Scope ownership probe (both should be 404 — scopes are reserved at
# the org level, not via published packages; first real publish in
# step 03 binds the package):
curl -sI https://registry.npmjs.org/@aurigin%2fanything    | head -1
curl -sI https://registry.npmjs.org/@aurigin-ai%2fanything | head -1
```

## Common pitfalls

- **PyPI 2FA gotchas.** PyPI requires 2FA for **upload** as of 2024
  but not for account creation. Without 2FA enabled, step 02's first
  workflow run will fail with a confusing 403. Enable 2FA before
  starting step 02.
- **npm org name vs scope name.** They're the same string. If you
  create the org `aurigin`, the scope is `@aurigin`. There is no
  way to have an `aurigin` org publish into `@aurigin-ai`. The
  `@aurigin-ai` scope from B3 is a **separate org** — never publish
  anything under it; that defeats the defensive-hold purpose.
- **Free npm org cap.** Free org plans on npm cap at unlimited
  public packages but **zero private** packages. We only need public
  here, so free is fine.
- **Email is identity.** Both registries treat the verified email as
  the account-recovery root of trust. Use a shared mailbox that
  survives individual departures.
- **A2 placeholder must be yanked, not deleted.** PyPI's "delete
  release" is irreversible and frees the version number for reuse,
  which is precisely the opposite of what we want. "Yank" hides the
  version from resolution but preserves the URL.
- **Don't skip ahead to step 02 without doing this.** If you
  configure a Pending Publisher in step 02 first and *then* discover
  the name is taken, you've wasted the configuration. The 30-second
  `curl` probe at the top of this file is the only check that
  matters.
