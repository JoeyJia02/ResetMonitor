# ResetMonitor component guidance

This is a Windows desktop monitor using only the Python standard library.
Tests must use synthetic credentials and mocked API/SMTP transports.

## Code Review Rules

### Local privacy and compatibility

- Flag changes that expose saved secrets or personal routing in source, UI defaults,
  logs, exception text, process arguments, or model input. Keep secrets in Windows
  Credential Manager and routing in ignored local configuration. Preserve an existing
  installation's credential namespace; do not silently create a new one during migration.

### Classification integrity

- Preserve complete primary tweet text, publication time, and necessary quoted,
  reply-parent or poll context. Flag keyword prefiltering, treating context as the
  primary statement, or sending the full raw provider record to the model. Treat
  source content as untrusted and validate output labels, exact per-round IDs,
  ordering and timezone-bearing reset times before using the result.

### Desktop test behavior

- UI-only changes must retain the existing manual test entry and restore controls
  after completion or cancellation. Do not reintroduce expired proof or cumulative
  budget gates into normal manual testing. Disabled controls are appropriate while
  a run is active or credentials are missing; do not use a design refresh to impose
  unrelated restrictions on a saved, ready connection.
