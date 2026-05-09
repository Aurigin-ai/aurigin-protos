# Repo guide for Claude

Guardrails and conventions for working in `aurigin-protos`. Read this before
making changes — it'll stop you re-discovering rules the hard way.

## What this repo is

Source of truth for Aurigin's gRPC service definitions. Generates Python
(`aurigin-protos`) and TypeScript (`@aurigin/protos` on CodeArtifact,
`@<owner>/protos` on GitHub Packages) client/server stubs. Currently exposes
one service: `aurigin.deepfake_detection.v1.DeepfakeDetection.DetectDeepfake`
(bidi streaming), with a vendored `twilio.tme.extensions.common.v1.AudioBuffer`
message used as the audio carrier so we interop with Twilio Media Extensions.

## Layout (where things live)

```
proto/                            # .proto sources (path mirrors package)
gen/{py,ts}/                      # generated stubs + per-language packaging
examples/
  audio/                          # shared .wav fixtures (gitignored except fixtures/)
  audio/fixtures/test_call.wav    # tracked test fixture (1 s 8 kHz mono sine)
  audio/generate-conversation.sh  # ffmpeg helper for FreeSWITCH-style 8 kHz call
  python/                         # Python example server/client + phone-call sim + tests
  typescript/                     # TS example server/client + phone-call sim + tests
scripts/                          # publish-{ts,py}{,-github}.sh
.github/workflows/                # ci, publish (GH Packages), publish-codeartifact
buf.yaml / buf.gen.yaml           # buf v2 config; STANDARD lint with SERVICE_SUFFIX exempted
Makefile                          # lint / generate / build / publish
```

## Hard rules (always follow)

### When adding, deleting, or modifying a `.proto`

1. Run `make lint` and `make generate` locally first — `buf breaking` runs in
   CI against `main`, so wire-incompatible changes will fail. If the change is
   intentionally breaking, say so explicitly in the PR's `## Wire / API impact`
   section.
2. **Always update both example clients to match.** Files to inspect:
   - `examples/python/server.py`, `client.py`, `phone_call.py`
   - `examples/typescript/server.ts`, `client.ts`, `phone_call.ts`
   Even type-only changes (renamed field, added `oneof` arm) need code updates
   in any example that touches the affected message.
3. **Update the smoke tests if assertions reference the changed field/RPC.**
   - `examples/python/tests/test_smoke.py`
   - `examples/typescript/tests/smoke.test.ts`
   The smoke tests assert on stdout regex patterns from the example server's
   responses; if you rename `Session: demo-session-0001` or `📞 Session:`,
   update both sides.
4. Run both test suites end-to-end before committing:
   ```bash
   .venv-smoke/bin/python -m pytest examples/python/tests/ -v
   cd examples/typescript && npm test
   ```

### Reviewing markdown

Whenever you change anything that affects users (file moves, renamed
recipes, new install paths, version bumps, new languages), do a sweep of
**all** `.md` files and verify they still match reality. The set is small:

- `README.md` (top level) — repo overview, layout, prerequisites, publishing
- `gen/py/README.md` — ships in the Python wheel; consumer-facing
- `gen/ts/README.md` — ships in the npm tarball; consumer-facing
- `examples/README.md` — example usage, including phone-call modes
- `.github/pull_request_template.md` — keep tickbox set in sync with reality

Common failure mode: README.md gets updated but `gen/{py,ts}/README.md`
still references old import paths or the wrong owner. Check the deep links
(`https://github.com/<owner>/aurigin-protos`) and import examples.

### Tests for every proto definition

For each service in this repo there must be at least one **end-to-end**
runtime test (server + client roundtrip) for both Python and TypeScript. If
you add a new service:

1. Implement it in `examples/{python,typescript}/server.{py,ts}` with stub
   logic — real ML stays out of this repo.
2. Have at least one example client invoke each RPC.
3. Add an entry in `tests/test_smoke.py` and `tests/smoke.test.ts` that
   spawns the server and asserts the new RPC's response shape.
4. Confirm `make generate && pytest examples/python/tests/ && (cd
   examples/typescript && npm test)` is green before pushing.

The existing `DetectDeepfake` smoke tests are the template — copy that
shape (port via env var, async fixture, regex assertions on stdout).

### Adding a new language

If a downstream service needs C# / Kotlin / Rust / Go: mirror the existing
two-language layout. Don't introduce a divergent structure.

Concretely, for a new language `XYZ`:

1. **`buf.gen.yaml`** — add a buf plugin block alongside the python and
   ts-proto blocks. Prefer remote plugins from `buf.build/` where available.
2. **`gen/xyz/`** — output dir, with packaging metadata next to it
   (`*.csproj` for NuGet, `build.gradle.kts` for Gradle, `Cargo.toml`,
   `go.mod`). Mirror what `gen/py/` and `gen/ts/` do.
