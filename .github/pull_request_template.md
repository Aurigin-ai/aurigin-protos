<!--
Keep PRs small and self-describing. CI must pass before merge:
buf lint/format/breaking, TS build + tsc strict, Python build + import
smoke test, shellcheck. See `.github/workflows/ci.yml`.
-->

## Summary

<!-- One paragraph: what's changing and why. -->

## Changes

<!-- Bullet list of the concrete edits. Reference files when helpful. -->

-

## Wire / API impact

<!-- Tick whichever applies. Be honest about wire breaks — consumers depend on this package. -->

- [ ] No proto changes
- [ ] Proto change is **additive** (new field/message/RPC; old clients keep working)
- [ ] Proto change is **wire-breaking** (renamed/removed field, changed numbers, etc.) — describe the migration below

## Verification

<!-- What did you actually run? `make lint`, `make generate`, runtime smoke-test, etc. -->

-

## Notes for reviewers

<!-- Optional: tricky decisions, follow-ups, things you skipped on purpose. -->
