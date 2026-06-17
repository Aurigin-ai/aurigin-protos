# 02 - Publisher role + CodeArtifact (shared account)

The IAM role the `publish-codeartifact.yml` workflow assumes to
upload both the Python wheel and the npm tarball to the shared
CodeArtifact repository. Plus the CodeArtifact domain + repository
themselves.

This runbook is **mostly documentation** — the role, domain, and
repository already exist in the shared account (the workflow has
been live for some time). Use these commands as the recovery
procedure if the resources are ever lost, and to audit / amend the
IAM policy when adding a new package.

No ECR here — `aurigin-protos` ships language packages, not
container images.

**Account:** `shared`
**Region:** `eu-west-1` (CodeArtifact); IAM is global.
**Idempotent:** No on role creation; use `update-assume-role-policy` /
`put-role-policy` to amend. Domain/repo creation guarded by existence
check.

## Prerequisites

- Step [01](01-oidc-provider-shared.md) complete.
- AWS CLI configured for the **shared** account.
- IAM permissions: `iam:CreateRole`, `iam:PutRolePolicy`,
  `iam:GetRole`, `codeartifact:CreateDomain`,
  `codeartifact:CreateRepository`, `codeartifact:DescribeDomain`,
  `codeartifact:DescribeRepository`.

## Set shared-account shell variables

```bash
cat > /tmp/aurigin-protos-shared.env <<'EOF'
# aurigin-protos - shared-account setup variables.
# Source from each shared-account runbook step:
#   source /tmp/aurigin-protos-shared.env
export SHARED_REGION=eu-west-1                       # CHANGE if different
export AURIGIN_CA_DOMAIN=aurigin-ai-domain           # CHANGE if different
export AURIGIN_CA_REPO=aurigin-shared                # CHANGE if different
export PYPI_PACKAGE_NAME=aurigin-protos
export NPM_SCOPE=aurigin
export NPM_PACKAGE_NAME=protos
export ROLE_NAME=aurigin-protos-gha-publisher
export GITHUB_ORG=Aurigin-ai
export GITHUB_REPO=aurigin-protos
EOF

source /tmp/aurigin-protos-shared.env
export SHARED_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
echo "Operating in shared account: ${SHARED_ACCOUNT_ID}"
```

`AURIGIN_CA_DOMAIN` / `AURIGIN_CA_REPO` are the existing live values
(visible in `gen/py/README.md` and `gen/ts/README.md`). Mirror them
exactly so the wheels land in the same domain/repository every other
Aurigin package uses.

## Trust policy

Scope role assumption to GitHub Actions workflows in this repo only.

```bash
cat > /tmp/publisher-trust-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {
      "Federated": "arn:aws:iam::${SHARED_ACCOUNT_ID}:oidc-provider/token.actions.githubusercontent.com"
    },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
      },
      "StringLike": {
        "token.actions.githubusercontent.com:sub": "repo:${GITHUB_ORG}/${GITHUB_REPO}:*"
      }
    }
  }]
}
EOF
```

## Permissions policy

CodeArtifact publish + read-back, scoped to the one repository and
the two package coordinates (one PyPI, one scoped npm).

```bash
cat > /tmp/publisher-permissions-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "StsBearerToken",
      "Effect": "Allow",
      "Action": "sts:GetServiceBearerToken",
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "sts:AWSServiceName": "codeartifact.amazonaws.com"
        }
      }
    },
    {
      "Sid": "CodeArtifactDomainAuth",
      "Effect": "Allow",
      "Action": "codeartifact:GetAuthorizationToken",
      "Resource": "arn:aws:codeartifact:${SHARED_REGION}:${SHARED_ACCOUNT_ID}:domain/${AURIGIN_CA_DOMAIN}"
    },
    {
      "Sid": "CodeArtifactRepoEndpoint",
      "Effect": "Allow",
      "Action": [
        "codeartifact:GetRepositoryEndpoint",
        "codeartifact:ReadFromRepository"
      ],
      "Resource": "arn:aws:codeartifact:${SHARED_REGION}:${SHARED_ACCOUNT_ID}:repository/${AURIGIN_CA_DOMAIN}/${AURIGIN_CA_REPO}"
    },
    {
      "Sid": "CodeArtifactPyPiPublish",
      "Effect": "Allow",
      "Action": [
        "codeartifact:PublishPackageVersion",
        "codeartifact:DescribePackageVersion",
        "codeartifact:ListPackageVersions",
        "codeartifact:ReadFromRepository"
      ],
      "Resource": "arn:aws:codeartifact:${SHARED_REGION}:${SHARED_ACCOUNT_ID}:package/${AURIGIN_CA_DOMAIN}/${AURIGIN_CA_REPO}/pypi//${PYPI_PACKAGE_NAME}"
    },
    {
      "Sid": "CodeArtifactNpmPublish",
      "Effect": "Allow",
      "Action": [
        "codeartifact:PublishPackageVersion",
        "codeartifact:DescribePackageVersion",
        "codeartifact:ListPackageVersions",
        "codeartifact:ReadFromRepository"
      ],
      "Resource": "arn:aws:codeartifact:${SHARED_REGION}:${SHARED_ACCOUNT_ID}:package/${AURIGIN_CA_DOMAIN}/${AURIGIN_CA_REPO}/npm/${NPM_SCOPE}/${NPM_PACKAGE_NAME}"
    }
  ]
}
EOF
```

