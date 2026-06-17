# 01 - Create the GitHub Actions OIDC provider (shared account)

Establishes the trust relationship between GitHub Actions and AWS so
workflows can assume IAM roles via short-lived OIDC tokens instead of
long-lived access keys.

**Account:** `shared`
**Region:** N/A (IAM is global)
**Idempotent:** Yes — `aws iam create-open-id-connect-provider` errors
on second run with `EntityAlreadyExists`; the check below catches it
first.

## Prerequisites

- AWS CLI configured for the **shared** account.
- IAM permissions: `iam:CreateOpenIDConnectProvider`,
  `iam:GetOpenIDConnectProvider`, `iam:ListOpenIDConnectProviders`.

## Check whether it already exists

```bash
aws iam list-open-id-connect-providers \
  --query "OpenIDConnectProviderList[?ends_with(Arn, ':oidc-provider/token.actions.githubusercontent.com')].Arn" \
  --output text
```

If the output includes an ARN ending in `/token.actions.githubusercontent.com`,
the provider is already configured (very likely — `aurigin-protos`'s
`publish-codeartifact.yml` has been live for some time and depends on
this provider). Skip to "Capture the ARN".

## Create the provider

```bash
aws iam create-open-id-connect-provider \
  --url       https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com \
  --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1
```

Notes:

- The **URL** is identical for every GitHub-hosted runner; do not change it.
- The **client ID** (`sts.amazonaws.com`) is the audience claim AWS
  expects in the OIDC token.
- The **thumbprint** is the SHA-1 of the Actions OIDC root CA;
  `6938fd4d98bab03faadb97b34396831e3780aea1` is the long-standing one.

## Capture the ARN

```bash
aws iam list-open-id-connect-providers \
  --query "OpenIDConnectProviderList[?ends_with(Arn, ':oidc-provider/token.actions.githubusercontent.com')].Arn" \
  --output text
```

Expected output:

```
arn:aws:iam::<shared-account-id>:oidc-provider/token.actions.githubusercontent.com
```

Record this ARN - step 02 references it in its trust policy.

## Verify

```bash
aws iam get-open-id-connect-provider \
  --open-id-connect-provider-arn arn:aws:iam::<shared-account-id>:oidc-provider/token.actions.githubusercontent.com
```

Expected fields:

- `Url: https://token.actions.githubusercontent.com`
- `ClientIDList: ["sts.amazonaws.com"]`
- `ThumbprintList` populated

## Common pitfalls

- **One provider per account, not per repo.** This is reusable across
  every GitHub repo that needs OIDC into this AWS account. Don't create
  duplicates.
- **Wrong account.** Always confirm with `aws sts get-caller-identity`
  before creating. The shared account is the only one this repo needs.
- **Public channel doesn't use this.** The `../public/` channel
  publishes via OIDC trust between GitHub Actions and pypi.org /
  npmjs.com directly — no AWS hop. This provider only matters for
  the CodeArtifact path.
