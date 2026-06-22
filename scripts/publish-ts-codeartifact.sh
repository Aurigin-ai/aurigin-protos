#!/usr/bin/env bash
# Publish @aurigin/protos to AWS CodeArtifact.
#
# Required env (set these before running, or export from your shell):
#   AURIGIN_CA_DOMAIN          CodeArtifact domain name
#   AURIGIN_CA_DOMAIN_OWNER    AWS account ID that owns the domain
#   AURIGIN_CA_REPO            CodeArtifact repository name
#   AWS_REGION                 AWS region of the domain (e.g. eu-west-1)
#   AWS_PROFILE                (optional) AWS CLI profile to use

set -euo pipefail

: "${AURIGIN_CA_DOMAIN:?Set AURIGIN_CA_DOMAIN}"
: "${AURIGIN_CA_DOMAIN_OWNER:?Set AURIGIN_CA_DOMAIN_OWNER (AWS account ID)}"
: "${AURIGIN_CA_REPO:?Set AURIGIN_CA_REPO}"
: "${AWS_REGION:?Set AWS_REGION}"

aws codeartifact login \
  --tool npm \
  --domain "$AURIGIN_CA_DOMAIN" \
  --domain-owner "$AURIGIN_CA_DOMAIN_OWNER" \
  --repository "$AURIGIN_CA_REPO" \
  --region "$AWS_REGION"

cd "$(dirname "$0")/../gen/ts"
npm publish