Notes:

- **Two package statements**, one per format. The PyPI ARN has an
  empty `//` between format and name (no namespace); the npm ARN
  uses the scope as namespace (`npm/aurigin/protos`).
- The role can publish/read **only `aurigin-protos` and
  `@aurigin/protos`**. Even though `softbinding` lives in the same
  CodeArtifact repository, this role cannot touch it.
- `sts:GetServiceBearerToken` is required by `aws codeartifact login`
  / the AWS SDK to mint the bearer token twine / npm uses.

## Create the role (skip if it already exists)

```bash
# Check first:
aws iam get-role --role-name "${ROLE_NAME}" 2>&1 || true

# If absent, create:
aws iam create-role \
  --role-name "${ROLE_NAME}" \
  --assume-role-policy-document file:///tmp/publisher-trust-policy.json \
  --description "GitHub Actions OIDC role for aurigin-protos - CodeArtifact publish (pypi + npm)"

aws iam put-role-policy \
  --role-name "${ROLE_NAME}" \
  --policy-name codeartifact-publish \
  --policy-document file:///tmp/publisher-permissions-policy.json

ROLE_ARN=$(aws iam get-role --role-name "${ROLE_NAME}" --query 'Role.Arn' --output text)
echo "Publisher role ARN: ${ROLE_ARN}"
```

## Create the CodeArtifact domain (skip if it already exists)

```bash
# Check first:
aws codeartifact describe-domain \
  --region "${SHARED_REGION}" \
  --domain "${AURIGIN_CA_DOMAIN}" 2>&1 || true

# If absent, create:
aws codeartifact create-domain \
  --region "${SHARED_REGION}" \
  --domain "${AURIGIN_CA_DOMAIN}"
```

## Create the CodeArtifact repository (skip if it already exists)

```bash
# Check first:
aws codeartifact describe-repository \
  --region "${SHARED_REGION}" \
  --domain "${AURIGIN_CA_DOMAIN}" \
  --repository "${AURIGIN_CA_REPO}" 2>&1 || true

# If absent, create with the public PyPI + npm mirrors as upstreams
# so transitive deps resolve through the same endpoint consumers use:
aws codeartifact create-repository \
  --region "${SHARED_REGION}" \
  --domain "${AURIGIN_CA_DOMAIN}" \
  --repository "${AURIGIN_CA_REPO}" \
  --description "Shared Aurigin language packages plus public PyPI / npm mirrors"

# Wire the public PyPI upstream (idempotent):
aws codeartifact associate-external-connection \
  --region "${SHARED_REGION}" \
  --domain "${AURIGIN_CA_DOMAIN}" \
  --repository "${AURIGIN_CA_REPO}" \
  --external-connection public:pypi

# Wire the public npm upstream (idempotent):
aws codeartifact associate-external-connection \
  --region "${SHARED_REGION}" \
  --domain "${AURIGIN_CA_DOMAIN}" \
  --repository "${AURIGIN_CA_REPO}" \
  --external-connection public:npmjs
```

## Push values to GitHub Settings