3. **`gen/xyz/README.md`** — install instructions for **both** registries
   (CodeArtifact and the language's GitHub Packages support, if any). If
   GitHub Packages doesn't host that ecosystem (e.g., no PyPI), use the
   Release-asset pattern that `gen/py/README.md` documents.
4. **`scripts/publish-xyz.sh`** and **`scripts/publish-xyz-github.sh`** —
   one per registry, following the same env-var convention
   (`AURIGIN_CA_*` for CodeArtifact, `GITHUB_TOKEN` / `GITHUB_REPO` /
   `GITHUB_TAG` for GitHub).
5. **`Makefile`** — add `build-xyz`, `publish-xyz`, `publish-xyz-github`
   targets and update `help`.
6. **`.github/workflows/`** — add a CI job for the new language alongside
   the existing `python` / `typescript` jobs (build + import smoke test +
   end-to-end test). Add the same job to `publish.yml` and
   `publish-codeartifact.yml`.
7. **`examples/xyz/`** — at minimum `server.{ext}` + `client.{ext}`. If the
   language has a sensible streaming idiom, add a `phone_call.{ext}` mirror
   too (file mode + FIFO mode).
8. **Top-level `README.md`** — add the new language to the layout diagram
   and to the consumption section.
9. **Required CI contexts** — after the new job's name is finalised, run the
   `gh api -X PUT .../branches/main/protection` call to add it to the
   required-contexts list (see "Branch protection" below for the existing
   payload).

Don't add a language without all of: codegen, packaging, both publish
targets, CI build, end-to-end test, README.

## CI / branch protection

`main` is protected. Every PR runs:

- `buf lint + breaking + format` (`buf breaking` against `main`)
- `TypeScript build` (gen/ts build, examples tsc --noEmit, **example smoke test**)
- `Python build + import smoke test` (gen/py build, import smoke, **example smoke test**)
- `shellcheck publish scripts`

All four are required contexts. The smoke tests run inside the existing
TypeScript / Python jobs — adding a new test step doesn't require touching
branch protection unless the **job name** changes.

If you ever need to update required contexts, the canonical payload is:

```bash
gh api -X PUT repos/Aurigin-ai/aurigin-protos/branches/main/protection --input - <<'JSON'
{
  "required_status_checks": {
    "strict": true,
    "contexts": [
      "buf lint + breaking + format",
      "TypeScript build",
      "Python build + import smoke test",
      "shellcheck publish scripts"
    ]
  },
  "enforce_admins": false,
  "required_pull_request_reviews": null,
  "restrictions": null
}
JSON
```

## Versioning & publishing

- **Versions live nowhere on `main`.** `gen/ts/package.json` and
  `gen/py/pyproject.toml` stay at `0.0.0`. The publish workflows stamp the
  version from the `v*` tag at publish time.
- Tag format: `v<semver>`, e.g. `v0.1.0`. Tag-push triggers both
  `publish.yml` (GitHub Packages + Release) and `publish-codeartifact.yml`
  (CodeArtifact via OIDC role).
- Published TS scope differs by registry: `@aurigin/protos` on CodeArtifact,
  `@aurigin-ai/protos` on GitHub Packages (the workflow rewrites the scope
  at publish time without touching the source `package.json`).
- Python: the wheel is attached as a **GitHub Release asset** for the
  GitHub publish path (no PyPI registry on GitHub Packages). Install via
  PEP 508 direct-URL.

## Git workflow

- Squash-merge only (other merge styles disabled at the repo level).
- PR title becomes the squashed commit subject; PR body becomes the
  message body. Pick descriptive titles — they're permanent history.
- Branches auto-delete on merge.
- No direct pushes to `main`; branch protection rejects them.
- The PR template (`.github/pull_request_template.md`) has a wire/API
  impact tickbox — fill it in honestly. Reviewers and downstream
  upgraders rely on it.

## Helpful one-liners

```bash
# Full local check before pushing
make lint && make generate && \
  (cd gen/ts && npm run build) && \
  (cd gen/py && python -m build) && \
  pytest examples/python/tests/ && \
  (cd examples/typescript && npm test)

# Re-run the example server + client locally (against an existing server)
PYTHONPATH=gen/py:examples/python python examples/python/client.py
cd examples/typescript && npm run client

# Stream the test fixture through phone_call as a sanity check
PYTHONPATH=gen/py:examples/python python examples/python/phone_call.py \
  --audio examples/audio/fixtures/test_call.wav --duration 1
```

## What NOT to do

- Don't commit real customer audio to `examples/audio/`. The fixtures
  subdir is for synthetic, deterministic content only.
- Don't bump versions on `main` — the publish workflow does that from tags.
- Don't add a language without going through the full checklist above.
- Don't disable `buf breaking` to push through a wire change. If it's
  intentionally breaking, document it in the PR and bump the major version.
- Don't rename example output strings (e.g., `Session:`, `Analysis | offset=`,
  `📞 Session:`, `Call ended`) without updating the regex assertions in the
  smoke tests — silent breakage will pass CI locally and fail mysteriously
  for someone else.
