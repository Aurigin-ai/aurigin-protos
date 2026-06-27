// Graceful SIGINT/SIGTERM handler for in-flight gRPC bidi streams.
//
// Lets the example clients tear down active streams cleanly on Ctrl-C
// instead of dumping a stack trace. Pulled into common/ so phone_call.ts
// + phone_call_burst.ts don't each re-implement the boilerplate.
//
// Usage:
//   const calls: Call[] = [...];
//   const shutdown = installSignalShutdown(calls);
//   const results = await Promise.allSettled(perCallPromises);
//   if (shutdown.seen) { ... } // signal-triggered, not natural completion
//
// Behaviour:
//   - First SIGINT/SIGTERM: prints a banner to stderr and calls .cancel()
//     on every still-open stream in the list.
//   - Subsequent signals: no-op (so a frantic Ctrl-C-Ctrl-C doesn't double-
//     cancel or re-print the banner).
//   - Captures the call list by reference at install time — calls added
//     later won't be cancelled. The example clients create all their
//     streams up front, so this is fine.

// We avoid importing the concrete grpc-js types here so the helper doesn't
// depend on @grpc/grpc-js. Any object with a `.cancel()` method works.
interface Cancellable {
  cancel(): void;
}

export interface Shutdown {
  seen: boolean;
}

export function installSignalShutdown(calls: Cancellable[]): Shutdown {
  const state: Shutdown = { seen: false };

  const request = (signame: string): void => {
    if (state.seen) return;
    state.seen = true;
    process.stderr.write(`\nReceived ${signame}, cancelling streams...\n`);
    for (const c of calls) {
      try { c.cancel(); } catch { /* best-effort */ }
    }
  };

  process.on("SIGINT", () => request("SIGINT"));
  process.on("SIGTERM", () => request("SIGTERM"));
  return state;
}
