.PHONY: help install lint generate breaking clean build-ts build-py \
        publish-ts-codeartifact publish-py-codeartifact publish-codeartifact \
        publish-ts-github publish-py-github publish-github \
        publish

help:
	@echo "Targets:"
	@echo "  install                   Install ts-proto into gen/ts (one-time / on plugin updates)"
	@echo "  lint                      Run buf lint over proto/"
	@echo "  breaking                  Check breaking changes against main branch"
	@echo "  generate                  Run buf generate -> gen/py + gen/ts/src"
	@echo "  build-ts                  Compile gen/ts -> gen/ts/dist"
	@echo "  build-py                  Build gen/py wheel/sdist"
	@echo ""
	@echo "  Publishing — AWS CodeArtifact:"
	@echo "    publish-ts-codeartifact Publish @aurigin/protos via npm"
	@echo "    publish-py-codeartifact Publish aurigin-protos via twine"
	@echo "    publish-codeartifact    Both of the above"
	@echo ""
	@echo "  Publishing — GitHub Packages / Releases:"
	@echo "    publish-ts-github       Publish @<owner>/protos to GitHub Packages (npm)"
	@echo "    publish-py-github       Build wheel + sdist and attach to GitHub Release"
	@echo "    publish-github          Both of the above"
	@echo ""
	@echo "  Publishing — both registries:"
	@echo "    publish                 publish-codeartifact + publish-github"
	@echo ""
	@echo "  clean                     Remove generated and built artifacts"

install:
	cd gen/ts && npm install

lint:
	buf lint

breaking:
	buf breaking --against '.git#branch=main'

generate: install
	buf generate
	# Ensure every Python sub-package is importable
	@find gen/py/aurigin gen/py/twilio -type d -exec touch {}/__init__.py \;

build-ts: generate
	cd gen/ts && npm run build

build-py: generate
	cd gen/py && python -m build

publish-ts-codeartifact: build-ts
	bash scripts/publish-ts-codeartifact.sh

publish-py-codeartifact: build-py
	bash scripts/publish-py-codeartifact.sh

publish-codeartifact: publish-ts-codeartifact publish-py-codeartifact

publish-ts-github: build-ts
	bash scripts/publish-ts-github.sh

publish-py-github: build-py
	bash scripts/publish-py-github.sh

publish-github: publish-ts-github publish-py-github

# Publish to BOTH registries. Either side failing aborts; CodeArtifact
# runs first because GitHub Packages is the more visible one and we'd
# rather miss a CodeArtifact publish than a GH Release.
publish: publish-codeartifact publish-github

clean:
	rm -rf gen/ts/src gen/ts/dist gen/ts/node_modules
	rm -rf gen/py/aurigin gen/py/dist gen/py/build gen/py/*.egg-info
