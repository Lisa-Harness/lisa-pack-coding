# HTTP handlers

## Composition

`server.go` defines `routerDeps` and `newRouter`.
Dependencies are supplied by startup or by a test fixture.
The private mux handles resource routes.
The public mux exposes health and wraps the private mux with bearer auth.
Use Go 1.23 ServeMux method patterns and `PathValue`.
Do not manually split URLs or substitute a different router.

Examples of method patterns:

```go
mux.HandleFunc("POST /reservations", handler)
mux.HandleFunc("PUT /reservations/{mac}", updateHandler)
```

The constructor must remain useful without opening a listening socket.
It must not inspect environment variables or flags.
The process entrypoint is responsible for configuration validation.

## Resource map

Lease reads belong in `lease_handlers.go`.
Snapshot parsing belongs in `leases.go`.
Reservation reads and mutations belong in `reservations.go`.
File persistence belongs in `hostdir.go`.
Auth parsing and comparison belong in the token files.
Shared response encoding belongs in `server.go`.
Do not register a second copy of the routes in tests.

## JSON input

`decodeBody` limits the request body to 16 KiB.
It rejects unknown fields and trailing JSON documents.
Create and update decode an object, not an array or scalar.
A JSON null object is rejected by the handler.
Keep request-field names aligned with README examples.
A malformed field type is a request error, not a zero value to repair.
Normalize MAC, IPv4, and hostname before persisting a record.
The URL identifies the record for PUT.
An optional body MAC must agree with the URL after normalization.

## Response shape

Use `writeJSON` for handler-generated JSON responses.
Use `writeError` for a simple error code.
Required fields should not acquire `omitempty` tags.
An empty hostname is a meaningful empty string.
Collections should use arrays even when empty.
Do not return an internal filesystem path to API users.
DELETE succeeds with an empty 204 response.
Do not encode a JSON object after a 204 status.

## Status rules

| Status | Existing role |
| --- | --- |
| 200 | successful read or replacement |
| 201 | successful reservation creation |
| 204 | successful withdrawal |
| 400 | malformed JSON or invalid create input |
| 401 | missing or incorrect token |
| 404 | missing reservation or matching lease |
| 409 | duplicate MAC on creation |
| 413 | oversized body |
| 422 | replacement missing IP |
| 500 | failed Store operation |
| 503 | unavailable lease source or failed publisher |

Unknown routes and method mismatch are owned by ServeMux.
Do not depend on their text response as a domain JSON contract.
Preserve successful status codes when changing a write workflow.
Keep new machine-readable errors separate from human diagnostics.

## Mutation ordering

Validate fields before invoking Store methods.
Serialize writes on the API instance.
Call `Put` or `Delete` before publishing.
Return a successful response only after publication succeeds.
Do not publish after failed persistence.
Do not schedule publication in a detached goroutine.
A reload error does not undo the durable file write.
This is why the handler returns `publish_failed` rather than pretending success.

## Authentication

The fixed token is part of router dependencies.
Health is the only public application route.
The middleware requires a two-field Bearer header.
The scheme is case-insensitive; token bytes are not.
Blank configured tokens never authenticate a request.
Compare hashed token values with constant-time comparison.
Send a Bearer challenge with unauthorized responses.
Never put a token in a query parameter or response body.

## Handler change checklist

1. Identify the owning file.
2. Add a method pattern if a route is genuinely new.
3. Decode and validate before I/O.
4. Map operational errors without leaking paths.
5. Preserve response fields and status expectations.
6. Test with `newRouter` and `httptest`.
7. Assert failure side effects, not only the status.
8. Update the README endpoint table when necessary.
