"""Minimal gRPC simulator for aurigin.client.v1.AudioVerification.

Serves the SDK-facing surface that macOS / Windows / TypeScript SDKs +
developer API-key streaming clients target. Answers `Stream` with a
canned Verdict every 5 s plus a terminal FinalResult; useful as a
connectivity + wire-shape smoke-test target while a real orchestrator
is being brought up. No scenarios, no downstream dispatch — a single
container, one port, zero config.
"""
