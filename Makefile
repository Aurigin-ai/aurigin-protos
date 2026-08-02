.PHONY: help info install lint generate breaking clean build-ts build-py \
        publish-ts-codeartifact publish-py-codeartifact publish-codeartifact \
        smoke smoke-py smoke-ts

# Aurigin's CodeArtifact configuration. The literal values live in
# infra/aws/02-publisher-role-and-codeartifact.md (not committed here so
# the public README/Makefile don't leak the AWS account ID). Override
# from a developer shell or a sourced env file:
#
#   export CA_DOMAIN=...
#   export CA_DOMAIN_OWNER=...
#   export CA_REPO=...
#   export CA_REGION=...
#
# The publish-codeartifact.yml workflow reads them from GitHub Actions
# repository variables, which take their values from the same source.
CA_DOMAIN       ?=
CA_DOMAIN_OWNER ?=
CA_REPO         ?=
CA_REGION       ?= eu-west-1

help:
	@echo "Targets:"
	@echo "  info                      Print this repo's CodeArtifact config"
	@echo "  install                   Install ts-proto into gen/ts (one-time / on plugin updates)"
	@echo "  lint                      Run buf lint over proto/"
	@echo "  breaking                  Check breaking changes against main branch"
	@echo "  generate                  Run buf generate -> gen/py + gen/ts/src"
	@echo "  build-ts                  Compile gen/ts -> gen/ts/dist"
	@echo "  build-py                  Build gen/py wheel/sdist"
	@echo ""
	@echo "  smoke                     Run Python + TypeScript example smoke tests"
	@echo "  smoke-py                  Run examples/python end-to-end smoke tests"
	@echo "  smoke-ts                  Run examples/typescript end-to-end smoke tests"
	@echo ""
	@echo "  Publishing — AWS CodeArtifact (internal channel):"
	@echo "    publish-ts-codeartifact Publish @aurigin/protos via npm"
	@echo "    publish-py-codeartifact Publish aurigin-protos via twine"
	@echo "    publish-codeartifact    Both of the above"
	@echo ""
	@echo "  Public PyPI publishing happens via the publish-pypi.yml workflow"
	@echo "  and npm publishing via publish-npm.yml (both manual dispatch only)"
	@echo "  — no make targets."
	@echo ""
	@echo "  clean                     Remove generated and built artifacts"

info:
	@if [ -z "$(CA_DOMAIN)" ] || [ -z "$(CA_DOMAIN_OWNER)" ] || [ -z "$(CA_REPO)" ]; then \
		echo "CodeArtifact coordinates not configured."; \
		echo "Set CA_DOMAIN, CA_DOMAIN_OWNER, CA_REPO, CA_REGION in your environment."; \
		echo "The internal values live in infra/aws/02-publisher-role-and-codeartifact.md."; \
		exit 0; \
	fi
	@echo "AWS CodeArtifact"
	@echo "  Domain         $(CA_DOMAIN)"
	@echo "  Domain owner   $(CA_DOMAIN_OWNER)"
	@echo "  Repository     $(CA_REPO)"
	@echo "  Region         $(CA_REGION)"
	@echo "  npm endpoint   https://$(CA_DOMAIN)-$(CA_DOMAIN_OWNER).d.codeartifact.$(CA_REGION).amazonaws.com/npm/$(CA_REPO)/"
	@echo "  pypi endpoint  https://$(CA_DOMAIN)-$(CA_DOMAIN_OWNER).d.codeartifact.$(CA_REGION).amazonaws.com/pypi/$(CA_REPO)/simple/"
	@echo ""
	@echo "Login one-liners (need AWS credentials in env):"
	@echo "  aws codeartifact login --tool npm  --domain $(CA_DOMAIN) --domain-owner $(CA_DOMAIN_OWNER) --repository $(CA_REPO) --region $(CA_REGION)"
	@echo "  aws codeartifact login --tool pip  --domain $(CA_DOMAIN) --domain-owner $(CA_DOMAIN_OWNER) --repository $(CA_REPO) --region $(CA_REGION)"

install:
	cd gen/ts && npm install

lint:
	buf lint

breaking:
	buf breaking --against '.git#branch=main'

generate: install
	buf generate
	@# Ensure every Python sub-package is importable
	@find gen/py/aurigin gen/py/twilio -type d -exec touch {}/__init__.py \;
	@# Drop the repo LICENSE into each language package so it ships in the
	@# published tarball and PyPI / npm display the right SPDX identifier.
	@cp LICENSE gen/py/LICENSE
	@cp LICENSE gen/ts/LICENSE

build-ts: generate
	cd gen/ts && npm run build

build-py: generate
	cd gen/py && uv build

publish-ts-codeartifact: build-ts
	bash scripts/publish-ts-codeartifact.sh

publish-py-codeartifact: build-py
	bash scripts/publish-py-codeartifact.sh

publish-codeartifact: publish-ts-codeartifact publish-py-codeartifact

# End-to-end example smoke tests. Both language tests spawn the canonical
# Python simulator (examples/simulator/deepfake/) as a subprocess on a
# free port and run the language-specific example client against it,
# asserting the proto + gRPC wire path round-trips. No Docker required.
# Catches breakage from proto renames, stub API shifts, and simulator
# impl regressions before they reach consumers.
#
# The Python target deliberately sidesteps examples/python/pyproject.toml
# (which pins `aurigin-protos` to the CodeArtifact index and would need an
# SSO token). The smoke tests don't exercise the published package — they
# put gen/py on PYTHONPATH and import the locally generated stubs — so we
# run pytest in an ephemeral env with only the deps the example modules
# import directly (grpcio + pyyaml + jsonschema, plus pytest).
smoke: smoke-py smoke-ts

smoke-py: generate
	uv run --no-project --python 3.11 \
	  --with grpcio --with protobuf --with pyyaml --with jsonschema --with pytest \
	  python -m pytest examples/python/tests/ -v

# TS smoke spawns the Python simulator via `uv run --with …` inside the
# test itself — no per-language Python install needed. PYTHONPATH covers
# the generated stubs + the simulator package src/. Requires `uv` on
# PATH (setup-uv action provides it on CI).
#
# Depends on `build-ts` so gen/ts/dist/ exists. We then `npm pack` the
# built package into a tarball and `npm install --no-save` it into the
# example — this side-steps the `file:` symlink pitfall where node's
# ESM resolver follows the symlink to gen/ts/dist/ and can't find
# @grpc/grpc-js because it was hoisted to the consumer's node_modules.
# Tarball installs extract cleanly and deduplicate properly.
smoke-ts: build-ts
	cd gen/ts && npm pack --silent
	cd examples/typescript && npm install --silent \
	  && npm install --no-save --silent ../../gen/ts/aurigin-protos-*.tgz \
	  && npm test

clean:
	rm -rf gen/ts/src gen/ts/dist gen/ts/node_modules
	rm -rf gen/py/aurigin gen/py/dist gen/py/build gen/py/*.egg-info