```bash
echo -n "${ROLE_ARN}" | gh secret set AWS_ROLE_TO_ASSUME \
  --repo "${GITHUB_ORG}/${GITHUB_REPO}"

gh variable set AWS_REGION \
  --body "${SHARED_REGION}" \
  --repo "${GITHUB_ORG}/${GITHUB_REPO}"

gh variable set AURIGIN_CA_DOMAIN \
  --body "${AURIGIN_CA_DOMAIN}" \
  --repo "${GITHUB_ORG}/${GITHUB_REPO}"

gh variable set AURIGIN_CA_DOMAIN_OWNER \
  --body "${SHARED_ACCOUNT_ID}" \
  --repo "${GITHUB_ORG}/${GITHUB_REPO}"

gh variable set AURIGIN_CA_REPO \
  --body "${AURIGIN_CA_REPO}" \
  --repo "${GITHUB_ORG}/${GITHUB_REPO}"
```

The variable names match `.github/workflows/publish-codeartifact.yml`
exactly. The workflow consumes them as `${{ vars.* }}` and
`${{ secrets.AWS_ROLE_TO_ASSUME }}`.

## Verify

```bash
# Role permissions (one Sid per resource scope):
aws iam get-role-policy \
  --role-name "${ROLE_NAME}" \
  --policy-name codeartifact-publish \
  --query 'PolicyDocument.Statement[*].Sid' --output json
# Expect: ["StsBearerToken","CodeArtifactDomainAuth","CodeArtifactRepoEndpoint","CodeArtifactPyPiPublish","CodeArtifactNpmPublish"]

# CodeArtifact domain + repository + upstreams
{
  printf "DOMAIN\tREPOSITORY\tUPSTREAMS\n"
  upstreams=$(aws codeartifact describe-repository \
    --region "${SHARED_REGION}" \
    --domain "${AURIGIN_CA_DOMAIN}" \
    --repository "${AURIGIN_CA_REPO}" \
    --query 'repository.externalConnections[].externalConnectionName' \
    --output text 2>/dev/null || echo "MISSING")
  printf "%s\t%s\t%s\n" "${AURIGIN_CA_DOMAIN}" "${AURIGIN_CA_REPO}" "${upstreams:-none}"
} | column -t -s $'\t'

# End-to-end: trigger publish-codeartifact.yml on a no-op tag and
# verify both formats appear:
#   gh workflow run publish-codeartifact.yml -f version=0.0.0-test
#   aws codeartifact list-package-versions --domain ${AURIGIN_CA_DOMAIN} \
#     --repository ${AURIGIN_CA_REPO} --format pypi --package aurigin-protos
#   aws codeartifact list-package-versions --domain ${AURIGIN_CA_DOMAIN} \
#     --repository ${AURIGIN_CA_REPO} --format npm --namespace aurigin --package protos
```

Expected: domain + repo present, upstreams `public:pypi public:npmjs`
both listed, all five Sids on the role policy.

## Cleanup

```bash
rm -f \
  /tmp/publisher-trust-policy.json \
  /tmp/publisher-permissions-policy.json
# Keep /tmp/aurigin-protos-shared.env around if you're about to add
# more shared-account resources; otherwise remove it too.
```

## Common pitfalls

- **Em-dashes in `--description`.** AWS rejects them with
  `ValidationError`. Use plain ASCII hyphens.
- **PyPI ARN missing the empty namespace `//`.** Format is
  `package/<domain>/<repo>/pypi//<name>` — note the two consecutive
  slashes. Omit one and the permission scopes to nothing; publish
  returns `AccessDenied`.
- **npm ARN namespace = scope without `@`.** Format is
  `package/<domain>/<repo>/npm/<scope-name>/<package-name>`, so
  `@aurigin/protos` becomes `npm/aurigin/protos`. Including the `@`
  in the ARN is the most common typo.
- **`sts:GetServiceBearerToken` denied.** Without the
  `sts:AWSServiceName=codeartifact.amazonaws.com` condition the IAM
  policy can be rejected by SCPs.
- **Adding a new package.** When you add a third package (e.g. when
  `softbinding` joins this repo's role, which it shouldn't — it has
  its own), append a new `CodeArtifact*Publish` statement scoped to
  the new ARN. Don't widen the existing statement to `*` even
  briefly; tighten back is easy to forget.
- **The role's existence today.** If the resources already exist in
  the shared account (likely), these commands will error with
  `EntityAlreadyExists` or similar. That's the success case for
  "documenting what's there." Use `update-assume-role-policy` /
  `put-role-policy` to amend.
